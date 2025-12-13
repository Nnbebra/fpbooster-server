import asyncio
import re
import html as html_lib
import random
import json
import aiohttp
import traceback
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autobump", tags=["AutoBump Plugin"])

class CloudBumpSettings(BaseModel):
    golden_key: str
    node_ids: list[str]
    active: bool

# --- ЛОГИКА ПАРСИНГА (ПОРТ ИЗ CsrfUtils.cs) ---

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
    match = re.search(r'class="[^"]*ajax-alert-danger"[^>]*>(.*?)</div>', html, re.DOTALL)
    if match: return html_lib.unescape(match.group(1)).strip()
    return ""

def get_tokens_ultimate(html_text: str):
    """
    Мощный парсер, объединяющий логику C# CsrfUtils.cs и старых скриптов.
    """
    csrf = None
    game_id = None

    # --- 1. ПОИСК CSRF (6 методов из CsrfUtils.cs) ---
    patterns_csrf = [
        r'data-app-data="([^"]+)"',
        r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
        r'<input[^>]+name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']',
        r'window\.__NUXT__[^;]+["\']csrfToken["\']\s*:\s*["\']([^"\']+)["\']',
        r'data-csrf(?:-token)?=["\']([^"\']+)["\']',
        r"window\._csrf\s*=\s*['\"]([^'\"]+)['\"]"
    ]

    for p in patterns_csrf:
        m = re.search(p, html_text)
        if m:
            if "data-app-data" in p:
                try:
                    blob = html_lib.unescape(m.group(1))
                    t = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob) or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', blob)
                    if t: 
                        csrf = t.group(1)
                        break
                except: pass
            else:
                csrf = m.group(1)
                break

    # --- 2. ПОИСК GAME ID (4 метода) ---
    # A. Кнопка поднятия (class="... data-game="...")
    if not game_id:
        m = re.search(r'class="[^"]*js-lot-raise"[^>]*data-game=["\'](\d+)["\']', html_text)
        if m: game_id = m.group(1)

    # B. Атрибут data-game-id
    if not game_id:
        m = re.search(r'data-game-id=["\'](\d+)["\']', html_text)
        if m: game_id = m.group(1)

    # C. Атрибут data-game
    if not game_id:
        m = re.search(r'data-game=["\'](\d+)["\']', html_text)
        if m: game_id = m.group(1)

    # D. App Data (game-id)
    if not game_id:
        m_app = re.search(r'data-app-data="([^"]+)"', html_text)
        if m_app:
            try:
                blob = html_lib.unescape(m_app.group(1))
                t = re.search(r'"game-id"\s*:\s*(\d+)', blob)
                if t: game_id = t.group(1)
            except: pass

    return game_id, csrf

async def update_db_status(pool, uid, msg, next_run_seconds=None):
    try:
        safe_msg = str(msg)[:150]
        print(f"[DB LOG] User {uid}: {safe_msg}") # Дублируем в консоль сервера
        async with pool.acquire() as conn:
            if next_run_seconds is not None:
                await conn.execute("UPDATE autobump_tasks SET status_message=$1, last_bump_at=NOW(), next_bump_at=NOW()+interval '1 second'*$2 WHERE user_uid=$3", safe_msg, next_run_seconds, uid)
            else:
                await conn.execute("UPDATE autobump_tasks SET status_message=$1 WHERE user_uid=$2", safe_msg, uid)
    except Exception as e:
        print(f"[DB Error] {e}")

