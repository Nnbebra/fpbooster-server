import asyncio
import re
import html as html_lib
import logging
import random
from datetime import datetime, timedelta

import aiohttp
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel

# Импортируем исходную функцию под псевдонимом
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autobump", tags=["AutoBump Plugin"])

# --- ИСПРАВЛЕНИЕ ОШИБКИ 422 ---
# Переименовал, чтобы не конфликтовало с именем модуля/переменной
async def get_user_wrapper(request: Request):
    return await get_current_user_raw(request.app, request)

class CloudBumpSettings(BaseModel):
    golden_key: str
    node_ids: list[str]
    active: bool

# --- ПАРСИНГ FUNPAY (Под твой HTML) ---
def parse_wait_time(text: str) -> int:
    """Парсит: 'Подождите 4 часа.' -> 14400 сек"""
    if not text: return 0
    text = text.lower()
    
    hours = 0
    minutes = 0
    
    # 4 часа, 1 ч, 3 h
    h_match = re.search(r'(\d+)\s*(?:ч|h|hour|час)', text)
    if h_match: hours = int(h_match.group(1))
    
    # 15 мин, 10 min
    m_match = re.search(r'(\d+)\s*(?:м|min|мин)', text)
    if m_match: minutes = int(m_match.group(1))
    
    total = (hours * 3600) + (minutes * 60)
    
    # Если цифр нет, но есть "подождите", ставим 1 час
    if total == 0 and ("подож" in text or "wait" in text):
        return 3600
        
    return total

def extract_site_message(html_content: str) -> str:
    """Ищет текст ошибки в <div id="site-message">...</div>"""
    if not html_content: return ""
    # Ищем конкретный div
    match = re.search(r'<div[^>]*id=["\']site-message["\'][^>]*>(.*?)</div>', html_content, re.DOTALL | re.IGNORECASE)
    if match:
        clean = html_lib.unescape(match.group(1)).strip()
        # Убираем лишние теги внутри, если есть
        return re.sub(r'<[^>]+>', '', clean)
    return ""

async def update_status_db(pool, uid, status_msg, next_bump_in_seconds=None):
    """Пишет статус в базу. Если передан next_bump_in_seconds, обновляет и таймер."""
    async with pool.acquire() as conn:
        if next_bump_in_seconds is not None:
            await conn.execute("""
                UPDATE autobump_tasks 
                SET status_message = $1, 
                    last_bump_at = NOW(),
                    next_bump_at = NOW() + interval '1 second' * $2
                WHERE user_uid = $3
            """, status_msg, next_bump_in_seconds, uid)
        else:
            await conn.execute("UPDATE autobump_tasks SET status_message = $1 WHERE user_uid = $2", status_msg, uid)

