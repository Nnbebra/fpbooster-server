import asyncio
import re
import html as html_lib
import random
import json
import aiohttp
import time
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

# === ЗАЩИТА ОТ СПАМА (СЕРВЕРНАЯ) ===
# Хранит время последнего запроса: { 'user_uid': timestamp }
USER_LAST_ACTION = {}

def check_rate_limit(uid: str):
    """
    Проверяет лимит запросов (1 запрос в 30 секунд).
    Возвращает (разрешено: bool, сообщение: str).
    """
    now = time.time()
    last_time = USER_LAST_ACTION.get(uid, 0)
    
    # Если прошло меньше 30 секунд
    if now - last_time < 30:
        wait_time = int(30 - (now - last_time))
        return False, f"⏳ Не спамьте! Ждите {wait_time} сек."
    
    # Обновляем время
    USER_LAST_ACTION[uid] = now
    return True, ""
# ===================================

# --- DB HELPERS ---
async def update_status(pool, uid, msg, next_delay=None, disable=False):
    try:
        clean_msg = str(msg)[:150]
        # Логируем в консоль сервера только важные события
        if "✅" in clean_msg or "⏳" in clean_msg or "⚠️" in clean_msg:
            print(f"[AutoBump {uid}] {clean_msg}", flush=True)
            
        async with pool.acquire() as conn:
            if disable:
                await conn.execute("UPDATE autobump_tasks SET status_message=$1, is_active=FALSE WHERE user_uid=$2", clean_msg, uid)
            elif next_delay is not None:
                # Джиттер 10-20 секунд (для естественности)
                jitter = random.randint(10, 20) 
                final_delay = next_delay + jitter
                
                await conn.execute(
                    "UPDATE autobump_tasks SET status_message=$1, last_bump_at=NOW(), next_bump_at=NOW() + interval '1 second' * $2 WHERE user_uid=$3", 
                    clean_msg, final_delay, uid
                )
            else:
                await conn.execute("UPDATE autobump_tasks SET status_message=$1 WHERE user_uid=$2", clean_msg, uid)
    except Exception as e:
        print(f"[AutoBump DB Error] {e}")

# --- PARSERS ---
def parse_wait_time(text: str) -> int:
    """Парсит время из текста ошибки FunPay."""
    if not text: return 0
    text = text.lower()
    
    # 1. Формат "02:59:59"
    time_match = re.search(r'(\d+):(\d+):(\d+)', text)
    if time_match:
        h, m, s = map(int, time_match.groups())
        return h * 3600 + m * 60 + s

    # 2. Формат "3 ч. 15 мин."
    h = re.search(r'(\d+)\s*(?:ч|h|hour)', text)
    m = re.search(r'(\d+)\s*(?:м|min|мин)', text)
    
    hours = int(h.group(1)) if h else 0
    minutes = int(m.group(1)) if m else 0
    
    total = (hours * 3600) + (minutes * 60)
    
    # Если цифр нет, но есть текст ожидания -> 1 час
    if total == 0 and ("подож" in text or "wait" in text): return 3600
    
    return total

def get_tokens_and_status(html: str):
    """Извлекает CSRF, GameID кнопки и текст скрытого алерта."""
    csrf, gid, alert = None, None, None
    
    # CSRF
    m = re.search(r'data-app-data="([^"]+)"', html)
    if m:
        try:
            blob = html_lib.unescape(m.group(1))
            t = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob) or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', blob)
            if t: csrf = t.group(1)
        except: pass
    if not csrf:
        patterns = [r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', r'name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']']
        for p in patterns:
            m = re.search(p, html)
            if m: csrf = m.group(1); break
            
    # КНОПКА (Ищем по классу js-lot-raise)
    btn_match = re.search(r'<button[^>]*class=["\'][^"\']*js-lot-raise[^"\']*["\'][^>]*>', html)
    if btn_match:
        btn_html = btn_match.group(0)
        g_match = re.search(r'data-game=["\'](\d+)["\']', btn_html)
        if g_match: gid = g_match.group(1)
        else:
            # Fallback
            m = re.search(r'data-game-id=["\'](\d+)["\']', html)
            if m: gid = m.group(1)

    # АЛЕРТ (site-message)
    alert_match = re.search(r'id=["\']site-message["\'][^>]*>(.*?)</div>', html, re.DOTALL)
    if alert_match:
        alert = alert_match.group(1).strip()
        
    return csrf, gid, alert