# --- ВОРКЕР ---
async def worker(app):
    await asyncio.sleep(5)
    print(">>> [AutoBump] WORKER ULTIMATE STARTED", flush=True)
    
    # Отключаем SSL, увеличиваем таймаут
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=40) 

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://funpay.com",
        "Referer": "https://funpay.com/users/", # Базовый реферер
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
    }

    while True:
        try:
            if not hasattr(app.state, 'pool') or not app.state.pool:
                await asyncio.sleep(1)
                continue
            pool = app.state.pool
            
            tasks = []
            async with pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT user_uid, encrypted_golden_key, node_ids 
                    FROM autobump_tasks 
                    WHERE is_active = TRUE 
                    AND (next_bump_at IS NULL OR next_bump_at <= NOW())
                    LIMIT 2
                """)

            if not tasks:
                await asyncio.sleep(2)
                continue

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                for task in tasks:
                    uid = task['user_uid']
                    
                    # 1. БЛОКИРОВКА ЗАДАЧИ НА 15 МИНУТ (чтобы не было цикла)
                    await update_db_status(pool, uid, "⚡ Воркер взял задачу...", 900)

                    try:
                        key = decrypt_data(task['encrypted_golden_key'])
                        cookies = {"golden_key": key}
                        raw_nodes = str(task['node_ids']).split(',')
                        nodes = [n.strip() for n in raw_nodes if n.strip().isdigit()]

                        if not nodes:
                            await update_db_status(pool, uid, "❌ Нет NodeID", 3600)
                            continue

                        final_wait = 0
                        success_count = 0
                        errors = []

                        for i, node in enumerate(nodes):
                            await update_db_status(pool, uid, f"🔍 Лот {node}...", None)
                            if i > 0: await asyncio.sleep(random.uniform(2.0, 4.0))

                            url = f"https://funpay.com/lots/{node}/trade"
                            
                            # GET (с заголовком Referer!)
                            get_headers = HEADERS.copy()
                            get_headers["Referer"] = url
                            del get_headers["Content-Type"] # Для GET не нужен

                            try:
                                async with session.get(url, headers=get_headers, cookies=cookies) as resp:
                                    if "login" in str(resp.url):
                                        errors.append("AUTH_LOST")
                                        break
                                    if resp.status != 200:
                                        errors.append(f"HTTP {resp.status}")
                                        continue
                                    html = await resp.text()
                            except:
                                errors.append("Timeout GET")
                                continue

                            # Check Timer
                            alert = extract_alert_message(html)
                            if "подож" in alert.lower() or "wait" in alert.lower():
                                sec = parse_wait_time(alert)
                                if sec > final_wait: final_wait = sec
                                continue

                            # Parse (ULTIMATE METHOD)
                            gid, csrf = get_tokens_ultimate(html)
                            if not gid or not csrf:
                                errors.append(f"ErrParse {node}")
                                continue

                            # POST
                            post_headers = HEADERS.copy()
                            post_headers["X-CSRF-Token"] = csrf
                            post_headers["Referer"] = url
                            
                            payload = {"game_id": gid, "node_id": node, "csrf_token": csrf}
                            
                            try:
                                async with session.post("https://funpay.com/lots/raise", data=payload, cookies=cookies, headers=post_headers) as p_resp:
                                    txt = await p_resp.text()
                                    try:
                                        js = json.loads(txt)
                                        if not js.get("error"):
                                            success_count += 1
                                        else:
                                            msg = js.get("msg", "")
                                            w = parse_wait_time(msg)
                                            if w > 0:
                                                if w > final_wait: final_wait = w
                                            else:
                                                errors.append(f"FP: {msg[:15]}")
                                    except:
                                        if "поднято" in txt.lower(): success_count += 1
                                        else:
                                            errors.append(f"BadResp: {p_resp.status}")
                            except:
                                errors.append("Timeout POST")
                                continue

                        # ИТОГИ
                        if "AUTH_LOST" in errors:
                            await update_db_status(pool, uid, "❌ Слетела сессия", 999999)
                        elif final_wait > 0:
                            # Рандом +3..5 мин
                            final_wait += random.randint(180, 300)
                            h = final_wait // 3600
                            m = (final_wait % 3600) // 60
                            await update_db_status(pool, uid, f"⏳ Ждем {h}ч {m}мин", final_wait)
                        elif success_count > 0:
                            await update_db_status(pool, uid, f"✅ Поднято: {success_count}", 14400)
                        elif errors:
                            await update_db_status(pool, uid, f"⚠️ {errors[0]}", 1800)
                        else:
                            await update_db_status(pool, uid, "⚠️ Нет действий", 3600)

                    except Exception as e:
                        print(f"[Worker Task Error] {uid}: {e}")
                        traceback.print_exc()
                        await update_db_status(pool, uid, "⚠️ Сбой воркера (лог)", 600)

            await asyncio.sleep(1)

        except Exception as ex:
            print(f"[CRITICAL WORKER LOOP] {ex}")
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
