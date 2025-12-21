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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

# ВАЖНО: Мы НЕ импортируем auth или utils здесь в начале, 
# чтобы не было ошибки 502 Bad Gateway при старте сервера.

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])

# Глобальный флаг работы воркера
WORKER_TASK_STARTED = False

# --- ЛОГИРОВАНИЕ ---
LOG_FILE = os.path.join(os.getcwd(), "restock_final_debug.log")

def log_debug(msg):
    try:
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg}\n")
        print(f"[AutoRestock] {msg}")
    except:
        pass

# --- HELPERS ---
def count_lines(text: str):
    if not text: return 0
    return len([l for l in text.split('\n') if l.strip()])

def parse_edit_page(html: str):
    """Парсит страницу редактирования лота"""
    offer_id, secrets, csrf = None, "", None
    is_active, is_auto = False, False
    
    m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', html)
    if not m_oid: m_oid = re.search(r'value=["\'](\d+)["\'][^>]*name=["\']offer_id["\']', html)
    if m_oid: offer_id = m_oid.group(1)
    
    m_sec = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    if m_sec: secrets = html_lib.unescape(m_sec.group(1))

    m_csrf = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', html)
    if not m_csrf: m_csrf = re.search(r'value=["\']([^"\']+)["\']', html)
    if m_csrf: csrf = m_csrf.group(1)

    if re.search(r'name=["\']active["\'][^>]*checked', html): is_active = True
    if re.search(r'name=["\']auto_delivery["\'][^>]*checked', html): is_auto = True

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
    except: pass

async def update_status(pool, uid_obj, msg):
    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2::uuid", str(msg)[:100], uid_obj)
    except: pass

# --- BACKGROUND WORKER ---
async def background_worker(pool):
    """Фоновая задача, которая проверяет FunPay"""
    from utils_crypto import decrypt_data
    log_debug("Воркер запущен в фоновом режиме.")
    
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    POST_HEADERS = HEADERS.copy()
    POST_HEADERS["X-Requested-With"] = "XMLHttpRequest"

    while True:
        try:
            tasks = []
            async with pool.acquire() as conn:
                # Берем задачи раз в 2 часа
                tasks = await conn.fetch("""
                    SELECT * FROM autorestock_tasks 
                    WHERE is_active = TRUE 
                    AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')
                """)

            if not tasks:
                await asyncio.sleep(10)
                continue

            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                for t in tasks:
                    uid = t['user_uid']
                    try:
                        key = decrypt_data(t['encrypted_golden_key'])
                        cookies = {"golden_key": key}
                        lots_conf = json.loads(t['lots_config']) if isinstance(t['lots_config'], str) else t['lots_config']
                        
                        is_any_changed = False
                        results_log = []

                        for lot in lots_conf:
                            pool_keys = lot.get('secrets_pool', [])
                            if not pool_keys: continue 

                            offer_id = lot['offer_id']
                            min_qty = int(lot.get('min_qty', 5))
                            
                            # 1. Заходим на страницу редактирования
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=HEADERS, cookies=cookies) as r:
                                html_text = await r.text()
                            
                            oid, current_secrets, csrf, active_state, auto_state = parse_edit_page(html_text)

                            if not csrf:
                                results_log.append(f"⚠️ {offer_id}: Нет доступа")
                                continue

                            # 2. Проверяем автовыдачу
                            if not auto_state:
                                results_log.append(f"⏸ {offer_id}: Автовыдача ВЫКЛ")
                                continue

                            # 3. Считаем строки
                            current_lines_count = count_lines(current_secrets)

                            if current_lines_count < min_qty:
                                # Доливаем товар
                                to_add = pool_keys[:50] # Берем порцию
                                remaining = pool_keys[50:]
                                
                                new_secrets = current_secrets.strip() + "\n" + "\n".join(to_add)
                                
                                # 4. Сохраняем
                                payload = {
                                    "csrf_token": csrf, "offer_id": oid, "node_id": lot['node_id'],
                                    "secrets": new_secrets, "auto_delivery": "on",
                                    "active": "on" if active_state else "", "save": "Сохранить"
                                }
                                if not active_state: payload.pop("active")

                                post_h = POST_HEADERS.copy()
                                post_h["Referer"] = edit_url
                                
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=post_h) as pr:
                                    if pr.status == 200:
                                        results_log.append(f"✅ {offer_id}: +{len(to_add)}")
                                        lot['secrets_pool'] = remaining
                                        is_any_changed = True
                                    else:
                                        results_log.append(f"❌ {offer_id}: {pr.status}")
                            
                            await asyncio.sleep(2) # Пауза

                        if is_any_changed:
                            async with pool.acquire() as conn_update:
                                await conn_update.execute("UPDATE autorestock_tasks SET lots_config=$1::jsonb WHERE user_uid=$2::uuid", json.dumps(lots_conf), uid)
                        
                        status_msg = ", ".join(results_log) if results_log else "✅ Проверено"
                        await update_status(pool, uid, status_msg)

                    except Exception as e:
                        log_debug(f"Worker task fail: {e}")
            
            await asyncio.sleep(10)
        except Exception as e:
            log_debug(f"Worker loop fail: {e}")
            await asyncio.sleep(10)

