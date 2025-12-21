import asyncio
import re
import html as html_lib
import json
import aiohttp
import traceback
import uuid
import sys
import os
from datetime import datetime
from typing import Dict, Any, List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

# Импортируем авторизацию, но вызывать будем вручную
from auth.guards import get_current_user 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])

# --- ЛОГИРОВАНИЕ (Абсолютный путь) ---
# Лог будет лежать прямо там, откуда запущен скрипт
LOG_FILE = os.path.join(os.getcwd(), "restock_final_debug.log")

def log_debug(msg):
    try:
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg}\n")
    except Exception as e:
        print(f"LOGGING FAILED: {e}")

# --- HELPERS ---
def count_lines(text: str):
    if not text: return 0
    return len([l for l in text.split('\n') if l.strip()])

def parse_edit_page(html: str):
    offer_id, name, secrets, csrf = None, "Без названия", "", None
    is_active, is_auto = False, False
    
    m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', html)
    if not m_oid: m_oid = re.search(r'value=["\'](\d+)["\'][^>]*name=["\']offer_id["\']', html)
    if m_oid: offer_id = m_oid.group(1)
    
    m_name = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', html)
    if m_name: name = html_lib.unescape(m_name.group(1))
    else:
        m_en = re.search(r'name=["\']fields\[summary\]\[en\]["\'][^>]*value=["\']([^"\']+)["\']', html)
        if m_en: name = html_lib.unescape(m_en.group(1))
        
    m_sec = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    if m_sec: secrets = html_lib.unescape(m_sec.group(1))

    m_csrf = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', html)
    if not m_csrf: m_csrf = re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']csrf_token["\']', html)
    if m_csrf: csrf = m_csrf.group(1)

    if 'name="active" checked' in html or "name='active' checked" in html: is_active = True
    if 'name="auto_delivery" checked' in html or "name='auto_delivery' checked" in html: is_auto = True

    return offer_id, name, secrets, csrf, is_active, is_auto

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
            try: await conn.execute("ALTER TABLE autorestock_tasks ADD COLUMN IF NOT EXISTS lots_config JSONB;")
            except: pass
            try: await conn.execute("ALTER TABLE autorestock_tasks ADD COLUMN IF NOT EXISTS check_interval INTEGER DEFAULT 7200;")
            except: pass
    except Exception as e:
        log_debug(f"DB Init Error: {e}")

async def update_status(pool, uid_obj, msg):
    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", str(msg)[:100], uid_obj)
    except: pass

# --- API ---

