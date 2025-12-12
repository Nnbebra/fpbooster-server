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

# --- Helpers ---
def parse_wait_time(text: str) -> int:
    if not text: return 14400 
    text = text.lower()
    
    hours = 0
    minutes = 0
    
    h_match = re.search(r'(\d+)\s*(?:ч|h|hour|час)', text)
    if h_match: hours = int(h_match.group(1))
    
    m_match = re.search(r'(\d+)\s*(?:м|min|мин)', text)
    if m_match: minutes = int(m_match.group(1))
    
    total = (hours * 3600) + (minutes * 60)
    # Если цифр нет, но есть слово "подождите" — считаем как 1 час
    if total == 0 and ("подож" in text or "wait" in text):
        return 3600
        
    return total if total > 0 else 14400

def extract_alert_message(html_content: str) -> str:
    match = re.search(r'class="[^"]*ajax-alert-danger"[^>]*>(.*?)</div>', html_content, re.DOTALL)
    if match:
        return html_lib.unescape(match.group(1)).strip()
    return ""

def is_bot_protection(html: str) -> bool:
    """Проверяет, не вернул ли FunPay страницу защиты вместо контента"""
    html_lower = html.lower()
    if "<title>just a moment...</title>" in html_lower: return True
    if "ddos-guard" in html_lower: return True
    if "security check" in html_lower: return True
    return False

def extract_game_id_and_csrf(html_content: str):
    csrf = None
    game_id = None
    
    # 1. Пробуем data-app-data (самый надежный, но может быть экранирован)
    m_app = re.search(r'data-app-data=["\']([^"\']+)["\']', html_content)
    if m_app:
        try:
            blob = html_lib.unescape(m_app.group(1))
            # Ищем csrf
            m_csrf = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob) or \
                     re.search(r'"csrfToken"\s*:\s*"([^"]+)"', blob)
            if m_csrf: csrf = m_csrf.group(1)
            
            # Ищем game_id
            m_gid = re.search(r'"game-id"\s*:\s*(\d+)', blob)
            if m_gid: game_id = m_gid.group(1)
        except:
            pass

    # 2. Fallback: ищем в мета-тегах и инпутах (если data-app-data не сработал)
    if not csrf:
        # <input name="csrf_token" value="...">
        m = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', html_content)
        if m: csrf = m.group(1)
        
    if not game_id:
        # data-game-id="..."
        m = re.search(r'data-game-id=["\'](\d+)["\']', html_content)
        if m: game_id = m.group(1)
        else:
            # class="... js-lot-raise ..." data-game="..."
            m = re.search(r'class="[^"]*js-lot-raise"[^>]*data-game=["\'](\d+)["\']', html_content) 
            if m: game_id = m.group(1)

    return game_id, csrf

async def update_db_status(pool, uid, msg, next_bump_in=None):
    try:
        async with pool.acquire() as conn:
            if next_bump_in is not None:
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
    except Exception as e:
        print(f"[AutoBump] DB Error {uid}: {e}", flush=True)

