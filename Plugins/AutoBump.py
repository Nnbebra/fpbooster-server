import asyncio
import re
import html as html_lib
import logging
import random
from datetime import datetime, timedelta

import aiohttp
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel

from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autobump", tags=["AutoBump Plugin"])

async def get_current_user(request: Request):
    return await get_current_user_raw(request.app, request)

class CloudBumpSettings(BaseModel):
    golden_key: str
    node_ids: list[str]
    active: bool

# --- ЛОГИКА ПАРСИНГА ВРЕМЕНИ ---
def parse_wait_time(error_msg: str) -> int:
    """Парсит сообщение FunPay 'Подождите 3 ч. 15 мин.' и возвращает секунды"""
    # Шаблоны для русского и английского языка
    # Пример: "Подождите 3 ч.", "3 hours", "15 мин.", "wait 15 min"
    
    hours = 0
    minutes = 0
    
    # Поиск часов
    h_match = re.search(r'(\d+)\s*(?:ч|h|hour)', error_msg, re.IGNORECASE)
    if h_match:
        hours = int(h_match.group(1))
        
    # Поиск минут
    m_match = re.search(r'(\d+)\s*(?:м|min)', error_msg, re.IGNORECASE)
    if m_match:
        minutes = int(m_match.group(1))
        
    total_seconds = (hours * 3600) + (minutes * 60)
    
    # Если не нашли времени, но ошибка есть - ставим дефолт 1 час для безопасности
    if total_seconds == 0 and ("подожд" in error_msg.lower() or "wait" in error_msg.lower()):
        return 3600
        
    return total_seconds