@router.post("/fetch_offers")
async def fetch_offers(req: Request):
    """Получение офферов"""
    try:
        body = await req.json()
        golden_key = body.get("golden_key") or body.get("GoldenKey")
        node_ids = body.get("node_ids") or body.get("NodeIds") or []
    except:
        return {"success": False, "message": "JSON Parse Error"}

    results = []
    HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    cookies = {"golden_key": golden_key}
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in node_ids:
            node = str(node).strip()
            if not node.isdigit(): continue
            try:
                async with session.get(f"https://funpay.com/lots/{node}/trade", headers=HEADERS, cookies=cookies) as resp:
                    if "login" in str(resp.url): return {"success": False, "message": "Golden Key невалиден"}
                    html = await resp.text()

                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html))
                
                if not found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=HEADERS, cookies=cookies) as r2:
                        h2 = await r2.text()
                        oid, name, _, _, _, _ = parse_edit_page(h2)
                        if oid: found_ids.add(oid)

                if not found_ids:
                    results.append({"node_id": node, "valid": False, "error": "Лоты не найдены"})
                    continue

                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies=cookies) as r_edit:
                        oid_real, name, _, _, _, _ = parse_edit_page(await r_edit.text())
                        if oid_real:
                            results.append({"node_id": node, "offer_id": oid_real, "name": name, "valid": True})
                    await asyncio.sleep(0.1)
            except Exception as e:
                results.append({"node_id": node, "valid": False, "error": str(e)[:20]})
            await asyncio.sleep(0.5)
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(req: Request):
    """
    БЕЗОПАСНЫЙ SAVE: Без Depends в аргументах.
    """
    log_debug("\n=== NEW REQUEST: /set ===")
    try:
        # 1. Проверяем пул БД
        pool = getattr(req.app.state, 'pool', None)
        if not pool:
            log_debug("FAIL: DB Pool is missing")
            return JSONResponse(status_code=200, content={"success": False, "message": "Server DB Error (No Pool)"})

        # 2. РУЧНАЯ АВТОРИЗАЦИЯ (Внутри try/except)
        # Мы убрали Depends из аргументов, чтобы поймать ошибку авторизации
        u = None
        try:
            log_debug("Checking auth...")
            u = await get_current_user(req)
            log_debug(f"Auth Success. User: {u.get('uid')}")
        except Exception as e:
            log_debug(f"AUTH FAIL: {e}")
            return JSONResponse(status_code=200, content={"success": False, "message": f"Auth Error: {e}"})

        # 3. Преобразование UID
        try:
            user_uid_obj = uuid.UUID(str(u['uid']))
        except Exception as e:
            log_debug(f"UUID ERROR: {e}")
            return JSONResponse(status_code=200, content={"success": False, "message": f"Bad UID: {e}"})

        # 4. Парсинг JSON
        try:
            body = await req.json()
            log_debug(f"JSON Body received. Keys: {len(body.keys())}")
            
            golden_key = body.get("golden_key") or body.get("GoldenKey")
            active = body.get("active") if "active" in body else body.get("Active", False)
            lots_data = body.get("lots") or body.get("Lots") or []
        except Exception as e:
            log_debug(f"JSON ERROR: {e}")
            return JSONResponse(status_code=200, content={"success": False, "message": f"JSON Error: {e}"})

        # 5. БД Init
        await ensure_table_exists(pool)

        async with pool.acquire() as conn:
            # 6. Чтение старого конфига
            existing_pools = {}
            try:
                row = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1", user_uid_obj)
                if row and row['lots_config']:
                    raw = row['lots_config']
                    loaded = json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(loaded, list):
                        for l in loaded:
                            existing_pools[str(l.get('offer_id'))] = l.get('secrets_pool', [])
            except Exception as e:
                log_debug(f"Read Config Warn: {e}")

            # 7. Новый конфиг
            final_lots = []
            for lot in lots_data:
                try:
                    oid = str(lot.get('offer_id') or lot.get('OfferId', ''))
                    nid = str(lot.get('node_id') or lot.get('NodeId', ''))
                    nm = str(lot.get('name') or lot.get('Name', 'Lot'))
                    
                    mq_val = lot.get('min_qty') if 'min_qty' in lot else lot.get('MinQty', 5)
                    try: mq = int(mq_val)
                    except: mq = 5

                    new_keys_raw = lot.get('add_secrets') or lot.get('AddSecrets') or []
                    new_keys = [str(k).strip() for k in new_keys_raw if str(k).strip()]
                    
                    if not oid: continue
                    
                    pool_keys = existing_pools.get(oid, []) + new_keys
                    
                    final_lots.append({
                        "node_id": nid, 
                        "offer_id": oid, 
                        "name": nm, 
                        "min_qty": mq, 
                        "secrets_pool": pool_keys
                    })
                except Exception as e:
                    log_debug(f"Lot parse error: {e}")

            # 8. Шифрование
            try:
                if not golden_key:
                    return JSONResponse(status_code=200, content={"success": False, "message": "Empty GoldenKey"})
                enc = encrypt_data(golden_key)
            except Exception as e:
                return JSONResponse(status_code=200, content={"success": False, "message": f"Encrypt Error: {e}"})
            
            # 9. SQL ЗАПИСЬ
            json_str = json.dumps(final_lots)
            log_debug("Executing INSERT...")
            
            try:
                await conn.execute("""
                    INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
                    VALUES ($1, $2, $3, $4::jsonb, NOW(), 'Обновлено')
                    ON CONFLICT (user_uid) DO UPDATE SET
                    encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                    is_active = EXCLUDED.is_active,
                    lots_config = EXCLUDED.lots_config,
                    status_message = 'Настройки сохранены'
                """, user_uid_obj, enc, active, json_str)
                log_debug("SQL Success!")
            except Exception as e:
                log_debug(f"SQL FAIL: {e}")
                return JSONResponse(status_code=200, content={"success": False, "message": f"DB Error: {e}"})
            
        return {"success": True, "message": "Настройки сохранены!"}

    except Exception as e:
        full_err = traceback.format_exc()
        log_debug(f"CRITICAL: {full_err}")
        return JSONResponse(status_code=200, content={"success": False, "message": f"Internal Error: {str(e)}"})

