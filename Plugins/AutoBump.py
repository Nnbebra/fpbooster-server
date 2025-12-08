import asyncio
import re
import html as html_lib
import logging
import random
from datetime import datetime, timedelta

import aiohttp
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel

# Используем обертку для авторизации
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autobump", tags=["AutoBump Plugin"])

# --- AUTH WRAPPER ---
async def get_current_user(request: Request):
    return await get_current_user_raw(request.app, request)

# --- MODELS ---
class CloudBumpSettings(BaseModel):
    golden_key: str
    node_ids: list[str]
    active: bool

# --- WORKER LOGIC ---
async def worker(app):
    print(">>> [PLUGIN] AutoBump Smart Worker Started (v2)")
    
    # Регулярки (компилируем один раз для скорости)
    RE_GAME_ID = [
        re.compile(r'class="btn[^"]*js-lot-raise"[^>]*data-game="(\d+)"'),
        re.compile(r'data-game-id="(\d+)"'),
        re.compile(r'data-game="(\d+)"')
    ]
    RE_APP_DATA = re.compile(r'data-app-data="([^"]+)"')
    RE_CSRF = [
        re.compile(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']'),
        re.compile(r'window\._csrf\s*=\s*[\'"]([^\'"]+)[\'"]')
    ]

    while True:
        try:
            pool = app.state.pool
            
            # 1. Берем задачи, у которых подошло время (или они новые)
            # LIMIT 10 - берем небольшими пачками, чтобы не забить память, но обрабатывать часто
            async with pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT user_uid, encrypted_golden_key, node_ids 
                    FROM autobump_tasks 
                    WHERE is_active = TRUE 
                    AND (next_bump_at IS NULL OR next_bump_at < NOW())
                    ORDER BY next_bump_at ASC NULLS FIRST
                    LIMIT 10
                """)

            if not tasks:
                # Если задач нет, спим дольше (экономим ресурсы CPU)
                await asyncio.sleep(20)
                continue

            print(f">>> [AutoBump] Processing batch of {len(tasks)} users...")

            async with aiohttp.ClientSession() as session:
                for task in tasks:
                    uid = task['user_uid']
                    bumped_count = 0
                    
                    try:
                        golden_key = decrypt_data(task['encrypted_golden_key'])
                        nodes = [n.strip() for n in task['node_ids'].split(',') if n.strip()]

                        # Заголовки как у реального браузера
                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                            "X-Requested-With": "XMLHttpRequest",
                            "Origin": "https://funpay.com",
                            "Referer": "https://funpay.com/users/"
                        }
                        cookies = {"golden_key": golden_key}

                        for node_id in nodes:
                            trade_url = f"https://funpay.com/lots/{node_id}/trade"
                            game_id = None
                            csrf_token = None
                            
                            # 1. GET Request (Parsing)
                            async with session.get(trade_url, headers=headers, cookies=cookies) as resp:
                                if resp.status != 200: 
                                    print(f"[Warn] Node {node_id} returned {resp.status}")
                                    continue
                                page_html = await resp.text()

                            # Parse GameID
                            for pattern in RE_GAME_ID:
                                m = pattern.search(page_html)
                                if m: 
                                    game_id = m.group(1)
                                    break
                            
                            if not game_id:
                                # Fallback parse
                                m_app = RE_APP_DATA.search(page_html)
                                if m_app:
                                    blob = html_lib.unescape(m_app.group(1))
                                    m_g = re.search(r'"game-id"\s*:\s*(\d+)', blob)
                                    if m_g: game_id = m_g.group(1)

                            if not game_id: continue

                            # Parse CSRF
                            m_app = RE_APP_DATA.search(page_html)
                            if m_app:
                                blob = html_lib.unescape(m_app.group(1))
                                m_c = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob)
                                if m_c: csrf_token = m_c.group(1)
                            
                            if not csrf_token:
                                for pattern in RE_CSRF:
                                    m = pattern.search(page_html)
                                    if m:
                                        csrf_token = m.group(1)
                                        break

                            # 2. POST Request (Bump)
                            raise_url = "https://funpay.com/lots/raise"
                            payload = {"game_id": game_id, "node_id": node_id}
                            if csrf_token: payload["csrf_token"] = csrf_token

                            post_headers = headers.copy()
                            post_headers["Referer"] = trade_url
                            
                            async with session.post(raise_url, data=payload, headers=post_headers, cookies=cookies) as post_resp:
                                if post_resp.status == 200:
                                    bumped_count += 1

                            # Пауза между лотами одного юзера (имитация человека)
                            await asyncio.sleep(random.uniform(1.5, 3.5))

                        # --- УМНЫЙ ТАЙМЕР (Smart Schedule) ---
                        # FunPay разрешает раз в 4 часа.
                        # Мы ставим 4 часа + 1..4 минуты рандома.
                        # Это гарантирует, что мы не постучимся раньше времени и не словим блок.
                        
                        next_run_seconds = (4 * 3600) + random.randint(60, 240)
                        
                        async with pool.acquire() as conn:
                            await conn.execute("""
                                UPDATE autobump_tasks 
                                SET last_bump_at = NOW(), 
                                    next_bump_at = NOW() + interval '1 second' * $1 
                                WHERE user_uid = $2
                            """, next_run_seconds, uid)
                            
                        print(f">>> [AutoBump] User {uid} finished. Bumped {bumped_count} nodes. Next run in ~4h.")

                    except Exception as e:
                        print(f"[Err] AutoBump Task Error {uid}: {e}")
                        # Если ошибка, пробуем снова через 10 минут, а не долбим сразу
                        async with pool.acquire() as conn:
                            await conn.execute("UPDATE autobump_tasks SET next_bump_at = NOW() + interval '10 minutes' WHERE user_uid=$1", uid)

            # Короткая пауза между пачками юзеров
            await asyncio.sleep(2)

        except Exception as e:
            print(f"[Crit] Worker Loop Error: {e}")
            await asyncio.sleep(30)

# --- API ---

@router.post("/set")
async def set_autobump(data: CloudBumpSettings, request: Request, user=Depends(get_current_user)):
    async with request.app.state.pool.acquire() as conn:
        # Проверка на Plus версию (можно раскомментировать для продакшена)
        # has_plus = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM purchases WHERE user_uid=$1 AND plan ILIKE '%plus%')", user['uid'])
        # if not has_plus: raise HTTPException(403, "Need Plus subscription")

        enc_key = encrypt_data(data.golden_key)
        nodes_str = ",".join(data.node_ids)

        # При включении ставим next_bump_at = NOW(), чтобы поднять сразу
        await conn.execute("""
            INSERT INTO autobump_tasks (user_uid, encrypted_golden_key, node_ids, is_active, next_bump_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (user_uid) DO UPDATE SET 
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                node_ids = EXCLUDED.node_ids,
                is_active = EXCLUDED.is_active,
                next_bump_at = CASE 
                    WHEN EXCLUDED.is_active = TRUE AND autobump_tasks.is_active = FALSE THEN NOW() 
                    ELSE autobump_tasks.next_bump_at 
                END
        """, user['uid'], enc_key, nodes_str, data.active)

    return {"status": "success", "active": data.active}

@router.get("/status")
async def get_autobump_status(request: Request, user=Depends(get_current_user)):
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT is_active, last_bump_at, next_bump_at 
            FROM autobump_tasks WHERE user_uid=$1
        """, user['uid'])
    
    if not row:
        return {"is_active": False}

    return {
        "is_active": row['is_active'],
        "last_bump": row['last_bump_at'],
        "next_bump": row['next_bump_at']
    }
