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
    """Пишет статус в БД и дублирует в консоль сервера"""
    try:
        clean_msg = str(msg)[:150]
        print(f"[AutoBump] User {uid}: {msg}", flush=True) # Лог в консоль сервера
        async with pool.acquire() as conn:
            if next_delay is not None:
                # Сдвигаем время следующего запуска
                await conn.execute("UPDATE autobump_tasks SET status_message=$1, last_bump_at=NOW(), next_bump_at=NOW()+interval '1 second'*$2 WHERE user_uid=$3", clean_msg, next_delay, uid)
            else:
                # Просто обновляем текст
                await conn.execute("UPDATE autobump_tasks SET status_message=$1 WHERE user_uid=$2", clean_msg, uid)
    except Exception as e:
        print(f"[AutoBump] CRITICAL DB ERROR: {e}", flush=True)

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
    """Ищет токены и возвращает отладочную инфу"""
    csrf, game_id = None, None
    found_in = []

    # CSRF
    m = re.search(r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html)
    if m: 
        csrf = m.group(1)
        found_in.append("csrf_input")
    
    # GameID (Button)
    m = re.search(r'class="[^"]*js-lot-raise"[^>]*data-game=["\'](\d+)["\']', html)
    if m: 
        game_id = m.group(1)
        found_in.append("gid_btn")

    # GameID (Attribute)
    if not game_id:
        m = re.search(r'data-game-id=["\'](\d+)["\']', html)
        if m:
            game_id = m.group(1)
            found_in.append("gid_attr")

    # App Data (Fallback)
    if not csrf or not game_id:
        m_app = re.search(r'data-app-data="([^"]+)"', html)
        if m_app:
            try:
                blob = html_lib.unescape(m_app.group(1))
                if not csrf:
                    t = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob) or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', blob)
                    if t: 
                        csrf = t.group(1)
                        found_in.append("csrf_blob")
                if not game_id:
                    t = re.search(r'"game-id"\s*:\s*(\d+)', blob)
                    if t:
                        game_id = t.group(1)
                        found_in.append("gid_blob")
            except: pass

    return game_id, csrf, "+".join(found_in)

# --- ВОРКЕР ---
async def worker(app):
    await asyncio.sleep(3)
    print(">>> [AutoBump] WORKER V8 (ULTIMATE DEBUG) STARTED", flush=True)
    
    # Отключаем SSL, ставим таймаут 40с
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
                    
                    # 1. СРАЗУ БЛОКИРУЕМ ЗАДАЧУ (на 10 минут)
                    # Это предотвращает бесконечный цикл "взял-упал-взял"
                    await log_db(pool, uid, "⚡ Воркер: Старт...", 600)

                    try:
                        # Дешифровка
                        try:
                            key = decrypt_data(task['encrypted_golden_key'])
                        except:
                            await log_db(pool, uid, "❌ Ошибка ключа (пересохраните)", 999999)
                            continue

                        cookies = {"golden_key": key}
                        
                        # Парсинг списка нод
                        raw_nodes = str(task['node_ids']).split(',')
                        nodes = [n.strip() for n in raw_nodes if n.strip().isdigit()]
                        if not nodes:
                            await log_db(pool, uid, "❌ Нет NodeID", 3600)
                            continue

                        # --- ЦИКЛ ПО ЛОТАМ ---
                        success_count = 0
                        final_status_msg = ""
                        final_delay = 0

                        for idx, node in enumerate(nodes):
                            # Лог прогресса
                            await log_db(pool, uid, f"🔍 [{idx+1}/{len(nodes)}] Лот {node}...")
                            if idx > 0: await asyncio.sleep(random.uniform(1.5, 3.0))

                            url = f"https://funpay.com/lots/{node}/trade"
                            
                            # A. GET
                            async with session.get(url, headers=HEADERS, cookies=cookies) as resp:
                                if "login" in str(resp.url):
                                    final_status_msg = "❌ Слетела авторизация"
                                    final_delay = 999999
                                    break # Прерываем всё
                                
                                if resp.status == 404:
                                    # Лот удален, идем к следующему
                                    continue 
                                    
                                if resp.status != 200:
                                    final_status_msg = f"❌ HTTP {resp.status}"
                                    final_delay = 600
                                    break

                                html = await resp.text()

                            # B. Проверка страницы
                            if "Подождите" in html:
                                # Найден таймер
                                m_alert = re.search(r'class="[^"]*ajax-alert-danger"[^>]*>(.*?)</div>', html, re.DOTALL)
                                alert = m_alert.group(1).strip() if m_alert else "Таймер"
                                sec = parse_wait_time(alert)
                                if sec > final_delay: 
                                    final_delay = sec
                                    final_status_msg = f"⏳ {alert}"
                                continue

                            # C. Парсинг токенов
                            gid, csrf, debug_src = get_tokens_debug(html)
                            if not gid or not csrf:
                                # ЛОГИРУЕМ ОШИБКУ ПОДРОБНО В КОНСОЛЬ
                                print(f"[AutoBump] PARSE ERROR Node {node}: GID={gid} CSRF={bool(csrf)} Src={debug_src}")
                                # Проверка на Cloudflare
                                if "just a moment" in html.lower():
                                    final_status_msg = "🛡️ Cloudflare Block"
                                    final_delay = 3600
                                    break
                                else:
                                    final_status_msg = f"❌ ErrParse (см. консоль)"
                                    final_delay = 600
                                continue

                            # D. POST (Поднятие)
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
                                        wait = parse_wait_time(msg)
                                        if wait > 0:
                                            if wait > final_delay:
                                                final_delay = wait
                                                final_status_msg = f"⏳ {msg}"
                                        else:
                                            final_status_msg = f"⚠️ FP: {msg[:30]}"
                                            if final_delay == 0: final_delay = 600
                                except:
                                    if "поднято" in txt.lower(): success_count += 1

                        # --- ИТОГИ ---
                        if final_delay > 900000: # Слет авторизации
                            await log_db(pool, uid, final_status_msg, final_delay)
                        
                        elif final_delay > 0: # Таймер
                            # Добавляем 2-5 минут рандома
                            final_delay += random.randint(120, 300) 
                            h = final_delay // 3600
                            m = (final_delay % 3600) // 60
                            msg = final_status_msg or f"⏳ Ждем {h}ч {m}мин"
                            await log_db(pool, uid, msg, final_delay)
                        
                        elif success_count > 0: # Успех
                            await log_db(pool, uid, f"✅ Поднято: {success_count}", 14400) # 4 часа
                        
                        elif final_status_msg: # Ошибка
                            await log_db(pool, uid, final_status_msg, 1800)
                        
                        else: # Ничего не произошло
                            await log_db(pool, uid, "⚠️ Нет активных лотов", 3600)

                    except Exception as e:
                        print(f"[AutoBump] TASK FAILED {uid}: {e}")
                        traceback.print_exc()
                        await log_db(pool, uid, "⚠️ Сбой воркера (см. консоль)", 600)

            await asyncio.sleep(1)

        except Exception as ex:
            print(f"[AutoBump] CRITICAL LOOP: {ex}")
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
