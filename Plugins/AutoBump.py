import asyncio
import re
import html as html_lib
import logging
import random
from datetime import datetime

import aiohttp
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel

[cite_start]from auth.guards import get_current_user # Импорт из твоего проекта [cite: 125]
from utils_crypto import encrypt_data, decrypt_data # Наш новый файл

# Создаем роутер (как мини-приложение внутри приложения)
router = APIRouter(prefix="/api/plus/autobump", tags=["AutoBump Plugin"])

# --- МОДЕЛИ ---
class CloudBumpSettings(BaseModel):
    golden_key: str
    node_ids: list[str]
    active: bool

# --- ВОРКЕР (Фоновая задача) ---
async def worker(app):
    print(">>> [PLUGIN] AutoBump Worker Started (Modular)")
    
    # [cite_start]Регулярки для парсинга (как в C# AutoBumpCore [cite: 176])
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
            # Используем пул соединений из app.state (передан из server.py)
            pool = app.state.pool
            
            async with pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT user_uid, encrypted_golden_key, node_ids 
                    FROM autobump_tasks 
                    WHERE is_active = TRUE 
                    AND (next_bump_at IS NULL OR next_bump_at < NOW())
                    LIMIT 20
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

                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                            "X-Requested-With": "XMLHttpRequest",
                            "Origin": "https://funpay.com"
                        }
                        cookies = {"golden_key": golden_key}

                        for node_id in nodes:
                            # 1. GET (Получаем game_id и токен)
                            trade_url = f"https://funpay.com/lots/{node_id}/trade"
                            game_id = None
                            csrf_token = None
                            
                            async with session.get(trade_url, headers=headers, cookies=cookies) as resp:
                                if resp.status != 200: continue
                                page_html = await resp.text()

                            # Парсинг game_id
                            for pattern in RE_GAME_ID:
                                m = pattern.search(page_html)
                                if m: 
                                    game_id = m.group(1)
                                    break
                            
                            if not game_id:
                                m_app = RE_APP_DATA.search(page_html)
                                if m_app:
                                    blob = html_lib.unescape(m_app.group(1))
                                    m_g = re.search(r'"game-id"\s*:\s*(\d+)', blob)
                                    if m_g: game_id = m_g.group(1)

                            if not game_id: continue

                            # Парсинг CSRF
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

                            # 2. POST (Поднятие)
                            raise_url = "https://funpay.com/lots/raise"
                            payload = {"game_id": game_id, "node_id": node_id}
                            if csrf_token: payload["csrf_token"] = csrf_token

                            post_headers = headers.copy()
                            post_headers["Referer"] = trade_url
                            
                            async with session.post(raise_url, data=payload, headers=post_headers, cookies=cookies) as post_resp:
                                pass # Логирование по желанию

                            await asyncio.sleep(2) 

                        # Обновляем время (рандом 4-5 часов)
                        hours = random.randint(4, 5)
                        async with pool.acquire() as conn:
                            await conn.execute("UPDATE autobump_tasks SET last_bump_at=NOW(), next_bump_at=NOW() + interval '1 hour' * $1 WHERE user_uid=$2", hours, uid)

                    except Exception as e:
                        print(f"[AutoBump] Task Error {uid}: {e}")

            await asyncio.sleep(10)
        except Exception as e:
            print(f"[AutoBump] Critical Worker Error: {e}")
            await asyncio.sleep(30)

# --- API ENDPOINTS (Роуты) ---

@router.post("/set")
async def set_autobump(data: CloudBumpSettings, request: Request, user=Depends(get_current_user)):
    # Проверка подписки Plus
    async with request.app.state.pool.acquire() as conn:
        has_plus = await conn.fetchval("""
            SELECT EXISTS(SELECT 1 FROM purchases WHERE user_uid=$1 AND plan ILIKE '%plus%')
        """, user['uid'])
        
        # if not has_plus: raise HTTPException(403, "Only for Plus users") 

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
    
    if not row:
        return {"is_active": False}

    return {
        "is_active": row['is_active'],
        "last_bump": row['last_bump_at'],
        "next_bump": row['next_bump_at']
    }