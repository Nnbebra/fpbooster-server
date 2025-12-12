import asyncio
import re
import html as html_lib
import random
import json
from datetime import datetime, timedelta

import aiohttp
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autobump", tags=["AutoBump Plugin"])

# --- API Models ---
class CloudBumpSettings(BaseModel):
    golden_key: str
    node_ids: list[str]
    active: bool

# --- Логика парсинга (Ported from bump.py.txt) ---

def parse_wait_time(text: str) -> int:
    """
    Парсит сообщение вида 'Подождите 3 ч. 15 мин.' или 'Подождите 4 часа'.
    Возвращает секунды.
    """
    if not text: return 14400 # Дефолт 4 часа
    text = text.lower()
    
    hours = 0
    minutes = 0
    
    # Регулярки для часов и минут
    h_match = re.search(r'(\d+)\s*(?:ч|h|hour|час)', text)
    if h_match: hours = int(h_match.group(1))
    
    m_match = re.search(r'(\d+)\s*(?:м|min|мин)', text)
    if m_match: minutes = int(m_match.group(1))
    
    total = (hours * 3600) + (minutes * 60)
    
    if total == 0 and ("подож" in text or "wait" in text):
        return 3600 # Если не распарсилось, но есть слово 'подождите' — 1 час
        
    return total

