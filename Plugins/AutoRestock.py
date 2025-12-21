import asyncio
import re
import html
import json
import aiohttp
import traceback
import uuid
import sys
import os
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

# Используем try-except для импортов, чтобы сервер не падал, 
# даже если структура папок отличается
try:
    from auth.guards import get_current_user
    from utils_crypto import encrypt_data, decrypt_data
except ImportError:
    # Заглушки, чтобы сервер мог запуститься и показать ошибку в логах
    def get_current_user(*args, **kwargs): pass
    def encrypt_data(s): return s
    def decrypt_data(s): return s

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])

# Глобальный флаг, чтобы не плодить воркеров
WORKER_RUNNING = False

# --- ЛОГГЕР ---
def log(msg):
    """Пишет лог в консоль сервера"""
    t = datetime.now().strftime("%H:%M:%S")
    print(f"[{t}] [AutoRestock] {msg}", flush=True)

# --- HELPERS ---
def count_lines(text: str):
    if not text: return 0
    return len([l for l in text.split('\n') if l.strip()])

def parse_edit_page(page_html: str):
    """
    Парсит HTML страницы редактирования.
    Возвращает: offer_id, secrets, csrf, is_active, is_auto
    """
    offer_id = None
    secrets = ""
    csrf = None
    is_active = False
    is_auto = False
    
    # 1. Offer ID
    m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', page_html)
    if not m_oid: m_oid = re.search(r'value=["\'](\d+)["\'][^>]*name=["\']offer_id["\']', page_html)
    if m_oid: offer_id = m_oid.group(1)
    
    # 2. Secrets (textarea)
    m_sec = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', page_html, re.DOTALL)
    if m_sec: secrets = html.unescape(m_sec.group(1))

    # 3. CSRF
    m_csrf = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', page_html)
    if not m_csrf: m_csrf = re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']csrf_token["\']', page_html)
    if m_csrf: csrf = m_csrf.group(1)

    # 4. Checkboxes
    if re.search(r'name=["\']active["\'][^>]*checked', page_html): is_active = True
    if re.search(r'name=["\']auto_delivery["\'][^>]*checked', page_html): is_auto = True

    return offer_id, secrets, csrf, is_active, is_auto

