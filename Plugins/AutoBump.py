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

# ВАЖНО: Эта функция-обертка исправляет ошибку 422 "missing query app"
# Она принимает ТОЛЬКО request, а app достает сама внутри.
async def get_current_user(request: Request):
    return await get_current_user_raw(request.app, request)

class CloudBumpSettings(BaseModel):
    golden_key: str
    node_ids: list[str]
    active: bool

# --- ПАРСИНГ ОШИБОК FUNPAY ---
def parse_wait_time(text: str) -> int:
    if not text: return 0
    text = text.lower()
    hours = 0
    minutes = 0
    h_match = re.search(r'(\d+)\s*(?:ч|h|hour|час)', text)
    if h_match: hours = int(h_match.group(1))
    m_match = re.search(r'(\d+)\s*(?:м|min|мин)', text)
    if m_match: minutes = int(m_match.group(1))
    total = (hours * 3600) + (minutes * 60)
    if total == 0 and ("подож" in text or "wait" in text): return 3600
    return total

def extract_site_message(html_content: str) -> str:
    if not html_content: return ""
    match = re.search(r'<div[^>]*id=["\']site-message["\'][^>]*>(.*?)</div>', html_content, re.DOTALL | re.IGNORECASE)
    if match:
        clean = html_lib.unescape(match.group(1)).strip()
        return re.sub(r'<[^>]+>', '', clean)
    return ""

# --- ВОРКЕР ---
async def worker(app):
    print(">>> [PLUGIN] AutoBump Worker v5 Started")
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

                        print(f"--> [Job] User {uid}: Processing {len(nodes)} nodes")
                        
                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "X-Requested-With": "XMLHttpRequest",
                            "Origin": "https://funpay.com"
                        }
                        cookies = {"golden_key": golden_key}
                        first_node = nodes[0]
                        
                        # 1. Заходим на страницу (GET)
                        async with session.get(f"https://funpay.com/lots/{first_node}/trade", headers=headers, cookies=cookies) as resp:
                            if resp.status != 200:
                                await update_next_run(pool, uid, 300) # 5 мин
                                continue
                            html = await resp.text()

                        # Проверка таймера в HTML
                        msg = extract_site_message(html)
                        if msg and ("подож" in msg.lower() or "wait" in msg.lower()):
                            wait = parse_wait_time(msg)
                            print(f"--- [Wait] Timer found: {wait}s")
                            await update_next_run(pool, uid, wait + random.randint(60, 300))
                            continue

                        # Парсинг токенов
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
                        
                        if not csrf or not game_id:
                            print(f"--- [Err] Missing tokens for {uid}")
                            await update_next_run(pool, uid, 600) # 10 мин
                            continue

                        # 2. Поднятие (POST)
                        payload = {"game_id": game_id, "node_id": first_node, "csrf_token": csrf}
                        async with session.post("https://funpay.com/lots/raise", data=payload, headers=headers, cookies=cookies) as post_resp:
                            txt = await post_resp.text()
                            try:
                                js = await post_resp.json()
                                err = js.get('error', False)
                                msg = js.get('msg', '')
                            except:
                                msg = extract_site_message(txt)
                                err = True if msg else False

                            if not err:
                                print(f"--- [Success] Bumped! Next in ~4h")
                                await update_next_run(pool, uid, (4*3600) + random.randint(60, 300))
                            else:
                                print(f"--- [Fail] {msg}")
                                wait = parse_wait_time(msg)
                                if wait > 0: await update_next_run(pool, uid, wait + random.randint(60, 300))
                                else: await update_next_run(pool, uid, 3600)

                    except Exception as e:
                        print(f"!!! Error {uid}: {e}")
                        await update_next_run(pool, uid, 600)

            await asyncio.sleep(2)
        except Exception as e:
            print(f"!!! CRIT: {e}")
            await asyncio.sleep(30)

async def update_next_run(pool, uid, seconds):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE autobump_tasks SET last_bump_at=NOW(), next_bump_at=NOW() + interval '1 second' * $1 WHERE user_uid=$2", seconds, uid)

# --- API ---
@router.post("/set")
async def set_autobump(data: CloudBumpSettings, request: Request, user=Depends(get_current_user)):
    async with request.app.state.pool.acquire() as conn:
        enc_key = encrypt_data(data.golden_key)
        nodes_str = ",".join(data.node_ids)
        # Ставим NOW(), чтобы проверить сразу
        await conn.execute("""
            INSERT INTO autobump_tasks (user_uid, encrypted_golden_key, node_ids, is_active, next_bump_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (user_uid) DO UPDATE SET 
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                node_ids = EXCLUDED.node_ids,
                is_active = EXCLUDED.is_active,
                next_bump_at = NOW() 
        """, user['uid'], enc_key, nodes_str, data.active)
    return {"status": "success", "active": data.active}

@router.get("/status")
async def get_autobump_status(request: Request, user=Depends(get_current_user)):
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_active, last_bump_at, next_bump_at FROM autobump_tasks WHERE user_uid=$1", user['uid'])
    if not row: return {"is_active": False}
    return {"is_active": row['is_active'], "last_bump": row['last_bump_at'], "next_bump": row['next_bump_at']}
