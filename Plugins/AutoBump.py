import asyncio
import re
import html as html_lib
import logging
import random
from datetime import datetime, timedelta

import aiohttp
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel

# ВАЖНО: Импортируем исходную функцию как _raw
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autobump", tags=["AutoBump Plugin"])

# --- ИСПРАВЛЕНИЕ ОШИБКИ 422 ---
# Эта обертка нужна, чтобы FastAPI не искал параметр "app" в URL запроса
async def get_current_user(request: Request):
    return await get_current_user_raw(request.app, request)

class CloudBumpSettings(BaseModel):
    golden_key: str
    node_ids: list[str]
    active: bool

# --- ПАРСИНГ FUNPAY ---
def parse_wait_time(text: str) -> int:
    if not text: return 0
    text = text.lower()
    
    hours = 0
    minutes = 0
    
    # 4 часа, 3 ч, 1 hour
    h_match = re.search(r'(\d+)\s*(?:ч|h|hour|час)', text)
    if h_match: hours = int(h_match.group(1))
    
    # 15 мин, 10 min, 10 м
    m_match = re.search(r'(\d+)\s*(?:м|min|мин)', text)
    if m_match: minutes = int(m_match.group(1))
    
    total = (hours * 3600) + (minutes * 60)
    
    # Фолбэк: если цифр нет, но есть слово "подождите", ставим 1 час
    if total == 0 and ("подож" in text or "wait" in text):
        return 3600
        
    return total

def extract_site_message(html_content: str) -> str:
    """Вытаскивает текст ошибки из HTML <div id='site-message'>"""
    if not html_content: return ""
    match = re.search(r'<div[^>]*id=["\']site-message["\'][^>]*>(.*?)</div>', html_content, re.DOTALL | re.IGNORECASE)
    if match:
        clean = html_lib.unescape(match.group(1)).strip()
        return re.sub(r'<[^>]+>', '', clean) # Удаляем теги внутри
    return ""

# --- ВОРКЕР ---
async def worker(app):
    print(">>> [PLUGIN] AutoBump Worker v5 Started (Fix 422)")
    
    RE_CSRF = re.compile(r'csrf-token["\'][^>]+content=["\']([^"\']+)["\']')
    RE_APP_DATA = re.compile(r'data-app-data="([^"]+)"')

    while True:
        try:
            pool = app.state.pool
            
            # 1. Ищем задачи
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
                    new_status = "Проверка..."
                    next_run_delay = 600 # Дефолт 10 мин при ошибке
                    
                    try:
                        golden_key = decrypt_data(task['encrypted_golden_key'])
                        nodes = [n.strip() for n in task['node_ids'].split(',') if n.strip()]
                        
                        if not nodes:
                            await update_task(pool, uid, 3600, "Нет NodeID")
                            continue

                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "X-Requested-With": "XMLHttpRequest",
                            "Origin": "https://funpay.com"
                        }
                        cookies = {"golden_key": golden_key}
                        first_node = nodes[0]

                        # --- ШАГ 1: GET (Проверка страницы) ---
                        async with session.get(f"https://funpay.com/lots/{first_node}/trade", headers=headers, cookies=cookies) as resp:
                            if resp.status != 200:
                                await update_task(pool, uid, 300, f"Ошибка доступа ({resp.status})")
                                continue
                            html = await resp.text()

                        # Ищем ошибку таймера СРАЗУ в HTML
                        site_msg = extract_site_message(html)
                        if site_msg and ("подож" in site_msg.lower() or "wait" in site_msg.lower()):
                            wait_sec = parse_wait_time(site_msg)
                            next_run_delay = wait_sec + random.randint(120, 180) # +2-3 минуты рандома
                            print(f"--- [Wait] {uid}: Found timer {wait_sec}s")
                            await update_task(pool, uid, next_run_delay, f"FunPay: {site_msg}")
                            continue

                        # Парсим токены
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
                            print(f"--- [Err] {uid}: Tokens not found")
                            await update_task(pool, uid, 600, "Ошибка парсинга (нет токенов)")
                            continue

                        # --- ШАГ 2: POST (Поднятие) ---
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
                                if not error_msg: error_msg = "Unknown response"

                            if success:
                                next_run_delay = (4 * 3600) + random.randint(120, 300)
                                new_status = "✅ Успешно поднято"
                                print(f"--- [OK] {uid}: Bumped!")
                            else:
                                wait_sec = parse_wait_time(error_msg)
                                if wait_sec > 0:
                                    next_run_delay = wait_sec + random.randint(120, 180)
                                    new_status = f"FunPay: {error_msg}"
                                else:
                                    next_run_delay = 3600
                                    new_status = f"Ошибка: {error_msg[:30]}..."
                                print(f"--- [Fail] {uid}: {error_msg}")

                        await update_task(pool, uid, next_run_delay, new_status)

                    except Exception as e:
                        print(f"!!! Error task {uid}: {e}")
                        await update_task(pool, uid, 600, f"Сбой: {str(e)[:20]}")

            await asyncio.sleep(2)
        except Exception as e:
            print(f"!!! CRIT WORKER: {e}")
            await asyncio.sleep(30)

async def update_task(pool, uid, seconds, status_msg):
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE autobump_tasks 
            SET last_bump_at = NOW(),
                next_bump_at = NOW() + interval '1 second' * $1,
                status_message = $2
            WHERE user_uid = $3
        """, seconds, status_msg, uid)

# --- API ---

@router.post("/set")
async def set_autobump(data: CloudBumpSettings, request: Request, user=Depends(get_current_user)):
    async with request.app.state.pool.acquire() as conn:
        enc_key = encrypt_data(data.golden_key)
        nodes_str = ",".join(data.node_ids)
        
        await conn.execute("""
            INSERT INTO autobump_tasks (user_uid, encrypted_golden_key, node_ids, is_active, next_bump_at, status_message)
            VALUES ($1, $2, $3, $4, NOW(), 'Инициализация...')
            ON CONFLICT (user_uid) DO UPDATE SET 
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                node_ids = EXCLUDED.node_ids,
                is_active = EXCLUDED.is_active,
                next_bump_at = NOW(),
                status_message = 'Обновлено пользователем'
        """, user['uid'], enc_key, nodes_str, data.active)

    return {"status": "success", "active": data.active}

@router.post("/force_check")
async def force_check_autobump(request: Request, user=Depends(get_current_user)):
    """Кнопка 'Проверить сейчас'"""
    async with request.app.state.pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM autobump_tasks WHERE user_uid=$1", user['uid'])
        if not exists:
            return {"status": "error", "message": "Сначала включите автоподнятие"}
            
        await conn.execute("""
            UPDATE autobump_tasks 
            SET next_bump_at = NOW(), 
                status_message = 'Принудительная проверка...' 
            WHERE user_uid = $1
        """, user['uid'])
        
    return {"status": "success", "message": "В очереди"}

@router.get("/status")
async def get_autobump_status(request: Request, user=Depends(get_current_user)):
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_active, last_bump_at, next_bump_at, status_message FROM autobump_tasks WHERE user_uid=$1", user['uid'])
    
    if not row: return {"is_active": False}

    return {
        "is_active": row['is_active'],
        "last_bump": row['last_bump_at'],
        "next_bump": row['next_bump_at'],
        "status_message": row['status_message'] or "Ожидание"
    }
