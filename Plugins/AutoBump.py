import asyncio
import re
import html as html_lib
import random
import json
import aiohttp
import traceback
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autobump", tags=["AutoBump Plugin"])

class CloudBumpSettings(BaseModel):
    golden_key: str
    node_ids: list[str]
    active: bool

# --- МАКСИМАЛЬНО АГРЕССИВНЫЙ ПАРСИНГ ---

def parse_wait_time(text: str) -> int:
    if not text: return 14400 
    text = text.lower()
    h = re.search(r'(\d+)\s*(?:ч|h|hour)', text)
    m = re.search(r'(\d+)\s*(?:м|min|мин)', text)
    hours = int(h.group(1)) if h else 0
    minutes = int(m.group(1)) if m else 0
    total = (hours * 3600) + (minutes * 60)
    if total == 0 and ("подож" in text or "wait" in text): return 3600
    return total if total > 0 else 14400

def extract_alert_message(html: str) -> str:
    # Ищет сообщения в красных рамках (ошибки, таймеры)
    match = re.search(r'class="[^"]*ajax-alert-danger"[^>]*>(.*?)</div>', html, re.DOTALL)
    if match: return html_lib.unescape(match.group(1)).strip()
    return ""

def extract_tokens_aggressive(html: str):
    """
    Ищет CSRF и GameID везде, где только можно.
    """
    csrf = None
    game_id = None

    # 1. Декодируем data-app-data (там часто спрятано всё)
    m_app = re.search(r'data-app-data="([^"]+)"', html)
    blob = ""
    if m_app:
        try:
            blob = html_lib.unescape(m_app.group(1))
        except: pass

    # --- ПОИСК CSRF ---
    # В blob
    if blob:
        m = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob) or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', blob)
        if m: csrf = m.group(1)
    
    # В мета-тегах/инпутах (если не нашли в blob)
    if not csrf:
        m = re.search(r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html)
        if m: csrf = m.group(1)
    if not csrf:
        m = re.search(r'name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', html)
        if m: csrf = m.group(1)

    # --- ПОИСК GAME ID ---
    # 1. Приоритет: Кнопка поднятия (самый надежный для лотов)
    m = re.search(r'class="btn[^"]*js-lot-raise"[^>]*data-game="(\d+)"', html)
    if m: game_id = m.group(1)

    # 2. В blob (для категорий типа 1094 часто тут)
    if not game_id and blob:
        m = re.search(r'"game-id"\s*:\s*(\d+)', blob)
        if m: game_id = m.group(1)

    # 3. Атрибуты data-game-id
    if not game_id:
        m = re.search(r'data-game-id="(\d+)"', html) or re.search(r'data-game="(\d+)"', html)
        if m: game_id = m.group(1)

    return game_id, csrf

async def update_db(pool, uid, msg, delay=None):
    try:
        async with pool.acquire() as conn:
            if delay is not None:
                # Добавляем рандом 2-5 минут
                final_delay = delay + random.randint(120, 300)
                await conn.execute("UPDATE autobump_tasks SET status_message=$1, last_bump_at=NOW(), next_bump_at=NOW()+interval '1 second'*$2 WHERE user_uid=$3", msg, final_delay, uid)
            else:
                await conn.execute("UPDATE autobump_tasks SET status_message=$1 WHERE user_uid=$2", msg, uid)
    except Exception as e:
        print(f"[AutoBump] DB Update Error: {e}")

