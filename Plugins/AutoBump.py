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

# --- ПАРСИНГ ВРЕМЕНИ ---
def parse_wait_time(text: str) -> int:
    """Парсит строки вида 'Подождите 4 часа.' или '3 ч. 15 мин.'"""
    if not text: return 0
    text = text.lower()
    
    hours = 0
    minutes = 0
    
    # 4 часа, 3 ч, 1 hour
    h_match = re.search(r'(\d+)\s*(?:ч|h|hour|час)', text)
    if h_match:
        hours = int(h_match.group(1))
        
    # 15 мин, 10 min, 10 м
    m_match = re.search(r'(\d+)\s*(?:м|min|мин)', text)
    if m_match:
        minutes = int(m_match.group(1))
        
    total_seconds = (hours * 3600) + (minutes * 60)
    
    # Фолбэк: если цифр нет, но есть слово "подождите", ставим 1 час
    if total_seconds == 0 and ("подож" in text or "wait" in text):
        return 3600
        
    return total_seconds

def extract_site_message(html_content: str) -> str:
    """Вытаскивает текст ошибки из HTML"""
    if not html_content: return ""
    # Ищем div с id="site-message"
    # Регулярка учитывает, что после id могут быть class, style и т.д.
    match = re.search(r'<div[^>]*id=["\']site-message["\'][^>]*>(.*?)</div>', html_content, re.DOTALL | re.IGNORECASE)
    if match:
        clean_text = html_lib.unescape(match.group(1)).strip()
        # Удаляем HTML теги внутри, если есть
        clean_text = re.sub(r'<[^>]+>', '', clean_text)
        return clean_text
    return ""

