import asyncio
import re
import html as html_lib
import random
import json
import aiohttp
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autobump", tags=["AutoBump Plugin"])

class CloudBumpSettings(BaseModel):
    golden_key: str
    node_ids: list[str]
    active: bool

# --- ЛОГИКА ИЗ СТАРОГО БОТА (bump.py.txt) ---

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

def extract_alert_message(html_content: str) -> str:
    match = re.search(r'class="[^"]*ajax-alert-danger"[^>]*>(.*?)</div>', html_content, re.DOTALL)
    if match:
        return html_lib.unescape(match.group(1)).strip()
    return ""

def extract_game_id_and_csrf_legacy(html_text: str):
    """
    Портированная логика из bump.py.txt (Regex вместо JSON)
    Это работает надежнее для категорий типа 1094.
    """
    csrf = None
    game_id = None

    # 1. Поиск в data-app-data (Сначала атрибут, потом unescape, потом Regex внутри)
    m_app = re.search(r'data-app-data="([^"]+)"', html_text)
    if m_app:
        try:
            blob = html_lib.unescape(m_app.group(1))
            
            # CSRF внутри blob
            m_csrf = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob) or \
                     re.search(r'"csrfToken"\s*:\s*"([^"]+)"', blob)
            if m_csrf: csrf = m_csrf.group(1)
            
            # GameID внутри blob
            m_gid = re.search(r'"game-id"\s*:\s*(\d+)', blob)
            if m_gid: game_id = m_gid.group(1)
        except:
            pass

    # 2. Fallback методы (из старого кода)
    if not csrf:
        # <meta name="csrf-token" ...>
        m = re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', html_text)
        if m: csrf = m.group(1)
        
        # <input name="csrf_token" ...>
        if not csrf:
            m = re.search(r'<input[^>]+name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html_text)
            if m: csrf = m.group(1)

    if not game_id:
        # data-game-id="..."
        m = re.search(r'data-game-id="(\d+)"', html_text)
        if m: game_id = m.group(1)
        
        # class="... js-lot-raise ..." data-game="..." (Приоритетный метод старого бота)
        if not game_id:
            m = re.search(r'class="[^"]*js-lot-raise"[^>]*data-game="(\d+)"', html_text) 
            if m: game_id = m.group(1)
            
        # data-game="..."
        if not game_id:
            m = re.search(r'data-game="(\d+)"', html_text)
            if m: game_id = m.group(1)

    return game_id, csrf

async def update_db(pool, uid, msg, delay=None):
    try:
        async with pool.acquire() as conn:
            if delay is not None:
                final_delay = delay + random.randint(120, 300) # +2-5 мин рандома
                await conn.execute("UPDATE autobump_tasks SET status_message=$1, last_bump_at=NOW(), next_bump_at=NOW()+interval '1 second'*$2 WHERE user_uid=$3", msg, final_delay, uid)
            else:
                await conn.execute("UPDATE autobump_tasks SET status_message=$1 WHERE user_uid=$2", msg, uid)
    except Exception as e:
        print(f"[AutoBump] DB Error: {e}")

# --- WORKER ---
async def worker(app):
    await asyncio.sleep(3)
    print(">>> [AutoBump] WORKER RELOADED (Legacy Parser)", flush=True)
    
    # Заголовки точь-в-точь как в старом коде
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://funpay.com"
    }

    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(1); continue
            pool = app.state.pool
            
            async with pool.acquire() as conn:
                tasks = await conn.fetch("SELECT user_uid, encrypted_golden_key, node_ids FROM autobump_tasks WHERE is_active=TRUE AND (next_bump_at IS NULL OR next_bump_at <= NOW()) LIMIT 5")

            if not tasks: await asyncio.sleep(2); continue

            async with aiohttp.ClientSession(headers=HEADERS) as session:
                for task in tasks:
                    uid = task['user_uid']
                    try:
                        key = decrypt_data(task['encrypted_golden_key'])
                        raw_nodes = str(task['node_ids']).split(',')
                        nodes = [n.strip() for n in raw_nodes if n.strip().isdigit()]
                        
                        if not nodes:
                            await update_db(pool, uid, "❌ Нет ID лотов", 3600)
                            continue

                        cookies = {"golden_key": key}
                        
                        # Переменные для агрегации результата
                        batch_timer = 0
                        batch_success = 0
                        has_error = False
                        error_msg = ""

                        # Идем по всем лотам
                        for node in nodes:
                            await asyncio.sleep(random.uniform(1.0, 2.0)) # Пауза между запросами
                            
                            target_url = f"https://funpay.com/lots/{node}/trade"
                            
                            async with session.get(target_url, cookies=cookies, timeout=15) as resp:
                                if resp.status != 200:
                                    print(f"[AutoBump] {uid} -> HTTP {resp.status}")
                                    has_error = True
                                    error_msg = f"HTTP {resp.status}"
                                    continue
                                html = await resp.text()

                            # 1. Проверка на таймер
                            alert = extract_alert_message(html)
                            if alert and ("подож" in alert.lower() or "wait" in alert.lower()):
                                sec = parse_wait_time(alert)
                                if sec > batch_timer: batch_timer = sec
                                continue

                            # 2. Парсинг (Старый метод)
                            gid, csrf = extract_game_id_and_csrf_legacy(html)
                            
                            if not gid or not csrf:
                                print(f"[AutoBump] {uid} -> Parse Fail Node {node}")
                                has_error = True
                                error_msg = "Ошибка парсинга"
                                # Проверка на слет авторизации
                                if "login" in str(resp.url):
                                    error_msg = "Слетела сессия"
                                    break
                                continue

                            # 3. Поднятие
                            post_headers = HEADERS.copy()
                            post_headers["X-CSRF-Token"] = csrf
                            post_headers["Referer"] = target_url
                            
                            payload = {"game_id": gid, "node_id": node, "csrf_token": csrf}
                            
                            async with session.post("https://funpay.com/lots/raise", data=payload, cookies=cookies, headers=post_headers) as post_resp:
                                txt = await post_resp.text()
                                # Парсинг ответа
                                is_ok = False
                                try:
                                    js = json.loads(txt)
                                    if not js.get("error"): is_ok = True
                                    else: 
                                        srv_msg = js.get("msg", "")
                                        sec = parse_wait_time(srv_msg)
                                        if sec > batch_timer: batch_timer = sec
                                except:
                                    # Fallback если не JSON
                                    if "поднято" in txt.lower(): is_ok = True
                                
                                if is_ok: batch_success += 1

                        # --- ИТОГ ПО ЮЗЕРУ ---
                        if error_msg == "Слетела сессия":
                            await update_db(pool, uid, "❌ Слетела сессия (обновите ключ)", 999999)
                        elif batch_timer > 0:
                            # Найден таймер
                            h = batch_timer // 3600
                            m = (batch_timer % 3600) // 60
                            print(f"[AutoBump] {uid} -> Timer {h}h {m}m")
                            await update_db(pool, uid, f"⏳ Ждем {h}ч {m}мин", batch_timer)
                        elif batch_success > 0:
                            # Успех
                            print(f"[AutoBump] {uid} -> Success ({batch_success})")
                            await update_db(pool, uid, f"✅ Поднято: {batch_success}", 14400)
                        elif has_error:
                            await update_db(pool, uid, f"❌ {error_msg}", 600)
                        else:
                            # Странный кейс, но пусть будет таймер по умолчанию
                            await update_db(pool, uid, "✅ Цикл завершен", 14400)

                    except Exception as e:
                        print(f"[AutoBump] Loop Error {uid}: {e}")
                        await update_db(pool, uid, "⚠️ Сбой воркера", 600)

            await asyncio.sleep(1)
        except Exception as e:
            print(f"CRITICAL WORKER: {e}")
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
        await conn.execute("UPDATE autobump_tasks SET next_bump_at=NOW(), status_message='Проверка...' WHERE user_uid=$1", u['uid'])
    return {"status": "success"}

@router.get("/status")
async def get_stat(req: Request, u=Depends(get_plugin_user)):
    async with req.app.state.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT is_active, next_bump_at, status_message FROM autobump_tasks WHERE user_uid=$1", u['uid'])
    if not r: return {"is_active": False, "status_message": "Выключено"}
    return {"is_active": r['is_active'], "next_bump": r['next_bump_at'], "status_message": r['status_message']}
