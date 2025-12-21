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

# Импортируем авторизацию и криптографию
from auth.guards import get_current_user
from utils_crypto import encrypt_data, decrypt_data

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])

# Глобальный флаг, чтобы не запускать воркер несколько раз
WORKER_STARTED = False

# --- ЛОГИРОВАНИЕ ---
LOG_FILE = os.path.join(os.getcwd(), "restock_final_debug.log")

def log_debug(msg):
    try:
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg}\n")
        print(f"[AutoRestock] {msg}")
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
    if not m_csrf: m_csrf = re.search(r'value=["\']([^"\']+)["\']', html)
    if m_csrf: csrf = m_csrf.group(1)

    if re.search(r'name=["\']active["\'][^>]*checked', html): is_active = True
    if re.search(r'name=["\']auto_delivery["\'][^>]*checked', html): is_auto = True

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
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2::uuid", str(msg)[:100], uid_obj)
    except: pass

# --- BACKGROUND WORKER (ЛОГИКА ПРОВЕРКИ) ---
async def restock_worker(pool):
    log_debug("Воркер запущен в фоновом потоке.")
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
                await asyncio.sleep(20)
                continue

            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                for t in tasks:
                    uid = t['user_uid']
                    try:
                        key = decrypt_data(t['encrypted_golden_key'])
                        cookies = {"golden_key": key}
                        lots_conf = json.loads(t['lots_config']) if isinstance(t['lots_config'], str) else t['lots_config']
                        
                        is_changed = False
                        results_log = []

                        for lot in lots_conf:
                            pool_keys = lot.get('secrets_pool', [])
                            if not pool_keys: continue 

                            offer_id = lot['offer_id']
                            min_qty = int(lot.get('min_qty', 5))
                            
                            # 1. Заходим на страницу редактирования
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=HEADERS, cookies=cookies) as r:
                                html_txt = await r.text()
                            
                            oid, _, current_secrets, csrf, active_state, auto_state = parse_edit_page(html_txt)

                            if not csrf:
                                results_log.append(f"⚠️ {offer_id}: Нет доступа")
                                continue

                            # 2. Проверяем автовыдачу (Логика из ТЗ)
                            if not auto_state:
                                results_log.append(f"⏸ {offer_id}: Автовыдача ВЫКЛ")
                                continue

                            # 3. Считаем строки и доливаем если надо
                            if count_lines(current_secrets) < min_qty:
                                to_add = pool_keys[:50]
                                remaining = pool_keys[50:]
                                new_secrets = current_secrets.strip() + "\n" + "\n".join(to_add)
                                
                                # 4. Сохраняем (POST)
                                payload = {
                                    "csrf_token": csrf, "offer_id": oid, "node_id": lot['node_id'],
                                    "secrets": new_secrets, "auto_delivery": "on",
                                    "active": "on" if active_state else "", "save": "Сохранить"
                                }
                                if not active_state: payload.pop("active")

                                req_h = POST_HEADERS.copy()
                                req_h["Referer"] = edit_url
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=req_h) as pr:
                                    if pr.status == 200:
                                        results_log.append(f"✅ {offer_id}: +{len(to_add)}")
                                        lot['secrets_pool'] = remaining
                                        is_changed = True
                            
                            await asyncio.sleep(2)

                        if is_changed:
                            async with pool.acquire() as conn_update:
                                await conn_update.execute("UPDATE autorestock_tasks SET lots_config=$1 WHERE user_uid=$2::uuid", json.dumps(lots_conf), uid)
                        
                        await update_status(pool, uid, ", ".join(results_log) if results_log else "✅ Проверено")

                    except Exception as e:
                        log_debug(f"Task Fail for {uid}: {e}")
            
            await asyncio.sleep(30)
        except Exception as e:
            log_debug(f"Worker Loop Error: {e}")
            await asyncio.sleep(60)

# --- API ENDPOINTS ---

