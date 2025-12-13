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

# --- ЛОГИРОВАНИЕ ---
async def log_db(pool, uid, msg, next_delay=None):
    try:
        clean_msg = str(msg)[:150]
        print(f"[AutoBump {uid}] {clean_msg}", flush=True)
        async with pool.acquire() as conn:
            if next_delay is not None:
                await conn.execute("UPDATE autobump_tasks SET status_message=$1, last_bump_at=NOW(), next_bump_at=NOW()+interval '1 second'*$2 WHERE user_uid=$3", clean_msg, next_delay, uid)
            else:
                await conn.execute("UPDATE autobump_tasks SET status_message=$1 WHERE user_uid=$2", clean_msg, uid)
    except Exception as e:
        print(f"[DB Error] {e}")

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

def get_tokens_v11(html: str):
    """Ищет токены (V11: Added JS patterns)"""
    csrf, gid = None, None
    log = []

    # --- 1. CSRF SEARCH ---
    # A. Input
    m = re.search(r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html)
    if m: csrf = m.group(1); log.append("C:Input")
    
    # B. Meta
    if not csrf:
        m = re.search(r'name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', html)
        if m: csrf = m.group(1); log.append("C:Meta")

    # C. JS Variables (New!)
    if not csrf:
        m = re.search(r'window\._csrf\s*=\s*["\']([^"\']+)["\']', html)
        if m: csrf = m.group(1); log.append("C:Win")

    # --- 2. GAME ID SEARCH ---
    # A. Button
    m = re.search(r'class="[^"]*js-lot-raise"[^>]*data-game=["\'](\d+)["\']', html)
    if m: gid = m.group(1); log.append("G:Btn")

    # B. Attributes
    if not gid:
        m = re.search(r'data-game-id=["\'](\d+)["\']', html) or re.search(r'data-game=["\'](\d+)["\']', html)
        if m: gid = m.group(1); log.append("G:Attr")

    # --- 3. APP DATA (Deep Search) ---
    if not csrf or not gid:
        m_app = re.search(r'data-app-data="([^"]+)"', html)
        if m_app:
            try:
                blob = html_lib.unescape(m_app.group(1))
                # CSRF in Blob
                if not csrf:
                    t = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob) or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', blob)
                    if t: csrf = t.group(1); log.append("C:Blob")
                # GID in Blob
                if not gid:
                    t = re.search(r'"game-id"\s*:\s*(\d+)', blob)
                    if t: gid = t.group(1); log.append("G:Blob")
            except: pass

    # --- VALIDATION ---
    missing = []
    if not csrf: missing.append("CSRF")
    if not gid: missing.append("GID")
    
    if missing:
        return gid, csrf, f"Missing: {','.join(missing)} Found: {'+'.join(log)}"
    
    return gid, csrf, "OK"

# --- ВОРКЕР ---
async def worker(app):
    await asyncio.sleep(3)
    print(">>> [AutoBump] WORKER V11 (RETRY + JS CSRF) STARTED", flush=True)
    
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=40) 

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://funpay.com",
        "Referer": "https://funpay.com/users/"
    }

    while True:
        try:
            if not hasattr(app.state, 'pool') or not app.state.pool:
                await asyncio.sleep(1); continue
            pool = app.state.pool
            
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

            # БЛОКИРУЕМ ЗАДАЧУ
            await log_db(pool, uid, "[1/5] Старт V11...", 900)

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                try:
                    key = decrypt_data(task['encrypted_golden_key'])
                    cookies = {"golden_key": key}
                    
                    raw_nodes = str(task['node_ids']).split(',')
                    nodes = [n.strip() for n in raw_nodes if n.strip().isdigit()]

                    if not nodes:
                        await log_db(pool, uid, "❌ Нет NodeID", 3600)
                        continue

                    final_msg = ""
                    final_delay = 0
                    success_cnt = 0

                    for i, node in enumerate(nodes):
                        await log_db(pool, uid, f"[2/5] GET {node}...", None)
                        url = f"https://funpay.com/lots/{node}/trade"
                        
                        get_hdrs = HEADERS.copy()
                        del get_hdrs["Content-Type"]
                        get_hdrs["Referer"] = url

                        html = ""
                        # RETRY LOGIC FOR GET
                        for attempt in range(3):
                            try:
                                async with session.get(url, headers=get_hdrs, cookies=cookies) as resp:
                                    if "login" in str(resp.url):
                                        final_msg = "❌ Redirect to Login"; final_delay = 999999; break
                                    if resp.status != 200:
                                        if attempt == 2: final_msg = f"❌ HTTP {resp.status}"; final_delay = 600
                                        await asyncio.sleep(2)
                                        continue
                                    html = await resp.text()
                                    break # Success
                            except:
                                if attempt == 2: final_msg = "❌ GET Timeout"; final_delay = 600
                                await asyncio.sleep(2)
                        
                        if final_msg: break # Stop if failed after retries

                        # CHECK TIMER
                        if "Подождите" in html:
                            m = re.search(r'class="[^"]*ajax-alert-danger"[^>]*>(.*?)</div>', html, re.DOTALL)
                            tm = m.group(1).strip() if m else "Timer"
                            sec = parse_wait_time(tm)
                            if sec > final_delay: final_delay = sec; final_msg = f"⏳ {tm}"
                            continue

                        # PARSE V11
                        gid, csrf, debug_info = get_tokens_v11(html)
                        
                        if not gid or not csrf:
                            if "just a moment" in html.lower():
                                final_msg = "🛡️ Cloudflare"; final_delay = 3600; break
                            final_msg = f"❌ {debug_info}"; final_delay = 600
                            continue

                        # POST
                        await log_db(pool, uid, f"[4/5] POST...", None)
                        post_hdrs = HEADERS.copy()
                        post_hdrs["X-CSRF-Token"] = csrf
                        post_hdrs["Referer"] = url
                        payload = {"game_id": gid, "node_id": node, "csrf_token": csrf}

                        try:
                            async with session.post("https://funpay.com/lots/raise", data=payload, cookies=cookies, headers=post_hdrs) as p_resp:
                                txt = await p_resp.text()
                                try:
                                    js = json.loads(txt)
                                    if not js.get("error"):
                                        success_cnt += 1
                                    else:
                                        msg = js.get("msg", "")
                                        w = parse_wait_time(msg)
                                        if w > 0:
                                            if w > final_delay: final_delay = w; final_msg = f"⏳ {msg}"
                                        else:
                                            final_msg = f"⚠️ FP: {msg[:20]}"
                                except:
                                    if "поднято" in txt.lower(): success_cnt += 1
                        except:
                            final_msg = "❌ POST Timeout"; final_delay = 600; break

                        await asyncio.sleep(1)

                    # --- FINAL STATUS ---
                    if final_delay > 900000:
                        await log_db(pool, uid, final_msg, final_delay)
                    elif final_delay > 0:
                        final_delay += random.randint(100, 300)
                        msg = final_msg or "⏳ Ожидание"
                        await log_db(pool, uid, msg, final_delay)
                    elif success_cnt > 0:
                        await log_db(pool, uid, f"✅ Поднято: {success_cnt}", 14400)
                    elif final_msg:
                        await log_db(pool, uid, final_msg, 1800)
                    else:
                        await log_db(pool, uid, "⚠️ Нет результата", 3600)

                except Exception as e:
                    traceback.print_exc()
                    err_text = str(e)[:90]
                    await log_db(pool, uid, f"⚠️ CRASH: {err_text}", 600)

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