async def ensure_table_exists(pool):
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS autorestock_tasks (
                    user_uid UUID PRIMARY KEY,
                    encrypted_golden_key TEXT,
                    is_active BOOLEAN DEFAULT FALSE,
                    check_interval INTEGER DEFAULT 7200,
                    lots_config JSONB,
                    status_message TEXT,
                    last_check_at TIMESTAMP WITHOUT TIME ZONE
                );
            """)
            # Миграции (безопасно)
            try: await conn.execute("ALTER TABLE autorestock_tasks ADD COLUMN IF NOT EXISTS lots_config JSONB;")
            except: pass
            try: await conn.execute("ALTER TABLE autorestock_tasks ADD COLUMN IF NOT EXISTS check_interval INTEGER DEFAULT 7200;")
            except: pass
    except: pass

async def update_status(pool, uid_obj, msg):
    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2::uuid", str(msg)[:100], uid_obj)
    except: pass

# --- ФОНОВЫЙ ВОРКЕР ---
async def background_worker(pool):
    """Основной цикл проверки лотов"""
    global WORKER_RUNNING
    log("Воркер запущен.")
    await ensure_table_exists(pool)

    # Заголовки
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    HEADERS_AJAX = HEADERS.copy()
    HEADERS_AJAX["X-Requested-With"] = "XMLHttpRequest"

    while True:
        try:
            # 1. Получаем задачи (активные + время вышло)
            tasks = []
            try:
                async with pool.acquire() as conn:
                    # Проверяем задачи, которые не проверялись 2 часа
                    tasks = await conn.fetch("""
                        SELECT * FROM autorestock_tasks 
                        WHERE is_active = TRUE 
                        AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')
                    """)
            except Exception as e:
                log(f"DB Fetch Error: {e}")
                await asyncio.sleep(10)
                continue

            if not tasks:
                await asyncio.sleep(5)
                continue

            # 2. Выполняем задачи
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                for t in tasks:
                    try:
                        uid_val = t['user_uid']
                        key = decrypt_data(t['encrypted_golden_key'])
                        cookies = {"golden_key": key}
                        
                        raw_conf = t['lots_config']
                        lots = json.loads(raw_conf) if isinstance(raw_conf, str) else raw_conf
                        if not isinstance(lots, list): lots = []

                        is_changed = False
                        logs = []

                        for lot in lots:
                            # Пропускаем, если в базе нет ключей для этого лота
                            pool_keys = lot.get('secrets_pool', [])
                            if not pool_keys: continue 

                            offer_id = lot.get('offer_id')
                            node_id = lot.get('node_id')
                            try: min_q = int(lot.get('min_qty', 5))
                            except: min_q = 5

                            # A) Загрузка страницы
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=HEADERS, cookies=cookies) as r:
                                page_html = await r.text()

                            # B) Парсинг
                            real_oid, secrets_text, csrf, is_active, is_auto = parse_edit_page(page_html)

                            if not csrf:
                                logs.append(f"⚠️ {offer_id} Access Denied")
                                continue
                            
                            # C) Проверка галочки (как просил пользователь)
                            if not is_auto:
                                # Галочка выключена — пропускаем лот
                                continue

                            # D) Проверка количества
                            cur_qty = count_lines(secrets_text)
                            
                            if cur_qty < min_q:
                                # E) Доливаем
                                to_add = pool_keys[:50]
                                remaining_pool = pool_keys[50:]
                                
                                new_text = secrets_text.strip() + "\n" + "\n".join(to_add)
                                new_text = new_text.strip()
                                
                                # F) Сохраняем
                                payload = {
                                    "csrf_token": csrf,
                                    "offer_id": real_oid,
                                    "node_id": node_id,
                                    "secrets": new_text,
                                    "auto_delivery": "on", # Подтверждаем галочку
                                    "active": "on" if is_active else "",
                                    "save": "Сохранить"
                                }
                                if not is_active: payload.pop("active", None)
                                
                                req_h = HEADERS_AJAX.copy()
                                req_h["Referer"] = edit_url
                                
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=req_h) as pr:
                                    if pr.status == 200:
                                        logs.append(f"✅ {offer_id}: +{len(to_add)}")
                                        lot['secrets_pool'] = remaining_pool
                                        is_changed = True
                                    else:
                                        logs.append(f"❌ {offer_id}: {pr.status}")
                            
                            await asyncio.sleep(2)

                        # Если заливали - сохраняем остатки в базу
                        if is_changed:
                            async with app.state.pool.acquire() as c:
                                await c.execute("UPDATE autorestock_tasks SET lots_config=$1::jsonb WHERE user_uid=$2::uuid", json.dumps(lots), uid_val)
                        
                        # Обновляем статус
                        status_txt = ", ".join(logs) if logs else f"✅ Проверено {datetime.now().strftime('%H:%M')}"
                        await update_status(pool, uid_val, status_txt)

                    except Exception as e:
                        log(f"Task Error: {e}")
                        await update_status(pool, uid_val, "Ошибка")
            
            await asyncio.sleep(5)
        except Exception as e:
            log(f"Loop Error: {e}")
            await asyncio.sleep(10)

# --- API ---

@router.post("/fetch_offers")
async def fetch_offers(req: Request):
    """Получение офферов"""
    try:
        body = await req.json()
        golden_key = body.get("golden_key") or body.get("GoldenKey")
        node_ids = body.get("node_ids") or body.get("NodeIds") or []
    except: return {"success": False, "message": "JSON Bad"}

    results = []
    HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    cookies = {"golden_key": golden_key}
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in node_ids:
            node = str(node).strip()
            if not node.isdigit(): continue
            try:
                async with session.get(f"https://funpay.com/lots/{node}/trade", headers=HEADERS, cookies=cookies) as resp:
                    if "login" in str(resp.url): return {"success": False, "message": "Bad Key"}
                    html_txt = await resp.text()

                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html_txt))
                if not found_ids:
                    # Fallback
                    async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=HEADERS, cookies=cookies) as r2:
                        h2 = await r2.text()
                        m = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', h2)
                        if m: found_ids.add(m.group(1))

                if not found_ids:
                    results.append({"node_id": node, "valid": False, "error": "Empty"})
                    continue

                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies=cookies) as r_edit:
                        ht = await r_edit.text()
                        nm_m = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', ht)
                        nm = html.unescape(nm_m.group(1)) if nm_m else "Item"
                        results.append({"node_id": node, "offer_id": oid, "name": nm, "valid": True})
                    await asyncio.sleep(0.1)
            except Exception as e:
                results.append({"node_id": node, "valid": False, "error": str(e)[:20]})
            await asyncio.sleep(0.5)
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(req: Request):
    """Сохранение настроек и старт воркера"""
    global WORKER_RUNNING
    try:
        pool = getattr(req.app.state, 'pool', None)
        if not pool: return JSONResponse(content={"success": False, "message": "DB Error"}, status_code=200)

        # 1. Авторизация
        try:
            u = await get_current_user(req.app, req)
            uid_obj = uuid.UUID(str(u['uid']))
        except Exception as e:
            return JSONResponse(content={"success": False, "message": f"Auth: {e}"}, status_code=200)

        # 2. Данные
        try:
            body = await req.json()
            golden_key = body.get("golden_key") or body.get("GoldenKey")
            active = body.get("active") if "active" in body else body.get("Active", False)
            lots_data = body.get("lots") or body.get("Lots") or []
        except: return JSONResponse(content={"success": False, "message": "JSON Bad"}, status_code=200)

        # 3. БД
        await ensure_table_exists(pool)

        async with pool.acquire() as conn:
            # Читаем старое
            existing_pools = {}
            try:
                row = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1::uuid", uid_obj)
                if row and row['lots_config']:
                    raw = row['lots_config']
                    loaded = json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(loaded, list):
                        for l in loaded:
                            existing_pools[str(l.get('offer_id'))] = l.get('secrets_pool', [])
            except: pass

            # Формируем новое
            final_lots = []
            for lot in lots_data:
                oid = str(lot.get('offer_id') or lot.get('OfferId', ''))
                nid = str(lot.get('node_id') or lot.get('NodeId', ''))
                nm = str(lot.get('name') or lot.get('Name', 'Lot'))
                try: mq = int(lot.get('min_qty') or lot.get('MinQty', 5))
                except: mq = 5
                
                new_keys = [str(k).strip() for k in (lot.get('add_secrets') or lot.get('AddSecrets') or []) if str(k).strip()]
                
                if not oid: continue
                
                pool_keys = existing_pools.get(oid, []) + new_keys
                
                final_lots.append({
                    "node_id": nid, "offer_id": oid, "name": nm, "min_qty": mq, "secrets_pool": pool_keys
                })

            # Запись
            enc = encrypt_data(golden_key)
            json_str = json.dumps(final_lots)
            
            await conn.execute("""
                INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
                VALUES ($1::uuid, $2, $3, $4::jsonb, NULL, 'В очереди...')
                ON CONFLICT (user_uid) DO UPDATE SET
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                is_active = EXCLUDED.is_active,
                lots_config = EXCLUDED.lots_config,
                status_message = 'Обновлено',
                last_check_at = NULL
            """, uid_obj, enc, active, json_str)

        # 4. ЗАПУСК ВОРКЕРА (если еще не запущен)
        if not WORKER_RUNNING:
            asyncio.create_task(background_worker(pool))
            WORKER_RUNNING = True
            
        return {"success": True, "message": "Сохранено"}

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(content={"success": False, "message": f"Err: {str(e)}"}, status_code=200)

@router.get("/status")
async def get_status(req: Request):
    try:
        u = await get_current_user(req.app, req)
        uid_obj = uuid.UUID(str(u['uid']))
        pool = req.app.state.pool
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1::uuid", uid_obj)
        
        if not r: return {"active": False, "message": "Не настроено", "lots": []}
        
        display_lots = []
        if r['lots_config']:
            raw = r['lots_config']
            loaded = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(loaded, list):
                for l in loaded:
                    display_lots.append({
                        "node_id": l.get('node_id'),
                        "offer_id": l.get('offer_id'),
                        "name": l.get('name', 'Лот'),
                        "min_qty": l.get('min_qty'),
                        "keys_in_db": len(l.get('secrets_pool', []))
                    })
        return {"active": r['is_active'], "message": r['status_message'], "lots": display_lots}
    except Exception as e:
        return {"active": False, "message": f"Err: {str(e)}", "lots": []}
