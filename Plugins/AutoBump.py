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

# --- ЛОГЕР В БАЗУ ---
async def log_step(pool, uid, msg, next_run_delay=None):
    """Пишет статус в БД. Если передан delay, переносит время следующего запуска."""
    try:
        # Обрезаем сообщение, чтобы влезло в базу
        safe_msg = str(msg)[:150]
        async with pool.acquire() as conn:
            if next_run_delay is not None:
                # Финальный статус с переносом времени
                await conn.execute("""
                    UPDATE autobump_tasks 
                    SET status_message=$1, last_bump_at=NOW(), 
                        next_bump_at=NOW() + interval '1 second' * $2 
                    WHERE user_uid=$3
                """, safe_msg, next_run_delay, uid)
            else:
                # Промежуточный статус (без переносом времени)
                await conn.execute("UPDATE autobump_tasks SET status_message=$1 WHERE user_uid=$2", safe_msg, uid)
    except Exception as e:
        print(f"[DB LOG ERROR] {e}")

# --- ПАРСЕРЫ ---
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

def get_tokens_debug(html: str):
    """Ищет токены и возвращает отчет о том, что нашел"""
    csrf, game_id = None, None
    log = []

    # 1. CSRF
    m = re.search(r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html)
    if m: 
        csrf = m.group(1)
        log.append("CSRF(input)")
    
    # 2. Game ID
    m = re.search(r'data-game-id=["\'](\d+)["\']', html)
    if m: 
        game_id = m.group(1)
        log.append("GID(attr)")
    
    if not game_id:
        m = re.search(r'class="[^"]*js-lot-raise"[^>]*data-game=["\'](\d+)["\']', html)
        if m: 
            game_id = m.group(1)
            log.append("GID(btn)")

    # 3. App Data (Fallback)
    if not csrf or not game_id:
        if 'data-app-data' in html:
            log.append("AppData found")
            m_app = re.search(r'data-app-data="([^"]+)"', html)
            if m_app:
                try:
                    blob = html_lib.unescape(m_app.group(1))
                    if not csrf:
                        m_c = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob)
                        if m_c: csrf = m_c.group(1); log.append("CSRF(blob)")
                    if not game_id:
                        m_g = re.search(r'"game-id"\s*:\s*(\d+)', blob)
                        if m_g: game_id = m_g.group(1); log.append("GID(blob)")
                except: log.append("AppData Parse Err")
        else:
            log.append("No AppData")

    return game_id, csrf, " ".join(log)