# --- WORKER ---
async def worker(app):
    await asyncio.sleep(5)
    print(">>> [AutoBump] WORKER STARTED (Final Logic)", flush=True)
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=45)
    
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://funpay.com"
    }

    while True:
        try:
            if not hasattr(app.state, 'pool') or not app.state.pool: await asyncio.sleep(2); continue
            pool = app.state.pool
            
            tasks = []
            async with pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT t.user_uid, t.encrypted_golden_key, t.node_ids 
                    FROM autobump_tasks t
                    WHERE t.is_active = TRUE 
                    AND (t.next_bump_at IS NULL OR t.next_bump_at <= NOW())
                    LIMIT 3
                """)

            if not tasks: await asyncio.sleep(3); continue

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                for task in tasks:
                    uid = task['user_uid']
                    # Ставим статус "В работе", чтобы не взять дважды
                    await update_status(pool, uid, "⚡ Работаю...", 120) 

                    try:
                        try: key = decrypt_data(task['encrypted_golden_key'])
                        except: await update_status(pool, uid, "❌ Ошибка ключа", disable=True); continue

                        cookies = {"golden_key": key}
                        raw_nodes = str(task['node_ids']).split(',')
                        nodes = [n.strip() for n in raw_nodes if n.strip().isdigit()]

                        if not nodes: await update_status(pool, uid, "❌ Нет лотов", disable=True); continue

                        final_msg = ""
                        final_delay = 0
                        success_cnt = 0
                        global_csrf = None

                        for node in nodes:
                            url = f"https://funpay.com/lots/{node}/trade"
                            get_hdrs = HEADERS.copy(); get_hdrs["Referer"] = url
                            html = ""
                            
                            # 1. Загрузка страницы
                            for attempt in range(2):
                                try:
                                    async with session.get(url, headers=get_hdrs, cookies=cookies) as resp:
                                        if "login" in str(resp.url): final_msg = "❌ Логин"; break
                                        if resp.status == 404: break 
                                        html = await resp.text(); break
                                except: await asyncio.sleep(1)
                            
                            if final_msg == "❌ Логин": 
                                await update_status(pool, uid, "❌ Сессия (STOP)", disable=True); break

                            # 2. Поиск данных
                            csrf, gid, alert_text = get_tokens_and_status(html)
                            
                            if not csrf and not global_csrf:
                                try:
                                    async with session.get("https://funpay.com/", headers=get_hdrs, cookies=cookies) as rh:
                                        c, _, _ = get_tokens_and_status(await rh.text())
                                        if c: global_csrf = c
                                except: pass
                            if not csrf and global_csrf: csrf = global_csrf

                            # 3. Логика
                            if gid:
                                # A. КНОПКА ЕСТЬ - ЖМЕМ
                                post_hdrs = HEADERS.copy()
                                post_hdrs["Referer"] = url
                                post_hdrs["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
                                if csrf: post_hdrs["X-CSRF-Token"] = csrf
                                payload = {"game_id": gid, "node_id": node}
                                if csrf: payload["csrf_token"] = csrf

                                try:
                                    async with session.post("https://funpay.com/lots/raise", data=payload, cookies=cookies, headers=post_hdrs) as pr:
                                        txt = await pr.text()
                                        try:
                                            js = json.loads(txt)
                                            if not js.get("error"): 
                                                success_cnt += 1
                                            else:
                                                msg = js.get("msg", "")
                                                w = parse_wait_time(msg)
                                                if w > 0:
                                                    if w > final_delay: final_delay = w; final_msg = f"⏳ {msg}"
                                                else: final_msg = f"⚠️ {msg[:30]}"
                                        except:
                                            if "поднято" in txt.lower(): success_cnt += 1
                                except: 
                                    final_msg = "❌ Ошибка сети"; final_delay = 60
                            
                            elif alert_text:
                                # B. КНОПКИ НЕТ - ЧИТАЕМ АЛЕРТ
                                w = parse_wait_time(alert_text)
                                if w > 0:
                                    if w > final_delay:
                                        final_delay = w
                                        h = w // 3600
                                        m = (w % 3600) // 60
                                        final_msg = f"⏳ Ждем {h}ч {m}мин"
                            
                            else:
                                # C. НИЧЕГО НЕТ - ПРОВЕРЯЕМ ЛОГИН
                                if "href=\"/account/login\"" in html or "href='/account/login'" in html:
                                    final_msg = "⚠️ Не авторизован"
                                    final_delay = 60
                                elif final_delay == 0:
                                    # Мы в системе, но таймеров нет -> Лот активен -> Ждем час
                                    final_msg = "⏳ Лот активен (1ч)"
                                    final_delay = 3600

                            await asyncio.sleep(random.uniform(1.0, 2.0))

                        # --- ИТОГИ ---
                        if "❌ Логин" in final_msg: pass 
                        elif final_delay > 0:
                            await update_status(pool, uid, final_msg, final_delay)
                        elif success_cnt > 0:
                            await update_status(pool, uid, f"✅ Поднято: {success_cnt}", 14400)
                        elif final_msg:
                            await update_status(pool, uid, final_msg, 60)
                        else:
                            await update_status(pool, uid, "⏳ Ожидание (1ч)", 3600)

                    except Exception as e:
                        traceback.print_exc()
                        await update_status(pool, uid, f"⚠️ Err: {str(e)[:50]}", 60)

            await asyncio.sleep(1)
        except: await asyncio.sleep(5)

# --- API ENDPOINTS ---
async def get_plugin_user(request: Request):
    return await get_current_user_raw(request.app, request)

@router.post("/set")
async def set_bump(data: CloudBumpSettings, req: Request, u=Depends(get_plugin_user)):
    # 🛑 СЕРВЕРНАЯ ЗАЩИТА ОТ СПАМА 🛑
    is_allowed, msg = check_rate_limit(u['uid'])
    if not is_allowed:
        # Возвращаем False, чтобы клиент показал ошибку
        return {"success": False, "message": msg}
    # -------------------------------

    async with req.app.state.pool.acquire() as conn:
        enc = encrypt_data(data.golden_key)
        ns = ",".join(data.node_ids)
        await conn.execute("""
            INSERT INTO autobump_tasks (user_uid, encrypted_golden_key, node_ids, is_active, next_bump_at, status_message) 
            VALUES ($1, $2, $3, $4, NOW(), 'Запуск...') 
            ON CONFLICT (user_uid) DO UPDATE SET 
            encrypted_golden_key=EXCLUDED.encrypted_golden_key, 
            node_ids=EXCLUDED.node_ids, 
            is_active=EXCLUDED.is_active, 
            next_bump_at=NOW(), 
            status_message='Обновлено'
        """, u['uid'], enc, ns, data.active)
    return {"status": "success"}

@router.post("/force_check")
async def force(req: Request, u=Depends(get_plugin_user)):
    # 🛑 СЕРВЕРНАЯ ЗАЩИТА ОТ СПАМА 🛑
    is_allowed, msg = check_rate_limit(u['uid'])
    if not is_allowed:
        return {"success": False, "message": msg}
    # -------------------------------

    async with req.app.state.pool.acquire() as conn:
        await conn.execute("UPDATE autobump_tasks SET next_bump_at=NOW(), status_message='В очереди...' WHERE user_uid=$1", u['uid'])
    return {"status": "success"}

@router.get("/status")
async def get_stat(req: Request, u=Depends(get_plugin_user)):
    async with req.app.state.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT is_active, next_bump_at, status_message, node_ids FROM autobump_tasks WHERE user_uid=$1", u['uid'])
    if not r: return {"is_active": False, "next_bump": None, "status_message": "Не настроено", "node_ids": []}
    
    nodes_list = [x.strip() for x in r['node_ids'].split(',') if x.strip()] if r['node_ids'] else []

    return {
        "is_active": r['is_active'], 
        "next_bump": r['next_bump_at'], 
        "status_message": r['status_message'],
        "node_ids": nodes_list
    }