# --- WORKER ---
async def worker(app):
    await asyncio.sleep(5) # Даем серверу стартануть
    print(">>> [AutoBump] Воркер ЗАПУЩЕН V2 (Smart Batching)", flush=True)
    
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://funpay.com",
        "Accept-Language": "ru,en;q=0.9"
    }

    while True:
        try:
            if not hasattr(app.state, 'pool') or not app.state.pool:
                await asyncio.sleep(1)
                continue

            pool = app.state.pool
            
            # Берем пользователей, которым пора (LIMIT 5 юзеров за цикл)
            tasks = []
            async with pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT user_uid, encrypted_golden_key, node_ids 
                    FROM autobump_tasks 
                    WHERE is_active = TRUE 
                    AND (next_bump_at IS NULL OR next_bump_at <= NOW())
                    ORDER BY next_bump_at ASC NULLS FIRST
                    LIMIT 5
                """)

            if not tasks:
                await asyncio.sleep(3)
                continue

            print(f"[AutoBump] Обработка {len(tasks)} пользователей...", flush=True)

            async with aiohttp.ClientSession(headers=HEADERS) as session:
                for task in tasks:
                    uid = task['user_uid']
                    try:
                        # 1. Дешифровка
                        try:
                            key = decrypt_data(task['encrypted_golden_key'])
                        except:
                            await update_db_status(pool, uid, "❌ Неверный ключ", 999999)
                            continue

                        cookies = {"golden_key": key}
                        # Чистим и валидируем ID лотов
                        raw_nodes = str(task['node_ids']).split(',')
                        nodes = [n.strip() for n in raw_nodes if n.strip().isdigit()]
                        
                        if not nodes:
                            await update_db_status(pool, uid, "❌ Нет NodeID", 3600)
                            continue

                        # === ЛОГИКА ОБРАБОТКИ ПАРТИИ ===
                        # Мы проверим все лоты, но запишем в статус самый важный результат.
                        # Приоритет: Timer > Protection > Success > ParseError
                        
                        batch_timer = 0
                        batch_success = 0
                        batch_errors = 0
                        last_error_msg = ""
                        is_blocked = False

                        # Сообщаем о начале (чтобы юзер видел, что процесс идет)
                        await update_db_status(pool, uid, f"🔄 Проверка {len(nodes)} лотов...")

                        for node_id in nodes:
                            # Небольшая пауза между лотами одного юзера
                            if len(nodes) > 1: await asyncio.sleep(random.uniform(1.5, 3.0))

                            async with session.get(f"https://funpay.com/lots/{node_id}/trade", cookies=cookies, timeout=15) as resp:
                                if resp.status == 403 or resp.status == 503:
                                    is_blocked = True
                                    break
                                html = await resp.text()

                            # А. Блокировка IP?
                            if is_bot_protection(html):
                                is_blocked = True
                                break

                            # Б. Таймер на странице?
                            alert_msg = extract_alert_message(html)
                            if alert_msg and ("подож" in alert_msg.lower() or "wait" in alert_msg.lower()):
                                sec = parse_wait_time(alert_msg)
                                if sec > batch_timer: batch_timer = sec
                                continue # Ловить тут больше нечего

                            # В. Парсинг
                            game_id, csrf = extract_game_id_and_csrf(html)
                            if not game_id or not csrf:
                                batch_errors += 1
                                last_error_msg = "Ошибка парсинга (проверьте лот)"
                                continue

                            # Г. Поднятие
                            post_headers = HEADERS.copy()
                            post_headers["X-CSRF-Token"] = csrf
                            payload = {"game_id": game_id, "node_id": node_id, "csrf_token": csrf}

                            async with session.post("https://funpay.com/lots/raise", data=payload, cookies=cookies, headers=post_headers, timeout=15) as post_resp:
                                txt = await post_resp.text()
                                try:
                                    js = json.loads(txt)
                                    err = js.get("error", False)
                                    msg = js.get("msg", "")
                                except:
                                    err = True
                                    msg = extract_alert_message(txt)

                                if not err:
                                    batch_success += 1
                                else:
                                    # Ошибка может быть таймером
                                    sec = parse_wait_time(msg)
                                    if sec > 0:
                                        if sec > batch_timer: batch_timer = sec
                                    else:
                                        batch_errors += 1
                                        last_error_msg = msg

                        # === ФИНАЛЬНОЕ РЕШЕНИЕ ПО ЮЗЕРУ ===
                        
                        if is_blocked:
                            print(f"[AutoBump] {uid} -> Blocked", flush=True)
                            # Если IP заблочен, откладываем на час, чтобы не спамить
                            await update_db_status(pool, uid, "🛡️ IP сервера заблокирован FunPay", 3600)
                        
                        elif batch_timer > 0:
                            # Если хоть один лот выдал таймер — ждем
                            print(f"[AutoBump] {uid} -> Timer {batch_timer}s", flush=True)
                            # Формируем красивое время
                            wait_h = batch_timer // 3600
                            wait_m = (batch_timer % 3600) // 60
                            msg = f"⏳ Ждем {wait_h}ч {wait_m}мин"
                            await update_db_status(pool, uid, msg, batch_timer)
                        
                        elif batch_success > 0:
                            # Успех (даже если часть лотов упала с ошибкой)
                            print(f"[AutoBump] {uid} -> Success ({batch_success})", flush=True)
                            await update_db_status(pool, uid, f"✅ Поднято лотов: {batch_success}", 14400) # 4 часа
                        
                        else:
                            # Только ошибки
                            print(f"[AutoBump] {uid} -> Fail: {last_error_msg}", flush=True)
                            await update_db_status(pool, uid, f"❌ {last_error_msg or 'Ошибка'}", 1800) # Повтор через 30 мин

                    except Exception as e:
                        print(f"[AutoBump] Worker Error {uid}: {e}", flush=True)
                        await update_db_status(pool, uid, "⚠️ Сбой воркера", 600)

            await asyncio.sleep(1)

        except Exception as global_ex:
            print(f"[AutoBump] CRITICAL: {global_ex}", flush=True)
            await asyncio.sleep(10)

# --- API ---
async def get_plugin_user(request: Request):
    return await get_current_user_raw(request.app, request)

@router.post("/set")
async def set_autobump(data: CloudBumpSettings, request: Request, user=Depends(get_plugin_user)):
    async with request.app.state.pool.acquire() as conn:
        enc_key = encrypt_data(data.golden_key)
        nodes_str = ",".join([str(n) for n in data.node_ids])
        await conn.execute("""
            INSERT INTO autobump_tasks (user_uid, encrypted_golden_key, node_ids, is_active, next_bump_at, status_message)
            VALUES ($1, $2, $3, $4, NOW(), 'Настройки сохранены')
            ON CONFLICT (user_uid) DO UPDATE SET 
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                node_ids = EXCLUDED.node_ids,
                is_active = EXCLUDED.is_active,
                next_bump_at = NOW(),
                status_message = 'Обновлено'
        """, user['uid'], enc_key, nodes_str, data.active)
    return {"status": "success"}

@router.post("/force_check")
async def force_check(request: Request, user=Depends(get_plugin_user)):
    async with request.app.state.pool.acquire() as conn:
        await conn.execute("UPDATE autobump_tasks SET next_bump_at = NOW(), status_message = 'Очередь...' WHERE user_uid = $1", user['uid'])
    return {"status": "success"}

@router.get("/status")
async def status(request: Request, user=Depends(get_plugin_user)):
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_active, last_bump_at, next_bump_at, status_message FROM autobump_tasks WHERE user_uid=$1", user['uid'])
    
    if not row: return {"is_active": False, "status_message": "Не настроено"}
    
    nb = row['next_bump_at'].isoformat() if row['next_bump_at'] else None
    lb = row['last_bump_at'].isoformat() if row['last_bump_at'] else None

    return {
        "is_active": row['is_active'],
        "last_bump": lb,
        "next_bump": nb,
        "status_message": row['status_message']
    }