# --- ВОРКЕР (DIAGNOSTIC MODE) ---
async def worker(app):
    await asyncio.sleep(5)
    print(">>> [AutoBump] DIAGNOSTIC WORKER STARTED", flush=True)
    
    connector = aiohttp.TCPConnector(ssl=False)
    # Тайм-аут 30 сек на всё
    timeout = aiohttp.ClientTimeout(total=30) 

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://funpay.com",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
    }

    while True:
        try:
            if not hasattr(app.state, 'pool') or not app.state.pool:
                await asyncio.sleep(1); continue
            pool = app.state.pool
            
            # Берем ОДНУ задачу, чтобы не забивать логи
            tasks = []
            async with pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT user_uid, encrypted_golden_key, node_ids 
                    FROM autobump_tasks 
                    WHERE is_active = TRUE 
                    AND (next_bump_at IS NULL OR next_bump_at <= NOW())
                    LIMIT 1
                """)

            if not tasks:
                await asyncio.sleep(2); continue

            task = tasks[0]
            uid = task['user_uid']

            # БЛОКИРУЕМ ЗАДАЧУ (чтобы не было цикла)
            await log_step(pool, uid, "🔍 [1/6] Воркер принял задачу...", 600)

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                try:
                    # 1. Ключ
                    key = decrypt_data(task['encrypted_golden_key'])
                    cookies = {"golden_key": key}
                    
                    # 2. Ноды
                    raw_nodes = str(task['node_ids']).split(',')
                    nodes = [n.strip() for n in raw_nodes if n.strip().isdigit()]
                    if not nodes:
                        await log_step(pool, uid, "❌ Нет NodeID в настройках", 3600)
                        continue

                    # Работаем с первым лотом для теста
                    node = nodes[0]
                    url = f"https://funpay.com/lots/{node}/trade"

                    await log_step(pool, uid, f"🌐 [2/6] Заход на {url}...")

                    # 3. GET Request
                    async with session.get(url, headers=HEADERS, cookies=cookies) as resp:
                        if "login" in str(resp.url):
                            await log_step(pool, uid, "❌ Слетела авторизация (Login redirect)", 999999)
                            continue
                        
                        if resp.status == 404:
                            await log_step(pool, uid, f"❌ Лот {node} не найден (404)", 3600)
                            continue
                            
                        if resp.status != 200:
                            await log_step(pool, uid, f"❌ Ошибка HTTP {resp.status}", 600)
                            continue
                            
                        html = await resp.text()

                    # 4. Анализ HTML
                    if "Подождите" in html:
                        match = re.search(r'class="[^"]*ajax-alert-danger"[^>]*>(.*?)</div>', html, re.DOTALL)
                        msg = match.group(1).strip() if match else "Таймер"
                        sec = parse_wait_time(msg)
                        await log_step(pool, uid, f"⏳ [Stop] Таймер на странице: {msg}", sec)
                        continue

                    # 5. Парсинг
                    await log_step(pool, uid, "🧩 [3/6] Парсинг токенов...")
                    gid, csrf, debug_info = get_tokens_debug(html)
                    
                    if not gid or not csrf:
                        # Логируем, что именно не нашли
                        err = f"❌ ErrParse: GID={gid} CSRF={bool(csrf)} ({debug_info})"
                        await log_step(pool, uid, err, 1800)
                        continue

                    # 6. POST Request
                    await log_step(pool, uid, f"🚀 [4/6] Отправка POST (gid={gid})...")
                    
                    post_headers = HEADERS.copy()
                    post_headers["X-CSRF-Token"] = csrf
                    post_headers["Referer"] = url
                    
                    payload = {"game_id": gid, "node_id": node, "csrf_token": csrf}
                    
                    async with session.post("https://funpay.com/lots/raise", data=payload, cookies=cookies, headers=post_headers) as p_resp:
                        resp_text = await p_resp.text()
                        
                        # 7. Анализ ответа
                        await log_step(pool, uid, f"📩 [5/6] Ответ сервера: {p_resp.status}")
                        
                        if p_resp.status != 200:
                            await log_step(pool, uid, f"❌ POST Fail: {p_resp.status}", 600)
                            continue

                        try:
                            js = json.loads(resp_text)
                            msg = js.get("msg", "")
                            if not js.get("error"):
                                await log_step(pool, uid, f"✅ [6/6] УСПЕХ! {msg}", 14400)
                            else:
                                # Ошибка от FP
                                wait = parse_wait_time(msg)
                                if wait > 0:
                                    await log_step(pool, uid, f"⏳ FunPay: {msg}", wait)
                                else:
                                    await log_step(pool, uid, f"⚠️ FunPay Error: {msg}", 600)
                        except:
                            # Не JSON
                            if "поднято" in resp_text.lower():
                                await log_step(pool, uid, "✅ Успех (HTML)", 14400)
                            else:
                                await log_step(pool, uid, f"⚠️ Странный ответ: {resp_text[:50]}", 600)

                except asyncio.TimeoutError:
                    await log_step(pool, uid, "❌ Timeout (слишком долго)", 600)
                except Exception as e:
                    await log_step(pool, uid, f"❌ Crash: {str(e)}", 600)
                    traceback.print_exc()

            await asyncio.sleep(1)

        except Exception as ex:
            print(f"[CRITICAL] {ex}")
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
        await conn.execute("UPDATE autobump_tasks SET next_bump_at=NOW(), status_message='В очереди...' WHERE user_uid=$1", u['uid'])
    return {"status": "success"}

@router.get("/status")
async def get_stat(req: Request, u=Depends(get_plugin_user)):
    async with req.app.state.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT is_active, next_bump_at, status_message FROM autobump_tasks WHERE user_uid=$1", u['uid'])
    if not r: return {"is_active": False, "status_message": "Выключено"}
    return {"is_active": r['is_active'], "next_bump": r['next_bump_at'], "status_message": r['status_message']}