@router.post("/fetch_offers")
async def fetch_offers(req: Request):
    try:
        body = await req.json()
        golden_key = body.get("golden_key") or body.get("GoldenKey")
        node_ids = body.get("node_ids") or body.get("NodeIds") or []
    except: return {"success": False, "message": "JSON Parse Error"}

    results = []
    HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    cookies = {"golden_key": golden_key}
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in node_ids:
            node = str(node).strip()
            if not node.isdigit(): continue
            try:
                async with session.get(f"https://funpay.com/lots/{node}/trade", headers=HEADERS, cookies=cookies) as resp:
                    if "login" in str(resp.url): return {"success": False, "message": "Key Expired"}
                    html = await resp.text()

                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html))
                if not found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=HEADERS, cookies=cookies) as r2:
                        h2 = await r2.text()
                        oid, name, _, _, _, _ = parse_edit_page(h2)
                        if oid: found_ids.add(oid)

                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies=cookies) as r_edit:
                        oid_real, name, _, _, _, _ = parse_edit_page(await r_edit.text())
                        if oid_real:
                            results.append({"node_id": node, "offer_id": oid_real, "name": name, "valid": True})
                    await asyncio.sleep(0.1)
            except: pass
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(req: Request):
    global WORKER_STARTED
    log_debug("\n=== NEW REQUEST: /set ===")
    try:
        pool = getattr(req.app.state, 'pool', None)
        if not pool: return JSONResponse(status_code=200, content={"success": False, "message": "No Pool"})

        # 1. Авторизация
        try:
            u = await get_current_user(req.app, req)
            uid_obj = uuid.UUID(str(u['uid']))
        except Exception as e:
            return JSONResponse(status_code=200, content={"success": False, "message": f"Auth Error: {e}"})

        # 2. Парсинг
        try:
            body = await req.json()
            golden_key = body.get("golden_key") or body.get("GoldenKey")
            active = body.get("active") if "active" in body else body.get("Active", False)
            lots_data = body.get("lots") or body.get("Lots") or []
        except: return JSONResponse(status_code=200, content={"success": False, "message": "JSON Error"})

        await ensure_table_exists(pool)

        async with pool.acquire() as conn:
            # 3. Чтение старых конфигов
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

            # 4. Сборка новых
            final_lots = []
            for lot in lots_data:
                oid = str(lot.get('offer_id') or lot.get('OfferId', ''))
                nid = str(lot.get('node_id') or lot.get('NodeId', ''))
                nm = str(lot.get('name') or lot.get('Name', 'Lot'))
                mq = int(lot.get('min_qty') or lot.get('MinQty', 5))
                new_keys = [str(k).strip() for k in (lot.get('add_secrets') or lot.get('AddSecrets') or []) if str(k).strip()]
                
                if not oid: continue
                pool_keys = existing_pools.get(oid, []) + new_keys
                final_lots.append({"node_id": nid, "offer_id": oid, "name": nm, "min_qty": mq, "secrets_pool": pool_keys})

            # 5. Запись
            enc = encrypt_data(golden_key)
            await conn.execute("""
                INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
                VALUES ($1, $2, $3, $4::jsonb, NULL, 'Настройки сохранены')
                ON CONFLICT (user_uid) DO UPDATE SET
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                is_active = EXCLUDED.is_active,
                lots_config = EXCLUDED.lots_config,
                status_message = 'Обновлено', last_check_at = NULL
            """, uid_obj, enc, active, json.dumps(final_lots))

        # --- ЗАПУСК ВОРКЕРА ПОСЛЕ ПЕРВОГО СОХРАНЕНИЯ ---
        if not WORKER_STARTED:
            asyncio.create_task(restock_worker(pool))
            WORKER_STARTED = True
            log_debug("Фоновый воркер успешно инициирован.")

        return {"success": True, "message": "Конфигурация успешно сохранена"}

    except Exception as e:
        log_debug(f"FATAL ERROR: {traceback.format_exc()}")
        return JSONResponse(status_code=200, content={"success": False, "message": f"Server Error: {str(e)}"})

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
                        "node_id": l.get('node_id'), "offer_id": l.get('offer_id'),
                        "name": l.get('name', 'Лот'), "min_qty": l.get('min_qty'),
                        "keys_in_db": len(l.get('secrets_pool', []))
                    })
        return {"active": r['is_active'], "message": r['status_message'], "lots": display_lots}
    except: return {"active": False, "message": "Error", "lots": []}
