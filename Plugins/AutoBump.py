import asyncio
import re
import html as html_lib
import random
import json
import traceback
import requests  # <-- Используем requests, как в старом боте
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autobump", tags=["AutoBump Plugin"])

class CloudBumpSettings(BaseModel):
    golden_key: str
    node_ids: list[str]
    active: bool

# --- DB ---
async def update_db(pool, uid, msg, next_delay=None):
    try:
        clean = str(msg)[:150]
        print(f"[AutoBump {uid}] {clean}", flush=True)
        async with pool.acquire() as conn:
            if next_delay is not None:
                await conn.execute("UPDATE autobump_tasks SET status_message=$1, last_bump_at=NOW(), next_bump_at=NOW()+interval '1 second'*$2 WHERE user_uid=$3", clean, next_delay, uid)
            else:
                await conn.execute("UPDATE autobump_tasks SET status_message=$1 WHERE user_uid=$2", clean, uid)
    except: pass

# --- СИНХРОННАЯ ФУНКЦИЯ (Копия логики старого бота) ---
def sync_bump_logic(key, nodes):
    # Заголовки один-в-один из bump.py
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://funpay.com"
    }
    
    session = requests.Session()
    session.cookies.set("golden_key", key, domain="funpay.com")
    session.headers.update(headers)

    final_msg = ""
    max_wait = 0
    success_count = 0

    for node in nodes:
        url = f"https://funpay.com/lots/{node}/trade"
        try:
            # 1. GET
            r = session.get(url, timeout=15)
            if "login" in r.url: return "❌ Слет сессии", 999999
            if r.status_code == 404: continue
            
            html = r.text
            
            # 2. PARSE (Все методы)
            csrf = None
            game_id = None
            
            # CSRF
            m = re.search(r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html)
            if m: csrf = m.group(1)
            
            if not csrf: # AppData fallback
                m = re.search(r'data-app-data="([^"]+)"', html)
                if m:
                    try:
                        blob = html_lib.unescape(m.group(1))
                        t = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob) or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', blob)
                        if t: csrf = t.group(1)
                    except: pass

            # Game ID
            m = re.search(r'class="[^"]*js-lot-raise"[^>]*data-game=["\'](\d+)["\']', html)
            if m: game_id = m.group(1)
            
            if not game_id:
                m = re.search(r'data-game-id=["\'](\d+)["\']', html) or re.search(r'data-game=["\'](\d+)["\']', html)
                if m: game_id = m.group(1)

            # FORCE MODE: Если нашли ID, пробуем даже без CSRF
            if not game_id:
                if "Подождите" in html:
                    return "⏳ Таймер (HTML)", 3600
                continue # Пропускаем, если вообще ничего не нашли

            # 3. POST
            p_data = {"game_id": game_id, "node_id": node}
            if csrf: p_data["csrf_token"] = csrf
            
            session.headers["Referer"] = url
            session.headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

            r_post = session.post("https://funpay.com/lots/raise", data=p_data, timeout=15)
            txt = r_post.text
            
            # Проверка ответа
            try:
                js = r_post.json()
                if not js.get("error"):
                    success_count += 1
                else:
                    msg = js.get("msg", "")
                    # Парсинг времени из ответа
                    h = re.search(r'(\d+)\s*(?:ч|h)', msg.lower())
                    m_min = re.search(r'(\d+)\s*(?:м|min)', msg.lower())
                    wait = 0
                    if h: wait += int(h.group(1)) * 3600
                    if m_min: wait += int(m_min.group(1)) * 60
                    
                    if wait > max_wait:
                        max_wait = wait
                        final_msg = f"⏳ {msg}"
            except:
                if "поднято" in txt.lower(): success_count += 1

        except Exception as e:
            return f"❌ Ошибка сети: {str(e)[:30]}", 600

    # Итог
    if max_wait > 0: return final_msg, max_wait + 120
    if success_count > 0: return f"✅ Поднято: {success_count}", 14400
    return "⚠️ Нет действий", 3600

# --- WORKER WRAPPER ---
async def worker(app):
    await asyncio.sleep(3)
    print(">>> [AutoBump] WORKER V18 (REQUESTS ENGINE) STARTED", flush=True)
    
    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(1); continue
            pool = app.state.pool
            loop = asyncio.get_running_loop()

            tasks = []
            async with pool.acquire() as conn:
                tasks = await conn.fetch("SELECT user_uid, encrypted_golden_key, node_ids FROM autobump_tasks WHERE is_active = TRUE AND (next_bump_at IS NULL OR next_bump_at <= NOW()) LIMIT 1")

            if not tasks: await asyncio.sleep(2); continue

            task = tasks[0]
            uid = task['user_uid']

            await update_db(pool, uid, "⚡ Обработка (Sync)...", 900)

            try:
                key = decrypt_data(task['encrypted_golden_key'])
                raw = str(task['node_ids']).split(',')
                nodes = [n.strip() for n in raw if n.strip().isdigit()]

                if not nodes:
                    await update_db(pool, uid, "❌ Нет NodeID", 3600)
                    continue

                # Запускаем синхронную функцию в отдельном потоке
                msg, delay = await loop.run_in_executor(None, sync_bump_logic, key, nodes)
                await update_db(pool, uid, msg, delay)

            except Exception as e:
                await update_db(pool, uid, f"⚠️ Crash: {e}", 600)

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