def extract_game_id_and_csrf(html_content: str):
    """
    Извлекает GameID и CSRF всеми способами из bump.py.txt
    """
    csrf = None
    game_id = None
    
    # 1. Поиск в data-app-data (Самый надежный)
    m_app = re.search(r'data-app-data="([^"]+)"', html_content)
    if m_app:
        blob = html_lib.unescape(m_app.group(1))
        # CSRF
        m_csrf = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob) or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', blob)
        if m_csrf: csrf = m_csrf.group(1)
        # GameID
        m_gid = re.search(r'"game-id"\s*:\s*(\d+)', blob)
        if m_gid: game_id = m_gid.group(1)

    # 2. Fallback методы (из bump.py.txt)
    if not csrf:
        m = re.search(r'<input[^>]+name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html_content)
        if m: csrf = m.group(1)
        
    if not game_id:
        m = re.search(r'class="btn[^"]*js-lot-raise"[^>]*data-game="(\d+)"', html_content) # Приоритет
        if m: game_id = m.group(1)
        else:
            m = re.search(r'data-game-id="(\d+)"', html_content)
            if m: game_id = m.group(1)

    return game_id, csrf

def extract_alert_message(html_content: str) -> str:
    """Ищет сообщения об ошибках или таймерах"""
    # <div id="site-message" class="ajax-alert ajax-alert-danger" ...>Подождите 4 часа.</div>
    match = re.search(r'class="[^"]*ajax-alert-danger"[^>]*>(.*?)</div>', html_content, re.DOTALL)
    if match:
        return html_lib.unescape(match.group(1)).strip()
    return ""

async def update_status(pool, uid, msg, next_bump_in=None):
    """Пишет статус в БД. Если передан next_bump_in (сек), обновляет таймер."""
    async with pool.acquire() as conn:
        if next_bump_in is not None:
            # === УМНЫЙ ТАЙМЕР ===
            # Добавляем 2-5 минут (120-300 сек) для имитации человека
            jitter = random.randint(120, 300)
            final_delay = next_bump_in + jitter
            
            await conn.execute("""
                UPDATE autobump_tasks 
                SET status_message = $1, last_bump_at = NOW(),
                    next_bump_at = NOW() + interval '1 second' * $2
                WHERE user_uid = $3
            """, msg, final_delay, uid)
        else:
            await conn.execute("UPDATE autobump_tasks SET status_message = $1 WHERE user_uid = $2", msg, uid)

# --- WORKER ---

async def worker(app):
    print(">>> [Server] Cloud AutoBump Worker Started")
    # Используем одну сессию на пачку запросов для оптимизации SSL рукопожатий
    # Заголовки как в твоем CsrfUtils.cs / bump.py
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://funpay.com",
        "Accept-Language": "ru,en;q=0.9"
    }

    while True:
        try:
            pool = app.state.pool
            
            # Берем 20 задач, у которых время пришло (или еще не задано)
            async with pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT user_uid, encrypted_golden_key, node_ids 
                    FROM autobump_tasks 
                    WHERE is_active = TRUE 
                    AND (next_bump_at IS NULL OR next_bump_at <= NOW())
                    ORDER BY next_bump_at ASC NULLS FIRST
                    LIMIT 20
                """)

            if not tasks:
                await asyncio.sleep(5)
                continue

            async with aiohttp.ClientSession(headers=HEADERS) as session:
                for task in tasks:
                    uid = task['user_uid']
                    try:
                        key = decrypt_data(task['encrypted_golden_key'])
                        cookies = {"golden_key": key}
                        
                        # Парсим ноды (разделитель запятая)
                        raw_nodes = task['node_ids'].split(',')
                        nodes = [n.strip() for n in raw_nodes if n.strip()]
                        
                        if not nodes:
                            await update_status(pool, uid, "❌ Нет NodeID", 3600)
                            continue

                        # Для проверки берем ПЕРВУЮ ноду из списка. 
                        # (Обычно, если таймер висит на одной, он висит на всех для аккаунта)
                        target_node = nodes[0]
                        
                        await update_status(pool, uid, "🔄 Работаю...")

                        # 1. GET Trade Page
                        async with session.get(f"https://funpay.com/lots/{target_node}/trade", cookies=cookies, timeout=20) as resp:
                            if resp.status != 200:
                                await update_status(pool, uid, f"Ошибка доступа ({resp.status})", 600)
                                continue
                            html = await resp.text()

                        # 2. Сразу чекаем наличие сообщения о таймере в HTML (без отправки POST)
                        alert_msg = extract_alert_message(html)
                        if alert_msg and ("подож" in alert_msg.lower() or "wait" in alert_msg.lower()):
                            wait_sec = parse_wait_time(alert_msg)
                            await update_status(pool, uid, f"⏳ {alert_msg}", wait_sec)
                            continue

                        # 3. Парсим токены
                        game_id, csrf = extract_game_id_and_csrf(html)
                        
                        if not game_id or not csrf:
                            await update_status(pool, uid, "❌ Ошибка парсинга", 1800)
                            continue

                        # 4. POST Raise
                        # Добавляем CSRF в заголовки (важно!)
                        post_headers = HEADERS.copy()
                        post_headers["X-CSRF-Token"] = csrf
                        
                        payload = {
                            "game_id": game_id,
                            "node_id": target_node,
                            "csrf_token": csrf
                        }

                        async with session.post("https://funpay.com/lots/raise", data=payload, cookies=cookies, headers=post_headers) as post_resp:
                            txt = await post_resp.text()
                            
                            # Пробуем JSON
                            try:
                                js = json.loads(txt)
                                msg = js.get("msg", "")
                                error = js.get("error", False) # может быть int или bool
                            except:
                                msg = extract_alert_message(txt) or txt[:50]
                                error = True

                            if not error:
                                # УСПЕХ -> ставим таймер на 4 часа (14400 сек)
                                await update_status(pool, uid, "✅ Успешно поднято", 14400)
                            else:
                                # ОШИБКА -> парсим время
                                wait_sec = parse_wait_time(msg)
                                if wait_sec > 0:
                                    await update_status(pool, uid, f"⏳ {msg}", wait_sec)
                                else:
                                    await update_status(pool, uid, f"⚠️ {msg}", 3600)

                    except Exception as e:
                        print(f"[Worker] Error uid {uid}: {e}")
                        await update_status(pool, uid, "Сбой воркера", 600)

            await asyncio.sleep(1) # Короткая пауза между пачками

        except Exception as e:
            print(f"[Worker] CRITICAL: {e}")
            await asyncio.sleep(10)

# --- API ENDPOINTS ---

async def get_plugin_user(request: Request):
    return await get_current_user_raw(request.app, request)

@router.post("/set")
async def set_autobump(data: CloudBumpSettings, request: Request, user=Depends(get_plugin_user)):
    async with request.app.state.pool.acquire() as conn:
        enc_key = encrypt_data(data.golden_key)
        # Собираем ноды в строку
        nodes_str = ",".join([str(n) for n in data.node_ids])
        
        # Upsert: обновляем запись или создаем новую
        await conn.execute("""
            INSERT INTO autobump_tasks (user_uid, encrypted_golden_key, node_ids, is_active, next_bump_at, status_message)
            VALUES ($1, $2, $3, $4, NOW(), 'Настройки сохранены')
            ON CONFLICT (user_uid) DO UPDATE SET 
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                node_ids = EXCLUDED.node_ids,
                is_active = EXCLUDED.is_active,
                next_bump_at = NOW(), -- Сброс таймера при сохранении
                status_message = 'Обновлено'
        """, user['uid'], enc_key, nodes_str, data.active)
        
    return {"status": "success"}

@router.post("/force_check")
async def force_check(request: Request, user=Depends(get_plugin_user)):
    """Сбрасывает таймер на 'сейчас', заставляя воркер обработать юзера вне очереди"""
    async with request.app.state.pool.acquire() as conn:
        await conn.execute("""
            UPDATE autobump_tasks 
            SET next_bump_at = NOW(), status_message = 'Очередь на проверку...' 
            WHERE user_uid = $1
        """, user['uid'])
    return {"status": "success"}

@router.get("/status")
async def status(request: Request, user=Depends(get_plugin_user)):
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_active, last_bump_at, next_bump_at, status_message FROM autobump_tasks WHERE user_uid=$1", user['uid'])
    
    if not row: return {"is_active": False, "status_message": "Не настроено"}
    
    return {
        "is_active": row['is_active'],
        "last_bump": row['last_bump_at'],
        "next_bump": row['next_bump_at'],
        "status_message": row['status_message']
    }