# --- API ENDPOINTS ---

@router.post("/fetch_offers")
async def fetch_offers(req: Request):
    try:
        body = await req.json()
        golden_key = body.get("golden_key") or body.get("GoldenKey")
        node_ids = body.get("node_ids") or body.get("NodeIds") or []
    except: return {"success": False, "message": "Bad JSON"}

    results = []
    HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    cookies = {"golden_key": golden_key}
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in node_ids:
            node = str(node).strip()
            if not node.isdigit(): continue
            try:
                # 1. Trade page
                async with session.get(f"https://funpay.com/lots/{node}/trade", headers=HEADERS, cookies=cookies) as resp:
                    if "login" in str(resp.url): return {"success": False, "message": "Key Expired"}
                    html = await resp.text()

                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html))
                if not found_ids:
                    # Fallback for single
                    async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=HEADERS, cookies=cookies) as r2:
                        h2 = await r2.text()
                        oid_m = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', h2)
                        if oid_m: found_ids.add(oid_m.group(1))

                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies=cookies) as r_edit:
                        ht = await r_edit.text()
                        m_nm = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', ht)
                        nm = html_lib.unescape(m_nm.group(1)) if m_nm else "Item"
                        results.append({"node_id": node, "offer_id": oid, "name": nm, "valid": True})
                    await asyncio.sleep(0.1)
            except: pass
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(req: Request):
    global WORKER_TASK_STARTED
    # Lazy imports to prevent 502
    from auth.guards import get_current_user
    from utils_crypto import encrypt_data

    try:
        pool = getattr(req.app.state, 'pool', None)
        if not pool: return JSONResponse(status_code=200, content={"success": False, "message": "DB Error"})

        # 1. Auth
        try:
            u = await get_current_user(req.app, req)
            uid_obj = uuid.UUID(str(u['uid']))
        except Exception as e:
            return JSONResponse(status_code=200, content={"success": False, "message": f"Auth Fail: {e}"})

        # 2. Data
        try:
            body = await req.json()
            golden_key = body.get("golden_key") or body.get("GoldenKey")
            active = body.get("active") if "active" in body else body.get("Active", False)
            lots_data = body.get("lots") or body.get("Lots") or []
        except: return JSONResponse(status_code=200, content={"success": False, "message": "JSON Bad"})

        await ensure_table_exists(pool)

        async with pool.acquire() as conn:
            # 3. Read old pools
            existing_pools = {}
            try:
                row = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1", uid_obj)
                if row and row['lots_config']:
                    raw = row['lots_config']
                    loaded = json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(loaded, list):
                        for l in loaded:
                            existing_pools[str(l.get('offer_id'))] = l.get('secrets_pool', [])
            except: pass

            # 4. Construct final lots
            final_lots = []
            for lot in lots_data:
                oid = str(lot.get('offer_id') or lot.get('OfferId', ''))
                nid = str(lot.get('node_id') or lot.get('NodeId', ''))
                nm = str(lot.get('name') or lot.get('Name', 'Lot'))
                mq = int(lot.get('min_qty') or lot.get('MinQty', 5))
                new_keys = [str(k).strip() for k in (lot.get('add_secrets') or lot.get('AddSecrets') or []) if str(k).strip()]
                
                if not oid: continue
                pool_keys = existing_pools.get(oid, []) + new_keys
                final_lots.append({
                    "node_id": nid, "offer_id": oid, "name": nm, "min_qty": mq, "secrets_pool": pool_keys
                })

            # 5. Database Save
            enc = encrypt_data(golden_key)
            await conn.execute("""
                INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
                VALUES ($1, $2, $3, $4::jsonb, NOW(), 'Настройки сохранены')
                ON CONFLICT (user_uid) DO UPDATE SET
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                is_active = EXCLUDED.is_active,
                lots_config = EXCLUDED.lots_config,
                status_message = 'Обновлено'
            """, uid_obj, enc, active, json.dumps(final_lots))

        # 6. Start worker once
        if not WORKER_TASK_STARTED:
            asyncio.create_task(background_worker(pool))
            WORKER_TASK_STARTED = True

        return {"success": True, "message": "Успешно сохранено!"}

    except Exception as e:
        log_debug(f"SAVE FATAL: {traceback.format_exc()}")
        return JSONResponse(status_code=200, content={"success": False, "message": f"Server Err: {e}"})

@router.get("/status")
async def get_status(req: Request):
    from auth.guards import get_current_user
    try:
        u = await get_current_user(req.app, req)
        uid_obj = uuid.UUID(str(u['uid']))
        pool = req.app.state.pool
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1", uid_obj)
        
        if not r: return {"active": False, "message": "Не настроено", "lots": []}
        
        display_lots = []
        if r['lots_config']:
            raw = r['lots_config']
            loaded = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(loaded, list):
                for l in loaded:
                    display_lots.append({
                        "node_id": l.get('node_id'), "offer_id": l.get('offer_id'),
                        "name": l.get('name', 'Лот'), "min_qty": l.get('min_qty'),
                        "keys_in_db": len(l.get('secrets_pool', []))
                    })
        return {"active": r['is_active'], "message": r['status_message'], "lots": display_lots}
    except: return {"active": False, "message": "Err", "lots": []}
