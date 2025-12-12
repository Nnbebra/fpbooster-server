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

# --- Helpers ---
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
    return html_lib.unescape(match.group(1)).strip() if match else ""

def detect_page_state(html: str, url: str) -> str:
    """Определяет, что за страницу вернул FunPay"""
    html_lower = html.lower()
    
    # 1. Проверка на логин
    if "login" in str(url) or "action=\"/login\"" in html_lower or "вохранить" in html_lower or "войти" in html_lower:
        if "user-link-dropdown" not in html_lower: # Если нет меню юзера, значит точно разлогин
            return "LOGIN_REQUIRED"

    # 2. Проверка на защиту
    if "<title>just a moment...</title>" in html_lower or "ddos-guard" in html_lower:
        return "BLOCKED"

    # 3. Проверка на 404/Ошибку
    if "страница не найдена" in html_lower or "page not found" in html_lower:
        return "NOT_FOUND"

    return "OK"

def extract_data(html: str):
    csrf, game_id = None, None
    m_app = re.search(r'data-app-data=["\']([^"\']+)["\']', html)
    if m_app:
        try:
            blob = html_lib.unescape(m_app.group(1))
            m_c = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob) or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', blob)
            if m_c: csrf = m_c.group(1)
            m_g = re.search(r'"game-id"\s*:\s*(\d+)', blob)
            if m_g: game_id = m_g.group(1)
        except: pass
    
    if not csrf:
        m = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', html)
        if m: csrf = m.group(1)
    if not game_id:
        m = re.search(r'data-game-id=["\'](\d+)["\']', html)
        if m: game_id = m.group(1)
        else:
            m = re.search(r'class="[^"]*js-lot-raise"[^>]*data-game=["\'](\d+)["\']', html)
            if m: game_id = m.group(1)
            
    return game_id, csrf

async def update_db(pool, uid, msg, delay=None):
    try:
        async with pool.acquire() as conn:
            if delay is not None:
                final_delay = delay + random.randint(60, 180)
                await conn.execute("UPDATE autobump_tasks SET status_message=$1, last_bump_at=NOW(), next_bump_at=NOW()+interval '1 second'*$2 WHERE user_uid=$3", msg, final_delay, uid)
            else:
                await conn.execute("UPDATE autobump_tasks SET status_message=$1 WHERE user_uid=$2", msg, uid)
    except Exception as e:
        print(f"[AutoBump] DB Error: {e}")

async def worker(app):
    await asyncio.sleep(3)
    print(">>> [AutoBump] DIAGNOSTIC WORKER STARTED", flush=True)
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "X-Requested-With": "XMLHttpRequest"}

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
                        nodes = [n.strip() for n in str(task['node_ids']).split(',') if n.strip().isdigit()]
                        if not nodes:
                            await update_db(pool, uid, "❌ Нет ID лотов", 3600)
                            continue

                        # ДИАГНОСТИКА: Берем первый лот
                        node = nodes[0]
                        cookies = {"golden_key": key}
                        
                        await update_db(pool, uid, f"🔍 Анализ лота {node}...")

                        # Пробуем и chips (валюта) и lots (предметы), так как ID может быть любым
                        target_url = f"https://funpay.com/lots/{node}/trade"
                        
                        async with session.get(target_url, cookies=cookies, timeout=15, allow_redirects=True) as resp:
                            final_url = str(resp.url)
                            html = await resp.text()
                            
                            # 1. Анализ состояния страницы
                            state = detect_page_state(html, final_url)
                            
                            if state == "LOGIN_REQUIRED":
                                print(f"[AutoBump] {uid} -> Login Required")
                                await update_db(pool, uid, "❌ Слетела сессия (обновите GoldenKey)", 999999)
                                continue
                            
                            if state == "BLOCKED":
                                print(f"[AutoBump] {uid} -> Cloudflare Block")
                                await update_db(pool, uid, "🛡️ IP заблокирован FunPay", 3600)
                                continue
                                
                            if state == "NOT_FOUND":
                                print(f"[AutoBump] {uid} -> 404 Not Found")
                                await update_db(pool, uid, f"❌ Лот {node} не существует/удален", 3600)
                                continue

                            # 2. Проверка таймера
                            alert = extract_alert_message(html)
                            if alert and ("подож" in alert.lower() or "wait" in alert.lower()):
                                sec = parse_wait_time(alert)
                                await update_db(pool, uid, f"⏳ {alert}", sec)
                                continue

                            # 3. Парсинг
                            gid, csrf = extract_data(html)
                            if not gid or not csrf:
                                # Если это категория (например 1094), там нет game-id
                                if "chips" in final_url or "lots" in final_url:
                                    print(f"[AutoBump] {uid} -> No buttons found on page")
                                    await update_db(pool, uid, f"❌ Не лот (ID {node} это категория?)", 3600)
                                else:
                                    await update_db(pool, uid, "❌ Ошибка структуры HTML", 1800)
                                continue

                            # 4. Поднятие
                            post_headers = HEADERS.copy()
                            post_headers["X-CSRF-Token"] = csrf
                            # Важно: Referer должен совпадать
                            post_headers["Referer"] = final_url 
                            
                            async with session.post("https://funpay.com/lots/raise", data={"game_id": gid, "node_id": node, "csrf_token": csrf}, cookies=cookies, headers=post_headers) as post_resp:
                                res = await post_resp.json()
                                if not res.get("error"):
                                    await update_db(pool, uid, "✅ Успешно поднято", 14400)
                                else:
                                    msg = res.get("msg", "")
                                    await update_db(pool, uid, f"⏳ {msg}", parse_wait_time(msg))

                    except Exception as e:
                        print(f"[AutoBump] Error {uid}: {e}")
                        await update_db(pool, uid, "⚠️ Сбой системы", 600)

            await asyncio.sleep(1)
        except Exception as e:
            print(f"CRITICAL: {e}")
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