# --- ВОРКЕР ---
async def worker(app):
    print(">>> [PLUGIN] AutoBump Worker v8 Started (Fix 422 & HTML Parsing)")
    
    RE_CSRF = re.compile(r'csrf-token["\'][^>]+content=["\']([^"\']+)["\']')
    RE_APP_DATA = re.compile(r'data-app-data="([^"]+)"')

    while True:
        try:
            pool = app.state.pool
            
            # 1. Берем задачи: Активные И (Время пришло ИЛИ Принудительная проверка)
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
                        # Сообщаем клиенту, что процесс пошел
                        await update_status_db(pool, uid, "🔄 Проверка FunPay...")
                        
                        golden_key = decrypt_data(task['encrypted_golden_key'])
                        nodes = [n.strip() for n in task['node_ids'].split(',') if n.strip()]
                        
                        if not nodes:
                            await update_status_db(pool, uid, "Ошибка: Нет NodeID", 3600)
                            continue

                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "X-Requested-With": "XMLHttpRequest",
                            "Origin": "https://funpay.com",
                            "Referer": "https://funpay.com/users/"
                        }
                        cookies = {"golden_key": golden_key}
                        first_node = nodes[0]

                        # --- ШАГ 1: GET (Заходим в лот) ---
                        print(f"--> [Job] Checking user {uid}")
                        async with session.get(f"https://funpay.com/lots/{first_node}/trade", headers=headers, cookies=cookies) as resp:
                            if resp.status != 200:
                                await update_status_db(pool, uid, f"Ошибка доступа ({resp.status})", 300)
                                continue
                            html = await resp.text()

                        # --- ШАГ 2: Ищем таймер в HTML (СРАЗУ) ---
                        # Если FunPay показывает "Подождите..." прямо на странице
                        site_msg = extract_site_message(html)
                        if site_msg and ("подож" in site_msg.lower() or "wait" in site_msg.lower()):
                            wait_sec = parse_wait_time(site_msg)
                            delay = wait_sec + random.randint(120, 240) # +2-4 мин рандома
                            print(f"--- [Wait] Found HTML timer: {wait_sec}s")
                            await update_status_db(pool, uid, f"⏳ FunPay: {site_msg}", delay)
                            continue

                        # --- ШАГ 3: Парсим токены ---
                        csrf = None
                        m = RE_CSRF.search(html)
                        if m: csrf = m.group(1)
                        else:
                            m_app = RE_APP_DATA.search(html)
                            if m_app:
                                blob = html_lib.unescape(m_app.group(1))
                                m_c = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob)
                                if m_c: csrf = m_c.group(1)

                        game_id = None
                        m_g = re.search(r'data-game-id="(\d+)"', html)
                        if m_g: game_id = m_g.group(1)
                        else:
                            m_app = RE_APP_DATA.search(html)
                            if m_app:
                                blob = html_lib.unescape(m_app.group(1))
                                m_g2 = re.search(r'"game-id"\s*:\s*(\d+)', blob)
                                if m_g2: game_id = m_g2.group(1)

                        if not csrf or not game_id:
                            await update_status_db(pool, uid, "Ошибка: Не найдены токены (Auth?)", 600)
                            continue

                        # --- ШАГ 4: POST (Поднятие) ---
                        await update_status_db(pool, uid, "📤 Отправка запроса...")
                        
                        payload = {"game_id": game_id, "node_id": first_node, "csrf_token": csrf}
                        async with session.post("https://funpay.com/lots/raise", data=payload, headers=headers, cookies=cookies) as post_resp:
                            txt = await post_resp.text()
                            success = False
                            error_msg = ""
                            
                            try:
                                js = await post_resp.json()
                                if not js.get('error'): success = True
                                else: error_msg = js.get('msg', '')
                            except:
                                error_msg = extract_site_message(txt)
                                if not error_msg: error_msg = "Неизвестный ответ"

                            if success:
                                # УСПЕХ: 4 часа + 2-5 мин рандома
                                delay = (4 * 3600) + random.randint(120, 300)
                                print(f"[OK] Bumped!")
                                await update_status_db(pool, uid, "✅ Успешно поднято", delay)
                            else:
                                # ОШИБКА: Парсим время из ответа JSON/HTML
                                wait_sec = parse_wait_time(error_msg)
                                if wait_sec > 0:
                                    delay = wait_sec + random.randint(120, 240)
                                    print(f"[Fail] Timer: {error_msg}")
                                    await update_status_db(pool, uid, f"⏳ FunPay: {error_msg}", delay)
                                else:
                                    print(f"[Fail] Error: {error_msg}")
                                    await update_status_db(pool, uid, f"Ошибка: {error_msg[:30]}", 3600)

                    except Exception as e:
                        print(f"[Crit] Task {uid}: {e}")
                        await update_status_db(pool, uid, "Сбой воркера", 600)

            await asyncio.sleep(2)
        except Exception as e:
            print(f"[Crit] Loop: {e}")
            await asyncio.sleep(30)

# --- API ---

@router.post("/set")
async def set_autobump(data: CloudBumpSettings, request: Request, user=Depends(get_user_wrapper)): # <-- ИСПОЛЬЗУЕМ WRAPPER
    async with request.app.state.pool.acquire() as conn:
        enc_key = encrypt_data(data.golden_key)
        nodes_str = ",".join(data.node_ids)
        
        # При сохранении ставим статус "Очередь...", и next_bump=NOW(), чтобы воркер сразу подхватил
        await conn.execute("""
            INSERT INTO autobump_tasks (user_uid, encrypted_golden_key, node_ids, is_active, next_bump_at, status_message)
            VALUES ($1, $2, $3, $4, NOW(), 'В очереди...')
            ON CONFLICT (user_uid) DO UPDATE SET 
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                node_ids = EXCLUDED.node_ids,
                is_active = EXCLUDED.is_active,
                next_bump_at = NOW(),
                status_message = 'Настройки обновлены. Ожидание...'
        """, user['uid'], enc_key, nodes_str, data.active)

    return {"status": "success", "active": data.active}

@router.post("/force_check")
async def force_check_autobump(request: Request, user=Depends(get_user_wrapper)): # <-- ИСПОЛЬЗУЕМ WRAPPER
    async with request.app.state.pool.acquire() as conn:
        # Принудительный сброс таймера на СЕЙЧАС
        await conn.execute("""
            UPDATE autobump_tasks 
            SET next_bump_at = NOW(), 
                status_message = 'Запрос проверки...' 
            WHERE user_uid = $1
        """, user['uid'])
    return {"status": "success"}

@router.get("/status")
async def get_autobump_status(request: Request, user=Depends(get_user_wrapper)): # <-- ИСПОЛЬЗУЕМ WRAPPER
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_active, last_bump_at, next_bump_at, status_message FROM autobump_tasks WHERE user_uid=$1", user['uid'])
    
    if not row: return {"is_active": False}

    return {
        "is_active": row['is_active'],
        "last_bump": row['last_bump_at'],
        "next_bump": row['next_bump_at'],
        "status_message": row['status_message'] or "Ожидание"
    }
