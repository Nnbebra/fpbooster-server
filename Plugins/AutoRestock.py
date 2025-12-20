import asyncio
import re
import html as html_lib
import json
import aiohttp
import traceback
import uuid
from typing import Dict, Any, List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
# from pydantic import BaseModel # Убрали BaseModel, чтобы не было 500-к на валидации
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])

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
            # Миграция колонок (на всякий случай)
            await conn.execute("ALTER TABLE autorestock_tasks ADD COLUMN IF NOT EXISTS lots_config JSONB;")
            await conn.execute("ALTER TABLE autorestock_tasks ADD COLUMN IF NOT EXISTS check_interval INTEGER DEFAULT 7200;")
    except: pass

async def update_status(pool, uid_obj, msg):
    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", str(msg)[:100], uid_obj)
    except: pass

# --- API ---

@router.post("/fetch_offers")
async def fetch_offers(req: Request):
    """
    Принимает Raw JSON, чтобы избежать ошибок валидации.
    """
    try:
        body = await req.json()
        golden_key = body.get("golden_key")
        node_ids = body.get("node_ids", [])
    except:
        return {"success": False, "message": "Invalid JSON"}

    results = []
    HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    cookies = {"golden_key": golden_key}
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in node_ids:
            node = str(node).strip()
            if not node.isdigit(): continue
            try:
                # 1. Загрузка /trade
                async with session.get(f"https://funpay.com/lots/{node}/trade", headers=HEADERS, cookies=cookies) as resp:
                    if "login" in str(resp.url): return {"success": False, "message": "Golden Key невалиден"}
                    html = await resp.text()

                # 2. Поиск offerEdit (только свои)
                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html))
                
                # Fallback для одиночных
                if not found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=HEADERS, cookies=cookies) as r2:
                        h2 = await r2.text()
                        oid, name, _, _, _, _ = parse_edit_page(h2)
                        if oid: found_ids.add(oid)

                if not found_ids:
                    results.append({"node_id": node, "valid": False, "error": "Лоты не найдены"})
                    continue

                # 3. Детализация
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
async def save_settings(req: Request, u=Depends(get_current_user_raw)):
    """
    Принимает сырой Request, парсит JSON вручную и сохраняет в БД.
    Гарантированно возвращает 200 OK с текстом ошибки в поле message.
    """
    try:
        # 1. Проверка пула БД
        pool = getattr(req.app.state, 'pool', None)
        if not pool: 
            return JSONResponse(status_code=200, content={"success": False, "message": "DB not ready"})

        # 2. Парсинг тела запроса (Manual JSON Parse)
        try:
            body = await req.json()
            golden_key = body.get("golden_key")
            active = body.get("active", False)
            lots_data = body.get("lots", [])
        except Exception as e:
            return JSONResponse(status_code=200, content={"success": False, "message": f"Invalid JSON body: {e}"})

        # 3. Конвертация UID
        try:
            user_uid_obj = uuid.UUID(str(u['uid']))
        except:
            return JSONResponse(status_code=200, content={"success": False, "message": "Invalid User UID"})

        await ensure_table_exists(pool)

        async with pool.acquire() as conn:
            # 5. Чтение старых данных
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
                print(f"Config Read Warn: {e}")

            # 6. Сборка нового конфига
            final_lots = []
            for lot_dict in lots_data:
                # Используем .get() так как это теперь словарь
                oid = str(lot_dict.get('offer_id', ''))
                if not oid: continue
                
                new_keys = [str(k).strip() for k in lot_dict.get('add_secrets', []) if str(k).strip()]
                pool_keys = existing_pools.get(oid, []) + new_keys
                
                final_lots.append({
                    "node_id": str(lot_dict.get('node_id', '')), 
                    "offer_id": oid, 
                    "name": str(lot_dict.get('name', 'Item')), 
                    "min_qty": int(lot_dict.get('min_qty', 5)), 
                    "secrets_pool": pool_keys
                })

            # 7. Шифрование
            try:
                enc = encrypt_data(golden_key)
            except:
                return JSONResponse(status_code=200, content={"success": False, "message": "Encryption Error"})
            
            # 8. Сохранение (JSON string explicit cast)
            json_str = json.dumps(final_lots)
            
            # Используем CAST($4 AS jsonb)
            await conn.execute("""
                INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
                VALUES ($1, $2, $3, CAST($4 AS jsonb), NOW(), 'Обновлено')
                ON CONFLICT (user_uid) DO UPDATE SET
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                is_active = EXCLUDED.is_active,
                lots_config = EXCLUDED.lots_config,
                status_message = 'Настройки сохранены'
            """, user_uid_obj, enc, active, json_str)
            
        return {"success": True, "message": "Сохранено успешно"}

    except Exception as e:
        # ЛОВИМ ВСЁ И ОТДАЕМ КЛИЕНТУ
        err_msg = f"{type(e).__name__}: {str(e)}"
        print(f"!!! CRITICAL SAVE ERROR: {err_msg}")
        traceback.print_exc()
        return JSONResponse(status_code=200, content={"success": False, "message": f"Server Err: {err_msg}"})

@router.get("/status")
async def get_status(req: Request, u=Depends(get_current_user_raw)):
    try:
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

    HEADERS = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"}
    GET_HEADERS = {k:v for k,v in HEADERS.items() if k != "X-Requested-With"}
    
    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            
            async with app.state.pool.acquire() as conn:
                try:
                    tasks = await conn.fetch("SELECT * FROM autorestock_tasks WHERE is_active = TRUE AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '1 hour')")
                except: tasks = []

            if not tasks: await asyncio.sleep(10); continue
            
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                for t in tasks:
                    try:
                        uid_val = t['user_uid']
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
                            async with session.get(edit_url, headers=GET_HEADERS, cookies=cookies) as r:
                                html = await r.text()
                                
                            oid, _, cur_text, csrf, is_active, is_auto = parse_edit_page(html)
                            
                            if not csrf: 
                                log_msg.append(f"⚠️ {lot['node_id']}: нет доступа")
                                continue
                            if not is_auto: continue 
                            
                            if count_lines(cur_text) < min_q:
                                to_add = pool[:50]
                                lot['secrets_pool'] = pool[50:]
                                new_text = cur_text.strip() + "\n" + "\n".join(to_add)
                                
                                payload = {"csrf_token": csrf, "offer_id": oid, "node_id": node_id, "secrets": new_text, "auto_delivery": "on", "active": "on" if is_active else "", "save": "Сохранить"}
                                if not is_active: payload.pop("active", None)
                                
                                post_h = HEADERS.copy(); post_h["Referer"] = edit_url
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=post_h) as pr:
                                    if pr.status == 200:
                                        log_msg.append(f"✅ {lot['node_id']}: +{len(to_add)}")
                                        is_changed = True
                                    else:
                                        log_msg.append(f"❌ {lot['node_id']}: {pr.status}")
                            await asyncio.sleep(2)

                        if is_changed:
                            async with app.state.pool.acquire() as c:
                                await c.execute("UPDATE autorestock_tasks SET lots_config=CAST($1 AS jsonb) WHERE user_uid=$2", json.dumps(lots), uid_val)
                        
                        await update_status(app.state.pool, uid_val, ", ".join(log_msg) if log_msg else "✅ Проверено")
                    except Exception as e:
                        print(f"Worker Err: {e}")
            await asyncio.sleep(5)
        except: await asyncio.sleep(5)
