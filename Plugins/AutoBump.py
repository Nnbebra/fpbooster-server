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

# --- ПАРСИНГ ОШИБОК FUNPAY ---

def parse_wait_time(text: str) -> int:
    """Парсит 'Подождите 3 ч. 15 мин.' или 'Подождите 4 часа.'"""
    if not text: return 0
    text = text.lower()
    
    hours = 0
    minutes = 0
    
    # Регулярка для часов (ч, час, часа, часов, h, hour)
    h_match = re.search(r'(\d+)\s*(?:ч|h|hour|час)', text)
    if h_match:
        hours = int(h_match.group(1))
        
    # Регулярка для минут (м, мин, m, min)
    m_match = re.search(r'(\d+)\s*(?:м|min|мин)', text)
    if m_match:
        minutes = int(m_match.group(1))
        
    total_seconds = (hours * 3600) + (minutes * 60)
    
    # Если цифр не нашли, но есть слово 'подождите', ставим час
    if total_seconds == 0 and ("подож" in text or "wait" in text):
        return 3600
        
    return total_seconds

def extract_site_message(html_content: str) -> str:
    """Вытаскивает текст из <div id="site-message">...</div>"""
    # Ищем конкретный div с id="site-message"
    match = re.search(r'<div[^>]*id=["\']site-message["\'][^>]*>(.*?)</div>', html_content, re.DOTALL | re.IGNORECASE)
    if match:
        return html_lib.unescape(match.group(1)).strip()
    return ""

# --- ВОРКЕР ---
async def worker(app):
    print(">>> [PLUGIN] AutoBump Worker v4 (HTML Parsing Support)")
    
    RE_GAME_ID = [re.compile(r'data-game-id="(\d+)"'), re.compile(r'data-game="(\d+)"')]
    RE_APP_DATA = re.compile(r'data-app-data="([^"]+)"')
    RE_CSRF = re.compile(r'csrf-token["\'][^>]+content=["\']([^"\']+)["\']')

    while True:
        try:
            pool = app.state.pool
            
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
                await asyncio.sleep(10)
                continue

            async with aiohttp.ClientSession() as session:
                for task in tasks:
                    uid = task['user_uid']
                    
                    try:
                        golden_key = decrypt_data(task['encrypted_golden_key'])
                        nodes = [n.strip() for n in task['node_ids'].split(',') if n.strip()]
                        
                        if not nodes: continue

                        # ЛОГ НА СЕРВЕРЕ (для твоей отладки)
                        key_preview = golden_key[:6] + "***"
                        print(f">>> [Job] User: {uid} | Key: {key_preview} | Nodes: {len(nodes)}")

                        first_node = nodes[0]
                        
                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "X-Requested-With": "XMLHttpRequest",
                            "Origin": "https://funpay.com"
                        }
                        cookies = {"golden_key": golden_key}

                        # 1. GET Request
                        async with session.get(f"https://funpay.com/lots/{first_node}/trade", headers=headers, cookies=cookies) as resp:
                            if resp.status != 200:
                                print(f"--- Error getting page: {resp.status}")
                                # Откладываем на 5 минут при ошибке сети
                                async with pool.acquire() as conn:
                                    await conn.execute("UPDATE autobump_tasks SET next_bump_at = NOW() + interval '5 minutes' WHERE user_uid=$1", uid)
                                continue
                            html = await resp.text()

                        # --- ПРОВЕРКА НА ОШИБКУ ТАЙМЕРА В HTML (СРАЗУ) ---
                        # Иногда FunPay показывает ошибку прямо на странице, не давая кнопку
                        site_msg = extract_site_message(html)
                        if site_msg and ("подож" in site_msg.lower() or "wait" in site_msg.lower()):
                            wait_sec = parse_wait_time(site_msg)
                            print(f"--- [Wait] Found timer on page: {site_msg} ({wait_sec}s)")
                            next_run = wait_sec + random.randint(60, 300)
                            async with pool.acquire() as conn:
                                await conn.execute("UPDATE autobump_tasks SET last_bump_at=NOW(), next_bump_at=NOW() + interval '1 second' * $1 WHERE user_uid=$2", next_run, uid)
                            continue

                        # Parsing Logic (CSRF & GameID)
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
                            print(f"--- No CSRF found for {uid}")
                            continue

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
                            print(f"--- No GameID found for {first_node}")
                            continue

                        # 2. POST Bump
                        raise_url = "https://funpay.com/lots/raise"
                        payload = {
                            "game_id": game_id,
                            "node_id": first_node,
                            "csrf_token": csrf_token
                        }
                        
                        next_run_seconds = (4 * 3600) + random.randint(60, 300) # Default success timer

                        async with session.post(raise_url, data=payload, headers=headers, cookies=cookies) as post_resp:
                            resp_text = await post_resp.text()
                            
                            # Пытаемся понять ответ
                            try:
                                resp_json = await post_resp.json()
                                msg = resp_json.get('msg', '')
                                error = resp_json.get('error', False)
                            except:
                                # Если не JSON, ищем ошибку в HTML ответа
                                msg = extract_site_message(resp_text)
                                error = True if msg else False

                            if not error:
                                print(f"--- [Success] Bumped node {first_node}")
                                # Тут можно пройтись по остальным нодам (nodes[1:])
                            else:
                                print(f"--- [FunPay Msg] {msg}")
                                wait_sec = parse_wait_time(msg)
                                if wait_sec > 0:
                                    next_run_seconds = wait_sec + random.randint(120, 300)

                        # Update DB
                        async with pool.acquire() as conn:
                            await conn.execute("""
                                UPDATE autobump_tasks 
                                SET last_bump_at = NOW(), 
                                    next_bump_at = NOW() + interval '1 second' * $1 
                                WHERE user_uid = $2
                            """, next_run_seconds, uid)

                    except Exception as e:
                        print(f"!!! Error processing task {uid}: {e}")
                        async with pool.acquire() as conn:
                            await conn.execute("UPDATE autobump_tasks SET next_bump_at = NOW() + interval '10 minutes' WHERE user_uid=$1", uid)

            await asyncio.sleep(2)

        except Exception as e:
            print(f"!!! CRITICAL WORKER ERROR: {e}")
            await asyncio.sleep(30)

# --- API ---
@router.post("/set")
async def set_autobump(data: CloudBumpSettings, request: Request, user=Depends(get_current_user)):
    async with request.app.state.pool.acquire() as conn:
        enc_key = encrypt_data(data.golden_key)
        nodes_str = ",".join(data.node_ids)
        
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