# --- ВОРКЕР ---
async def worker(app):
    print(">>> [PLUGIN] AutoBump Worker v5 (Robust Logic)")
    
    # Регулярки для парсинга данных лота
    RE_GAME_ID = [re.compile(r'data-game-id="(\d+)"'), re.compile(r'data-game="(\d+)"')]
    RE_APP_DATA = re.compile(r'data-app-data="([^"]+)"')
    RE_CSRF = re.compile(r'csrf-token["\'][^>]+content=["\']([^"\']+)["\']')

    while True:
        try:
            pool = app.state.pool
            
            # Берем задачи, которые пора выполнять
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
                await asyncio.sleep(5)
                continue

            async with aiohttp.ClientSession() as session:
                for task in tasks:
                    uid = task['user_uid']
                    
                    try:
                        golden_key = decrypt_data(task['encrypted_golden_key'])
                        nodes = [n.strip() for n in task['node_ids'].split(',') if n.strip()]
                        
                        if not nodes: continue
                        
                        # Лог для отладки на сервере
                        print(f"--> [Job] Processing User {uid}. Nodes: {len(nodes)}")

                        # Имитация браузера
                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "X-Requested-With": "XMLHttpRequest",
                            "Origin": "https://funpay.com",
                            "Referer": "https://funpay.com/users/" 
                        }
                        cookies = {"golden_key": golden_key}

                        # Используем первую ноду для проверки таймера и получения токенов
                        first_node = nodes[0]
                        next_run_seconds = 4 * 3600 # Дефолт: 4 часа
                        
                        # 1. ЗАХОДИМ НА СТРАНИЦУ (GET)
                        async with session.get(f"https://funpay.com/lots/{first_node}/trade", headers=headers, cookies=cookies) as resp:
                            if resp.status != 200:
                                print(f"--- [Err] HTTP {resp.status} for {first_node}")
                                # Ошибка сети - пробуем через 5 мин
                                async with pool.acquire() as conn:
                                    await conn.execute("UPDATE autobump_tasks SET next_bump_at = NOW() + interval '5 minutes' WHERE user_uid=$1", uid)
                                continue
                            html = await resp.text()

                        # 2. ПРОВЕРЯЕМ ТАЙМЕР НА СТРАНИЦЕ
                        # FunPay часто пишет "Подождите..." прямо в HTML, если зайти раньше времени
                        site_msg = extract_site_message(html)
                        if site_msg and ("подож" in site_msg.lower() or "wait" in site_msg.lower()):
                            wait_sec = parse_wait_time(site_msg)
                            print(f"--- [Timer] Found on page: '{site_msg}' -> {wait_sec}s")
                            # Ставим таймер + рандом 2-5 мин
                            next_run_seconds = wait_sec + random.randint(120, 300)
                            
                            async with pool.acquire() as conn:
                                await conn.execute("""
                                    UPDATE autobump_tasks 
                                    SET last_bump_at = NOW(), 
                                        next_bump_at = NOW() + interval '1 second' * $1 
                                    WHERE user_uid = $2
                                """, next_run_seconds, uid)
                            continue # Переходим к следующему юзеру

                        # 3. ИЩЕМ ТОКЕНЫ (CSRF + GameID)
                        csrf_token = None
                        m_csrf = RE_CSRF.search(html)
                        if m_csrf: csrf_token = m_csrf.group(1)
                        
                        if not csrf_token:
                            m_app = RE_APP_DATA.search(html)
                            if m_app:
                                blob = html_lib.unescape(m_app.group(1))
                                m_c = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob)
                                if m_c: csrf_token = m_c.group(1)

                        if not csrf_token:
                            print(f"--- [Err] No CSRF token found for {uid}")
                            continue # Не обновляем таймер, попробует снова в след цикле

                        game_id = None
                        m_app = RE_APP_DATA.search(html)
                        if m_app:
                            blob = html_lib.unescape(m_app.group(1))
                            m_g = re.search(r'"game-id"\s*:\s*(\d+)', blob)
                            if m_g: game_id = m_g.group(1)
                        
                        if not game_id:
                            m_g = re.search(r'data-game-id="(\d+)"', html)
                            if m_g: game_id = m_g.group(1)

                        if not game_id:
                            print(f"--- [Err] No GameID found for {first_node}")
                            continue

                        # 4. ПРОБУЕМ ПОДНЯТЬ (POST)
                        raise_url = "https://funpay.com/lots/raise"
                        payload = {
                            "game_id": game_id,
                            "node_id": first_node,
                            "csrf_token": csrf_token
                        }

                        async with session.post(raise_url, data=payload, headers=headers, cookies=cookies) as post_resp:
                            resp_text = await post_resp.text()
                            
                            # Пытаемся понять, что ответил FunPay
                            is_error = False
                            msg = ""
                            
                            try:
                                resp_json = await post_resp.json()
                                msg = resp_json.get('msg', '')
                                is_error = resp_json.get('error', False)
                            except:
                                # Не JSON, ищем ошибку в HTML
                                msg = extract_site_message(resp_text)
                                if msg: is_error = True

                            if not is_error:
                                print(f"--- [Success] Bumped {first_node}!")
                                # Успешно! Ставим 4 часа + рандом
                                next_run_seconds = (4 * 3600) + random.randint(60, 300)
                                
                                # (Опционально: тут можно пройтись циклом по остальным nodes[1:])
                            else:
                                print(f"--- [Fail] FunPay msg: {msg}")
                                # Если ошибка таймера, парсим время
                                wait_sec = parse_wait_time(msg)
                                if wait_sec > 0:
                                    next_run_seconds = wait_sec + random.randint(120, 300)
                                else:
                                    # Непонятная ошибка, пробуем через час
                                    next_run_seconds = 3600

                        # 5. ОБНОВЛЯЕМ БАЗУ (САМОЕ ВАЖНОЕ)
                        # Это выводит клиента из состояния "Checking..."
                        async with pool.acquire() as conn:
                            await conn.execute("""
                                UPDATE autobump_tasks 
                                SET last_bump_at = NOW(), 
                                    next_bump_at = NOW() + interval '1 second' * $1 
                                WHERE user_uid = $2
                            """, next_run_seconds, uid)

                    except Exception as e:
                        print(f"!!! Error task {uid}: {e}")
                        # Если скрипт упал, откладываем на 10 мин, чтобы не зависнуть
                        async with pool.acquire() as conn:
                            await conn.execute("UPDATE autobump_tasks SET next_bump_at = NOW() + interval '10 minutes' WHERE user_uid=$1", uid)

            await asyncio.sleep(2)

        except Exception as e:
            print(f"!!! WORKER CRASH: {e}")
            await asyncio.sleep(30)

# --- API ---
@router.post("/set")
async def set_autobump(data: CloudBumpSettings, request: Request, user=Depends(get_current_user)):
    async with request.app.state.pool.acquire() as conn:
        enc_key = encrypt_data(data.golden_key)
        nodes_str = ",".join(data.node_ids)
        
        # При включении ставим NOW(), чтобы проверить сразу
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