@router.get("/status")
async def get_status(req: Request):
    """Тоже безопасный get status"""
    try:
        u = await get_current_user(req)
        user_uid_obj = uuid.UUID(str(u['uid']))
        pool = req.app.state.pool
        
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1", user_uid_obj)
        
        if not r: return {"active": False, "message": "Не настроено", "lots": []}
        
        display_lots = []
        if r['lots_config']:
            try:
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
            except: pass
                
        return {"active": r['is_active'], "message": r['status_message'], "lots": display_lots}
    except Exception as e:
        return {"active": False, "message": f"Err: {str(e)}", "lots": []}

# --- WORKER ---
async def worker(app):
    await asyncio.sleep(5)
    print(">>> [AutoRestock] WORKER STARTED", flush=True)
    if hasattr(app.state, 'pool'): await ensure_table_exists(app.state.pool)

    HEADERS_GET = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    HEADERS_POST = HEADERS_GET.copy()
    HEADERS_POST["X-Requested-With"] = "XMLHttpRequest"
    
    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            
            async with app.state.pool.acquire() as conn:
                try:
                    tasks = await conn.fetch("SELECT * FROM autorestock_tasks WHERE is_active = TRUE AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')")
                except: tasks = []

            if not tasks: await asyncio.sleep(10); continue
            
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                for t in tasks:
                    try:
                        uid_val = uuid.UUID(str(t['user_uid']))
                    except: continue

                    try:
                        key = decrypt_data(t['encrypted_golden_key'])
                        raw = t['lots_config']
                        lots = json.loads(raw) if isinstance(raw, str) else raw
                        if not isinstance(lots, list): lots = []

                        is_changed = False
                        log_msg = []
                        cookies = {"golden_key": key}

                        for lot in lots:
                            pool = lot.get('secrets_pool', [])
                            if not pool: continue
                            
                            offer_id = lot['offer_id']
                            node_id = lot['node_id']
                            min_q = lot['min_qty']
                            
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=HEADERS_GET, cookies=cookies) as r:
                                html = await r.text()
                                
                            oid, _, cur_text, csrf, is_active, is_auto = parse_edit_page(html)
                            
                            if not csrf: 
                                log_msg.append(f"⚠️ {offer_id}: нет доступа")
                                continue
                            
                            if not is_auto: continue
                            
                            cur_qty = count_lines(cur_text)
                            
                            if cur_qty < min_q:
                                to_add = pool[:50]
                                remaining_pool = pool[50:]
                                
                                new_text = cur_text.strip() + "\n" + "\n".join(to_add)
                                
                                payload = {
                                    "csrf_token": csrf,
                                    "offer_id": oid,
                                    "node_id": node_id,
                                    "secrets": new_text,
                                    "auto_delivery": "on",
                                    "active": "on" if is_active else "",
                                    "save": "Сохранить"
                                }
                                if not is_active: payload.pop("active", None)
                                
                                req_headers = HEADERS_POST.copy()
                                req_headers["Referer"] = edit_url
                                
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=req_headers) as pr:
                                    if pr.status == 200:
                                        log_msg.append(f"✅ {offer_id}: +{len(to_add)}")
                                        lot['secrets_pool'] = remaining_pool
                                        is_changed = True
                                    else:
                                        log_msg.append(f"❌ {offer_id}: {pr.status}")
                            
                            await asyncio.sleep(2)

                        if is_changed:
                            async with app.state.pool.acquire() as c:
                                await c.execute("UPDATE autorestock_tasks SET lots_config=$1::jsonb WHERE user_uid=$2", json.dumps(lots), uid_val)
                        
                        status = ", ".join(log_msg) if log_msg else "✅ Проверено"
                        await update_status(app.state.pool, uid_val, status)
                        
                    except Exception as e:
                        log_debug(f"Worker Err: {e}")
                        await update_status(app.state.pool, uid_val, "Ошибка")
            await asyncio.sleep(5)
        except: await asyncio.sleep(5)