# --- ВОРКЕР ---
async def worker(app):
    print(">>> [PLUGIN] Smart AutoBump Worker Started (v3 - Time Parsing)")
    
    RE_GAME_ID = [re.compile(r'data-game-id="(\d+)"'), re.compile(r'data-game="(\d+)"')]
    RE_APP_DATA = re.compile(r'data-app-data="([^"]+)"')
    RE_CSRF = re.compile(r'csrf-token["\'][^>]+content=["\']([^"\']+)["\']')

    while True:
        try:
            pool = app.state.pool
            
            # Берем задачи, время которых пришло (или новые)
            async with pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT user_uid, encrypted_golden_key, node_ids 
                    FROM autobump_tasks 
                    WHERE is_active = TRUE 
                    AND (next_bump_at IS NULL OR next_bump_at < NOW())
                    ORDER BY next_bump_at ASC NULLS FIRST
                    LIMIT 5
                """)

            if not tasks:
                await asyncio.sleep(15)
                continue

            async with aiohttp.ClientSession() as session:
                for task in tasks:
                    uid = task['user_uid']
                    
                    try:
                        golden_key = decrypt_data(task['encrypted_golden_key'])
                        nodes = [n.strip() for n in task['node_ids'].split(',') if n.strip()]
                        
                        if not nodes: continue

                        # Используем первый NodeID для получения токенов (CSRF общий для аккаунта)
                        first_node = nodes[0]
                        
                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "X-Requested-With": "XMLHttpRequest",
                            "Origin": "https://funpay.com"
                        }
                        cookies = {"golden_key": golden_key}

                        # 1. GET (Получаем CSRF)
                        async with session.get(f"https://funpay.com/lots/{first_node}/trade", headers=headers, cookies=cookies) as resp:
                            if resp.status != 200:
                                # Ошибка доступа к сайту, откладываем на 5 мин
                                async with pool.acquire() as conn:
                                    await conn.execute("UPDATE autobump_tasks SET next_bump_at = NOW() + interval '5 minutes' WHERE user_uid=$1", uid)
                                continue
                            html = await resp.text()

                        csrf_token = None
                        m_csrf = RE_CSRF.search(html)
                        if m_csrf: csrf_token = m_csrf.group(1)
                        
                        if not csrf_token:
                            # Fallback: ищем в app-data
                            m_app = RE_APP_DATA.search(html)
                            if m_app:
                                blob = html_lib.unescape(m_app.group(1))
                                m_c = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob)
                                if m_c: csrf_token = m_c.group(1)

                        if not csrf_token:
                            print(f"[Err] No CSRF for {uid}")
                            continue

                        # 2. ПОПЫТКА ПОДНЯТИЯ (Пробуем поднять ПЕРВЫЙ лот, чтобы узнать таймер)
                        # FunPay вернет ошибку таймера даже если мы пытаемся поднять один лот
                        
                        # Парсим GameID (нужен для запроса)
                        game_id = None
                        m_app = RE_APP_DATA.search(html)
                        if m_app:
                            blob = html_lib.unescape(m_app.group(1))
                            m_g = re.search(r'"game-id"\s*:\s*(\d+)', blob)
                            if m_g: game_id = m_g.group(1)
                        
                        if not game_id:
                            # Простой поиск
                            m_g = re.search(r'data-game-id="(\d+)"', html)
                            if m_g: game_id = m_g.group(1)

                        if not game_id: continue

                        # ОТПРАВЛЯЕМ ЗАПРОС
                        raise_url = "https://funpay.com/lots/raise"
                        payload = {
                            "game_id": game_id,
                            "node_id": first_node,
                            "csrf_token": csrf_token
                        }
                        
                        # Поднимаем ВСЕ выбранные категории
                        # Но сначала проверяем ответ от первой, чтобы понять таймер
                        
                        next_run_seconds = 4 * 3600 # Дефолт 4 часа
                        server_message = "Raised"

                        async with session.post(raise_url, data=payload, headers=headers, cookies=cookies) as post_resp:
                            resp_json = await post_resp.json()
                            
                            # ЛОГИКА ОБРАБОТКИ ОТВЕТА
                            if not resp_json.get('error'):
                                # УСПЕХ! Поднимаем остальные ноды (если есть)
                                if len(nodes) > 1:
                                    for other_node in nodes[1:]:
                                        # Нужно найти game_id для других нод, это сложнее.
                                        # Для оптимизации: часто game_id совпадает, если игра та же.
                                        # Но правильно - делать GET запрос на каждую.
                                        # В рамках оптимизации пока поднимаем только ту, где есть game_id, 
                                        # или (TODO) надо парсить game_id для всех.
                                        pass 
                                
                                # Таймер: 4 часа + рандом 1-5 минут
                                next_run_seconds = (4 * 3600) + random.randint(60, 300)
                                server_message = "Success"
                                
                            else:
                                # ОШИБКА (Скорее всего таймер)
                                msg = resp_json.get('msg', '')
                                server_message = msg
                                wait_sec = parse_wait_time(msg)
                                
                                if wait_sec > 0:
                                    # FunPay сказал ждать. Добавляем 2-4 минуты "человеческого" лага
                                    next_run_seconds = wait_sec + random.randint(120, 240)
                                    print(f"[Smart] User {uid} must wait: {wait_sec}s. Setting timer.")
                                else:
                                    # Какая-то другая ошибка, пробуем через час
                                    next_run_seconds = 3600

                        # Обновляем базу
                        async with pool.acquire() as conn:
                            await conn.execute("""
                                UPDATE autobump_tasks 
                                SET last_bump_at = NOW(), 
                                    next_bump_at = NOW() + interval '1 second' * $1 
                                WHERE user_uid = $2
                            """, next_run_seconds, uid)

                    except Exception as e:
                        print(f"[Err] Task {uid}: {e}")
                        async with pool.acquire() as conn:
                            await conn.execute("UPDATE autobump_tasks SET next_bump_at = NOW() + interval '10 minutes' WHERE user_uid=$1", uid)

            await asyncio.sleep(2)

        except Exception as e:
            print(f"[Crit] Loop error: {e}")
            await asyncio.sleep(30)

# --- API ---
@router.post("/set")
async def set_autobump(data: CloudBumpSettings, request: Request, user=Depends(get_current_user)):
    async with request.app.state.pool.acquire() as conn:
        enc_key = encrypt_data(data.golden_key)
        nodes_str = ",".join(data.node_ids)
        
        # При включении ставим NOW(), чтобы воркер СРАЗУ проверил FunPay
        await conn.execute("""
            INSERT INTO autobump_tasks (user_uid, encrypted_golden_key, node_ids, is_active, next_bump_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (user_uid) DO UPDATE SET 
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                node_ids = EXCLUDED.node_ids,
                is_active = EXCLUDED.is_active,
                next_bump_at = CASE WHEN EXCLUDED.is_active = TRUE THEN NOW() ELSE NULL END
        """, user['uid'], enc_key, nodes_str, data.active)

    return {"status": "success", "active": data.active}

@router.get("/status")
async def get_autobump_status(request: Request, user=Depends(get_current_user)):
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT is_active, last_bump_at, next_bump_at 
            FROM autobump_tasks WHERE user_uid=$1
        """, user['uid'])
    
    if not row: return {"is_active": False}

    return {
        "is_active": row['is_active'],
        "last_bump": row['last_bump_at'],
        "next_bump": row['next_bump_at']
    }