# --- WORKER ---
async def worker(app):
    await asyncio.sleep(5) # Ждем старта БД
    print(">>> [AutoBump] WORKER STARTED (v3 Stable)", flush=True)
    
    # Настройки соединения: отключаем SSL проверку и ставим таймаут
    # Это лечит "бесконечную проверку" при зависании
    TIMEOUT = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(ssl=False)

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://funpay.com",
        "Accept": "application/json, text/javascript, */*; q=0.01"
    }

    while True:
        try:
            if not hasattr(app.state, 'pool') or not app.state.pool:
                await asyncio.sleep(1)
                continue
            pool = app.state.pool
            
            # Берем задачи
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
                await asyncio.sleep(2)
                continue

            # Создаем сессию с таймаутом!
            async with aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT, connector=aiohttp.TCPConnector(ssl=False)) as session:
                for task in tasks:
                    uid = task['user_uid']
                    
                    # СРАЗУ меняем статус, чтобы видно было, что воркер взял задачу
                    await update_db(pool, uid, "⚡ Воркер: Запуск...") 

                    try:
                        key = decrypt_data(task['encrypted_golden_key'])
                        cookies = {"golden_key": key}
                        
                        raw_nodes = str(task['node_ids']).split(',')
                        nodes = [n.strip() for n in raw_nodes if n.strip().isdigit()]
                        
                        if not nodes:
                            await update_db(pool, uid, "❌ Нет NodeID", 3600)
                            continue

                        # Логика обработки пачки
                        batch_timer = 0
                        batch_success = 0
                        errors = []

                        for node in nodes:
                            # Пауза между лотами (анти-спам)
                            if len(nodes) > 1: await asyncio.sleep(random.uniform(1.5, 3.0))
                            
                            target_url = f"https://funpay.com/lots/{node}/trade"
                            
                            async with session.get(target_url, cookies=cookies) as resp:
                                if resp.status == 404:
                                    errors.append(f"Лот {node} не найден")
                                    continue
                                if resp.status == 403 or resp.status == 503:
                                    errors.append("Cloudflare/DDOS Guard")
                                    break
                                
                                # Проверка на редирект (слет сессии)
                                if "login" in str(resp.url):
                                    errors.append("AUTH_LOST")
                                    break

                                html = await resp.text()

                            # 1. Проверка таймера на странице
                            alert = extract_alert_message(html)
                            if alert and ("подож" in alert.lower() or "wait" in alert.lower()):
                                sec = parse_wait_time(alert)
                                if sec > batch_timer: batch_timer = sec
                                continue

                            # 2. Парсинг токенов
                            gid, csrf = extract_tokens_aggressive(html)
                            if not gid or not csrf:
                                print(f"[AutoBump] Parse Fail for {node}. GameID: {gid}, CSRF: {bool(csrf)}")
                                errors.append(f"Ошибка парсинга {node}")
                                continue

                            # 3. Отправка запроса
                            post_headers = HEADERS.copy()
                            post_headers["X-CSRF-Token"] = csrf
                            post_headers["Referer"] = target_url
                            
                            payload = {"game_id": gid, "node_id": node, "csrf_token": csrf}
                            
                            async with session.post("https://funpay.com/lots/raise", data=payload, cookies=cookies, headers=post_headers) as post_resp:
                                txt = await post_resp.text()
                                try:
                                    js = json.loads(txt)
                                    if not js.get("error"):
                                        batch_success += 1
                                    else:
                                        # Ошибка от сервера (часто это таймер)
                                        msg = js.get("msg", "")
                                        sec = parse_wait_time(msg)
                                        if sec > 0:
                                            if sec > batch_timer: batch_timer = sec
                                        else:
                                            errors.append(f"FP Error: {msg}")
                                except:
                                    pass

                        # --- ИТОГОВЫЙ СТАТУС ---
                        if "AUTH_LOST" in errors:
                            await update_db(pool, uid, "❌ Слетела авторизация (обновите ключ)", 999999)
                        elif "Cloudflare/DDOS Guard" in errors:
                            await update_db(pool, uid, "🛡️ Блок IP (Cloudflare)", 3600)
                        elif batch_timer > 0:
                            h = batch_timer // 3600
                            m = (batch_timer % 3600) // 60
                            await update_db(pool, uid, f"⏳ Ждем {h}ч {m}мин", batch_timer)
                        elif batch_success > 0:
                            await update_db(pool, uid, f"✅ Поднято: {batch_success}", 14400)
                        elif errors:
                            # Берем первую ошибку для отображения
                            err_msg = errors[0]
                            await update_db(pool, uid, f"⚠️ {err_msg}", 1800)
                        else:
                            # Если ничего не произошло (например, 0 лотов)
                            await update_db(pool, uid, "⚠️ Нет активных лотов", 3600)

                    except Exception as e:
                        print(f"[AutoBump] Task Error {uid}: {e}")
                        traceback.print_exc()
                        await update_db(pool, uid, "⚠️ Сбой воркера (см. консоль)", 600)

            await asyncio.sleep(1)

        except Exception as global_ex:
            print(f"[AutoBump] CRITICAL WORKER LOOP ERROR: {global_ex}")
            await asyncio.sleep(5)

# --- API ---
async def get_plugin_user(request: Request):
    return await get_current_user_raw(request.app, request)

@router.post("/set")
async def set_bump(data: CloudBumpSettings, req: Request, u=Depends(get_plugin_user)):
    async with req.app.state.pool.acquire() as conn:
        enc = encrypt_data(data.golden_key)
        ns = ",".join(data.node_ids)
        await conn.execute("INSERT INTO autobump_tasks (user_uid, encrypted_golden_key, node_ids, is_active, next_bump_at, status_message) VALUES ($1, $2, $3, $4, NOW(), 'Ожидание...') ON CONFLICT (user_uid) DO UPDATE SET encrypted_golden_key=EXCLUDED.encrypted_golden_key, node_ids=EXCLUDED.node_ids, is_active=EXCLUDED.is_active, next_bump_at=NOW(), status_message='Обновлено'", u['uid'], enc, ns, data.active)
    return {"status": "success"}

@router.post("/force_check")
async def force(req: Request, u=Depends(get_plugin_user)):
    async with req.app.state.pool.acquire() as conn:
        # Ставим NOW(), чтобы воркер подхватил задачу немедленно
        await conn.execute("UPDATE autobump_tasks SET next_bump_at=NOW(), status_message='Очередь...' WHERE user_uid=$1", u['uid'])
    return {"status": "success"}

@router.get("/status")
async def get_stat(req: Request, u=Depends(get_plugin_user)):
    async with req.app.state.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT is_active, next_bump_at, status_message FROM autobump_tasks WHERE user_uid=$1", u['uid'])
    if not r: return {"is_active": False, "status_message": "Выключено"}
    return {"is_active": r['is_active'], "next_bump": r['next_bump_at'], "status_message": r['status_message']}
