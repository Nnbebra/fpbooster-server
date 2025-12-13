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
        # Дублируем в консоль сервера для отладки
        print(f"[AutoBump] {uid}: {clean_msg}", flush=True) 
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

def extract_alert_message(html: str) -> str:
    match = re.search(r'class="[^"]*ajax-alert-danger"[^>]*>(.*?)</div>', html, re.DOTALL)
    if match: return html_lib.unescape(match.group(1)).strip()
    return ""

def get_tokens_ultimate(html: str):
    """
    Полный набор методов поиска (из C# и старого бота).
    """
    csrf, game_id = None, None
    debug_log = []

    # --- 1. CSRF ---
    # A. Input
    m = re.search(r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html)
    if m: 
        csrf = m.group(1)
        debug_log.append("csrf_input")
    
    # B. App Data / Meta / Js (Fallback)
    if not csrf:
        m_app = re.search(r'data-app-data="([^"]+)"', html)
        if m_app:
            try:
                blob = html_lib.unescape(m_app.group(1))
                t = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob) or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', blob)
                if t: 
                    csrf = t.group(1)
                    debug_log.append("csrf_blob")
            except: pass

    # --- 2. GAME ID (Все методы) ---
    
    # A. Кнопка (для лотов)
    # class="... js-lot-raise ... data-game="123"
    if not game_id:
        m = re.search(r'class="[^"]*js-lot-raise"[^>]*data-game=["\'](\d+)["\']', html)
        if m: 
            game_id = m.group(1)
            debug_log.append("gid_btn")

    # B. Атрибут data-game-id
    if not game_id:
        m = re.search(r'data-game-id=["\'](\d+)["\']', html)
        if m:
            game_id = m.group(1)
            debug_log.append("gid_attr_id")

    # C. Атрибут data-game (ОЧЕНЬ ВАЖНО ДЛЯ КАТЕГОРИЙ!)
    if not game_id:
        m = re.search(r'data-game=["\'](\d+)["\']', html)
        if m:
            game_id = m.group(1)
            debug_log.append("gid_attr_simple")

    # D. App Data
    if not game_id:
        if 'blob' in locals(): # Если blob уже декодирован выше
            t = re.search(r'"game-id"\s*:\s*(\d+)', blob)
            if t:
                game_id = t.group(1)
                debug_log.append("gid_blob")
        else:
            # Пробуем найти blob заново, если CSRF нашли в input
            m_app = re.search(r'data-app-data="([^"]+)"', html)
            if m_app:
                try:
                    blob = html_lib.unescape(m_app.group(1))
                    t = re.search(r'"game-id"\s*:\s*(\d+)', blob)
                    if t:
                        game_id = t.group(1)
                        debug_log.append("gid_blob_new")
                except: pass

    return game_id, csrf, "+".join(debug_log)

# --- ВОРКЕР ---
async def worker(app):
    await asyncio.sleep(3)
    print(">>> [AutoBump] WORKER V9 (ULTIMATE PARSER) STARTED", flush=True)
    
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=40) 

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://funpay.com"
    }

    while True:
        try:
            if not hasattr(app.state, 'pool') or not app.state.pool:
                await asyncio.sleep(1); continue
            pool = app.state.pool
            
            # Берем задачи
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
                await asyncio.sleep(2); continue

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                for task in tasks:
                    uid = task['user_uid']
                    
                    # БЛОКИРОВКА ЗАДАЧИ
                    await log_db(pool, uid, "⚡ Воркер: Старт...", 600)

                    try:
                        key = decrypt_data(task['encrypted_golden_key'])
                        cookies = {"golden_key": key}
                        raw_nodes = str(task['node_ids']).split(',')
                        nodes = [n.strip() for n in raw_nodes if n.strip().isdigit()]

                        if not nodes:
                            await log_db(pool, uid, "❌ Нет NodeID", 3600)
                            continue

                        final_status = ""
                        final_delay = 0
                        success_count = 0

                        for idx, node in enumerate(nodes):
                            await log_db(pool, uid, f"🔍 [{idx+1}/{len(nodes)}] Лот {node}...")
                            if idx > 0: await asyncio.sleep(random.uniform(1.5, 3.0))

                            url = f"https://funpay.com/lots/{node}/trade"
                            
                            # 1. GET
                            async with session.get(url, headers=HEADERS, cookies=cookies) as resp:
                                if "login" in str(resp.url):
                                    final_status = "❌ Слетела сессия"
                                    final_delay = 999999
                                    break
                                
                                if resp.status != 200:
                                    final_status = f"❌ Ошибка {resp.status}"
                                    final_delay = 600
                                    break # Прерываем, если сайт лежит

                                html = await resp.text()

                            # 2. Таймер?
                            if "Подождите" in html:
                                m_alert = re.search(r'class="[^"]*ajax-alert-danger"[^>]*>(.*?)</div>', html, re.DOTALL)
                                msg = m_alert.group(1).strip() if m_alert else "Таймер"
                                wait = parse_wait_time(msg)
                                if wait > final_delay: 
                                    final_delay = wait
                                    final_status = f"⏳ {msg}"
                                continue

                            # 3. Парсинг (ULTIMATE)
                            gid, csrf, d_src = get_tokens_ultimate(html)
                            
                            if not gid or not csrf:
                                print(f"[AutoBump] PARSE FAIL Node {node}: GID={gid} CSRF={bool(csrf)} Src={d_src}")
                                if "just a moment" in html.lower():
                                    final_status = "🛡️ Cloudflare Block"
                                    final_delay = 3600
                                    break
                                else:
                                    final_status = f"❌ Не нашел кнопки (см. консоль)"
                                    # Не прерываем, вдруг следующий лот нормальный
                                    continue

                            # 4. POST
                            post_headers = HEADERS.copy()
                            post_headers["X-CSRF-Token"] = csrf
                            post_headers["Referer"] = url
                            payload = {"game_id": gid, "node_id": node, "csrf_token": csrf}
                            
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
                                            if w > final_delay:
                                                final_delay = w
                                                final_status = f"⏳ {msg}"
                                        else:
                                            final_status = f"⚠️ FP: {msg[:25]}"
                                except:
                                    if "поднято" in txt.lower(): success_count += 1

                        # --- ИТОГ ---
                        if final_delay > 900000: # Авторизация/Блок
                            await log_db(pool, uid, final_status, final_delay)
                        elif final_delay > 0: # Таймер
                            final_delay += random.randint(120, 300)
                            h = final_delay // 3600
                            m = (final_delay % 3600) // 60
                            st = final_status if final_status else f"⏳ Ждем {h}ч {m}мин"
                            await log_db(pool, uid, st, final_delay)
                        elif success_count > 0: # Успех
                            await log_db(pool, uid, f"✅ Поднято: {success_count}", 14400)
                        elif final_status: # Ошибка
                            await log_db(pool, uid, final_status, 1800)
                        else:
                            await log_db(pool, uid, "⚠️ Нет лотов/ошибок", 3600)

                    except Exception as e:
                        print(f"[Worker Error] {uid}: {e}")
                        traceback.print_exc()
                        await log_db(pool, uid, "⚠️ Сбой воркера", 600)

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
