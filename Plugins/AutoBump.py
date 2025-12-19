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

# --- ANTI-SPAM (ЧЕРЕЗ БАЗУ ДАННЫХ) ---
async def check_rate_limit(pool, uid: str):
    """Проверяет время последнего действия в БД."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT last_manual_check_at FROM autobump_tasks WHERE user_uid=$1", uid)
            if row and row['last_manual_check_at']:
                diff = (datetime.now() - row['last_manual_check_at']).total_seconds()
                if diff < 30:
                    wait_time = int(30 - diff)
                    return False, f"⏳ Сервер: ждите {wait_time} сек"
            
            # Обновляем таймер
            await conn.execute("""
                INSERT INTO autobump_tasks (user_uid, last_manual_check_at) VALUES ($1, NOW())
                ON CONFLICT (user_uid) DO UPDATE SET last_manual_check_at = NOW()
            """, uid)
        return True, ""
    except: return True, "" # Если ошибка БД - пропускаем

# --- DB HELPERS ---
async def update_status(pool, uid, msg, next_delay=None, disable=False):
    try:
        clean_msg = str(msg)[:150]
        if "✅" in clean_msg or "⏳" in clean_msg or "⚠️" in clean_msg:
            print(f"[AutoBump {uid}] {clean_msg}", flush=True)
            
        async with pool.acquire() as conn:
            if disable:
                await conn.execute("UPDATE autobump_tasks SET status_message=$1, is_active=FALSE WHERE user_uid=$2", clean_msg, uid)
            elif next_delay is not None:
                jitter = random.randint(30, 60) 
                final_delay = next_delay + jitter
                await conn.execute(
                    "UPDATE autobump_tasks SET status_message=$1, last_bump_at=NOW(), next_bump_at=NOW() + interval '1 second' * $2 WHERE user_uid=$3", 
                    clean_msg, final_delay, uid
                )
            else:
                await conn.execute("UPDATE autobump_tasks SET status_message=$1 WHERE user_uid=$2", clean_msg, uid)
    except: pass

# --- PARSERS ---
def clean_html(text: str) -> str:
    """Чистит HTML от тегов, чтобы найти '19 мин'."""
    text = html_lib.unescape(text) # Убираем &nbsp;
    text = re.sub(r'<[^>]+>', ' ', text) # Убираем теги
    return text.lower()

def parse_wait_time(text: str) -> int:
    if not text: return 0
    text = clean_html(text)
    
    # 1. Формат 02:59:59
    tm = re.search(r'(\d+):(\d+):(\d+)', text)
    if tm: return int(tm.group(1))*3600 + int(tm.group(2))*60 + int(tm.group(3))

    # 2. Текст (3 ч 19 мин)
    h = re.search(r'(\d+)\s*(?:ч|h|hour)', text)
    m = re.search(r'(\d+)\s*(?:м|min|мин)', text)
    
    total = (int(h.group(1))*3600 if h else 0) + (int(m.group(1))*60 if m else 0)
    
    if total == 0 and ("подож" in text or "wait" in text): return 3600
    return total

def get_tokens(html: str):
    """Ищет GID и CSRF."""
    csrf, gid = None, None
    
    # CSRF
    m = re.search(r'data-app-data="([^"]+)"', html)
    if m:
        try:
            js = json.loads(html_lib.unescape(m.group(1)))
            csrf = js.get("csrf-token") or js.get("csrfToken")
        except: pass
    if not csrf:
        m = re.search(r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html)
        if m: csrf = m.group(1)
            
    # GID (ID Игры/Кнопки)
    # 1. В кнопке js-lot-raise
    btn = re.search(r'class="[^"]*js-lot-raise"[^>]*data-game=["\'](\d+)["\']', html)
    if btn: gid = btn.group(1)
    
    # 2. Глобально (на случай если кнопка скрыта/изменена)
    if not gid:
        m = re.search(r'data-game-id=["\'](\d+)["\']', html) or re.search(r'data-game=["\'](\d+)["\']', html)
        if m: gid = m.group(1)

    return csrf, gid

# --- WORKER ---
async def worker(app):
    await asyncio.sleep(5)
    print(">>> [AutoBump] WORKER STARTED (Fresh Session Fix)", flush=True)
    
    # Настройки соединения (без сессии, она теперь внутри)
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=45)
    
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest"
    }

    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(2); continue
            pool = app.state.pool
            
            tasks = []
            async with pool.acquire() as conn:
                tasks = await conn.fetch("SELECT t.user_uid, t.encrypted_golden_key, t.node_ids FROM autobump_tasks t WHERE t.is_active = TRUE AND (t.next_bump_at IS NULL OR t.next_bump_at <= NOW()) LIMIT 3")

            if not tasks: await asyncio.sleep(3); continue

            for task in tasks:
                uid = task['user_uid']
                await update_status(pool, uid, "⚡ Работаю...", 120) 

                # === СОЗДАЕМ ЧИСТУЮ СЕССИЮ ДЛЯ КАЖДОГО ЗАДАНИЯ ===
                # Это имитирует перезагрузку сервера для каждого юзера
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), timeout=timeout) as session:
                    try:
                        try: key = decrypt_data(task['encrypted_golden_key'])
                        except: await update_status(pool, uid, "❌ Ошибка ключа", disable=True); continue
                        
                        raw_nodes = str(task['node_ids']).split(',')
                        nodes = [n.strip() for n in raw_nodes if n.strip().isdigit()]
                        if not nodes: await update_status(pool, uid, "❌ Нет лотов", disable=True); continue

                        cookies = {"golden_key": key}
                        final_msg = ""
                        final_delay = 0
                        success_cnt = 0
                        
                        # Глобальный CSRF
                        global_csrf = None

                        for node in nodes:
                            url = f"https://funpay.com/lots/{node}/trade"
                            get_hdrs = HEADERS.copy(); get_hdrs["Referer"] = url
                            html = ""
                            
                            # 1. Загрузка страницы
                            for _ in range(2):
                                try:
                                    async with session.get(url, headers=get_hdrs, cookies=cookies) as resp:
                                        if "login" in str(resp.url): final_msg = "❌ Логин"; break
                                        html = await resp.text(); break
                                except: await asyncio.sleep(1)
                            
                            if "❌" in final_msg: break

                            # 2. Поиск токенов
                            csrf, gid = get_tokens(html)
                            
                            if not csrf and not global_csrf:
                                # Fallback на главную
                                try:
                                    async with session.get("https://funpay.com/", headers=get_hdrs, cookies=cookies) as rh:
                                        global_csrf, _ = get_tokens(await rh.text())
                                except: pass
                            if not csrf: csrf = global_csrf

                            # 3. ПОПЫТКА ПОДНЯТИЯ (ВСЕГДА, ЕСЛИ ЕСТЬ ID)
                            # Даже если кнопки визуально нет, мы шлем запрос, чтобы получить точный таймер от API
                            if gid and csrf:
                                post_hdrs = HEADERS.copy()
                                post_hdrs["Referer"] = url
                                post_hdrs["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
                                post_hdrs["X-CSRF-Token"] = csrf
                                
                                try:
                                    async with session.post("https://funpay.com/lots/raise", data={"game_id": gid, "node_id": node, "csrf_token": csrf}, cookies=cookies, headers=post_hdrs) as pr:
                                        txt = await pr.text()
                                        try:
                                            js = json.loads(txt)
                                            # Успех
                                            if not js.get("error") and not js.get("msg"): 
                                                success_cnt += 1
                                            # Ошибка с сообщением (таймер)
                                            else:
                                                msg = js.get("msg", "")
                                                w = parse_wait_time(msg)
                                                if w > 0:
                                                    if w > final_delay: final_delay = w; final_msg = f"⏳ {msg}"
                                                else:
                                                    # Какая-то другая ошибка
                                                    final_msg = f"⚠️ {msg[:30]}"
                                        except:
                                            if "поднято" in txt.lower(): success_cnt += 1
                                except: final_msg = "❌ Сеть"
                            else:
                                # Если GID не нашли (совсем всё плохо), читаем текст страницы
                                w = parse_wait_time(html)
                                if w > 0:
                                    if w > final_delay: final_delay = w; h=w//3600; m=(w%3600)//60; final_msg = f"⏳ Ждем {h}ч {m}мин"
                                else:
                                    if "account/login" in html: final_msg = "⚠️ Не авторизован"; final_delay = 60
                                    elif final_delay == 0: final_msg = "⏳ Лот активен (1ч)"; final_delay = 3600

                            await asyncio.sleep(random.uniform(1, 2))

                        # Итоги по юзеру
                        if "❌" in final_msg: pass
                        elif final_delay > 0: await update_status(pool, uid, final_msg, final_delay)
                        elif success_cnt > 0: await update_status(pool, uid, f"✅ Поднято: {success_cnt}", 14400)
                        elif final_msg: await update_status(pool, uid, final_msg, 60)
                        else: await update_status(pool, uid, "⏳ Ожидание", 3600)

                    except Exception as e:
                        traceback.print_exc()
                        await update_status(pool, uid, "⚠️ Ошибка", 60)

            await asyncio.sleep(1)
        except: await asyncio.sleep(5)

# --- API ---
async def get_plugin_user(request: Request): return await get_current_user_raw(request.app, request)

@router.post("/set")
async def set_bump(data: CloudBumpSettings, req: Request, u=Depends(get_plugin_user)):
    ok, msg = await check_rate_limit(req.app.state.pool, u['uid'])
    if not ok: return {"success": False, "message": msg}
    async with req.app.state.pool.acquire() as conn:
        enc = encrypt_data(data.golden_key); ns = ",".join(data.node_ids)
        await conn.execute("INSERT INTO autobump_tasks (user_uid, encrypted_golden_key, node_ids, is_active, next_bump_at, status_message, last_manual_check_at) VALUES ($1, $2, $3, $4, NOW(), 'Запуск...', NOW()) ON CONFLICT (user_uid) DO UPDATE SET encrypted_golden_key=EXCLUDED.encrypted_golden_key, node_ids=EXCLUDED.node_ids, is_active=EXCLUDED.is_active, next_bump_at=NOW(), status_message='Обновлено', last_manual_check_at=NOW()", u['uid'], enc, ns, data.active)
    return {"status": "success"}

@router.post("/force_check")
async def force(req: Request, u=Depends(get_plugin_user)):
    ok, msg = await check_rate_limit(req.app.state.pool, u['uid'])
    if not ok: return {"success": False, "message": msg}
    async with req.app.state.pool.acquire() as conn:
        await conn.execute("UPDATE autobump_tasks SET next_bump_at=NOW(), status_message='В очереди...' WHERE user_uid=$1", u['uid'])
    return {"status": "success"}

@router.get("/status")
async def get_stat(req: Request, u=Depends(get_plugin_user)):
    async with req.app.state.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT is_active, next_bump_at, status_message, node_ids FROM autobump_tasks WHERE user_uid=$1", u['uid'])
    if not r: return {"is_active": False, "next_bump": None, "status_message": "Не настроено", "node_ids": []}
    return {"is_active": r['is_active'], "next_bump": r['next_bump_at'], "status_message": r['status_message'], "node_ids": [x.strip() for x in r['node_ids'].split(',') if x.strip()] if r['node_ids'] else []}
