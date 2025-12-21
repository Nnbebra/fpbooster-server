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
from auth.guards import get_current_user
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])

# Глобальная переменная для задачи воркера
WORKER_TASK = None

# --- ЛОГГЕР ---
def log(msg):
    t = datetime.now().strftime("%H:%M:%S")
    print(f"[{t}] [AutoRestock] {msg}", flush=True)

# --- HELPERS ---
def count_lines(text: str):
    if not text: return 0
    return len([l for l in text.split('\n') if l.strip()])

def parse_edit_page(html: str):
    """Парсинг страницы редактирования лота"""
    offer_id, name, secrets, csrf = None, "Без названия", "", None
    is_active, is_auto = False, False
    
    m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', html)
    if not m_oid: m_oid = re.search(r'value=["\'](\d+)["\'][^>]*name=["\']offer_id["\']', html)
    if m_oid: offer_id = m_oid.group(1)
    
    m_sec = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    if m_sec: secrets = html_lib.unescape(m_sec.group(1))

    m_csrf = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', html)
    if not m_csrf: m_csrf = re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']csrf_token["\']', html)
    if m_csrf: csrf = m_csrf.group(1)

    # Проверка галочек (ищем checked)
    if 'name="active" checked' in html or "name='active' checked" in html: is_active = True
    if 'name="auto_delivery" checked' in html or "name='auto_delivery' checked" in html: is_auto = True

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
            try: await conn.execute("ALTER TABLE autorestock_tasks ADD COLUMN IF NOT EXISTS lots_config JSONB;")
            except: pass
            try: await conn.execute("ALTER TABLE autorestock_tasks ADD COLUMN IF NOT EXISTS check_interval INTEGER DEFAULT 7200;")
            except: pass
    except Exception as e:
        log(f"DB Init Error: {e}")

async def update_status(pool, uid_obj, msg):
    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2::uuid", str(msg)[:100], uid_obj)
    except: pass

# --- ВОРКЕР (ЛОГИКА) ---
async def worker_loop(pool):
    log("Воркер успешно запущен!")
    await ensure_table_exists(pool)

    HEADERS_GET = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }
    HEADERS_POST = HEADERS_GET.copy()
    HEADERS_POST["X-Requested-With"] = "XMLHttpRequest"

    while True:
        try:
            # 1. Ищем задачи (проверка раз в 2 часа)
            tasks = []
            async with pool.acquire() as conn:
                try:
                    tasks = await conn.fetch("""
                        SELECT * FROM autorestock_tasks 
                        WHERE is_active = TRUE 
                        AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')
                    """)
                except: pass
            
            if not tasks:
                await asyncio.sleep(5)
                continue

            log(f"Найдено задач для проверки: {len(tasks)}")

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
                        logs = []
                        cookies = {"golden_key": key}

                        for lot in lots:
                            pool_keys = lot.get('secrets_pool', [])
                            if not pool_keys: continue 

                            offer_id = lot.get('offer_id')
                            node_id = lot.get('node_id')
                            min_q = int(lot.get('min_qty', 5))

                            # ШАГ 1: Загрузка страницы
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=HEADERS_GET, cookies=cookies) as r:
                                html = await r.text()

                            # ШАГ 2: Парсинг
                            real_oid, secrets_text, csrf, is_active, is_auto = parse_edit_page(html)

                            if not csrf:
                                logs.append(f"⚠️ {offer_id}: Нет доступа")
                                continue
                            
                            # ШАГ 3: Проверка галочки автовыдачи
                            if not is_auto:
                                # Галочка выключена -> пропускаем
                                continue

                            # ШАГ 4: Проверка количества
                            cur_qty = count_lines(secrets_text)
                            
                            if cur_qty < min_q:
                                to_add = pool_keys[:50]
                                remaining_pool = pool_keys[50:]
                                
                                new_text = secrets_text.strip() + "\n" + "\n".join(to_add)
                                new_text = new_text.strip()
                                
                                # ШАГ 5: Сохранение
                                payload = {
                                    "csrf_token": csrf,
                                    "offer_id": real_oid,
                                    "node_id": node_id,
                                    "secrets": new_text,
                                    "auto_delivery": "on",
                                    "active": "on" if is_active else "",
                                    "save": "Сохранить"
                                }
                                if not is_active: payload.pop("active", None)
                                
                                req_h = HEADERS_POST.copy()
                                req_h["Referer"] = edit_url
                                
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=req_h) as pr:
                                    if pr.status == 200:
                                        logs.append(f"✅ {offer_id}: +{len(to_add)}")
                                        lot['secrets_pool'] = remaining_pool
                                        is_changed = True
                                    else:
                                        logs.append(f"❌ {offer_id}: {pr.status}")
                            
                            await asyncio.sleep(2)

                        # Обновление базы
                        if is_changed:
                            async with app.state.pool.acquire() as c:
                                await c.execute("UPDATE autorestock_tasks SET lots_config=$1::jsonb WHERE user_uid=$2::uuid", json.dumps(lots), uid_val)
                        
                        # Обновление статуса
                        final_msg = ", ".join(logs) if logs else f"✅ Проверено {datetime.now().strftime('%H:%M')}"
                        await update_status(pool, uid_val, final_msg)

                    except Exception as e:
                        log(f"Worker Item Error: {e}")
                        await update_status(pool, uid_val, "Ошибка воркера")
            
            await asyncio.sleep(5)
        except Exception as e:
            log(f"Worker Global Error: {e}")
            await asyncio.sleep(5)

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
                        m = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', h2)
                        if m: found_ids.add(m.group(1))

                if not found_ids:
                    results.append({"node_id": node, "valid": False, "error": "Лоты не найдены"})
                    continue

                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies=cookies) as r_edit:
                        ht = await r_edit.text()
                        nm_m = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', ht)
                        nm = html_lib.unescape(nm_m.group(1)) if nm_m else "Без названия"
                        results.append({"node_id": node, "offer_id": oid, "name": nm, "valid": True})
                    await asyncio.sleep(0.1)
            except Exception as e:
                results.append({"node_id": node, "valid": False, "error": str(e)[:20]})
            await asyncio.sleep(0.5)
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(req: Request):
    """
    Сохранение и ЗАПУСК ВОРКЕРА.
    """
    global WORKER_TASK
    try:
        pool = getattr(req.app.state, 'pool', None)
        if not pool: return JSONResponse(status_code=200, content={"success": False, "message": "DB not ready"})

        # 1. Авторизация
        try:
            u = await get_current_user(req.app, req)
            uid_obj = uuid.UUID(str(u['uid']))
        except Exception as e:
            return JSONResponse(status_code=200, content={"success": False, "message": f"Auth Error: {e}"})

        # 2. JSON
        try:
            body = await req.json()
            golden_key = body.get("golden_key") or body.get("GoldenKey")
            active = body.get("active") if "active" in body else body.get("Active", False)
            lots_data = body.get("lots") or body.get("Lots") or []
        except:
            return JSONResponse(status_code=200, content={"success": False, "message": "JSON Error"})

        await ensure_table_exists(pool)

        async with pool.acquire() as conn:
            # 3. Merge старого конфига
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

            # 4. Новый конфиг
            final_lots = []
            for lot in lots_data:
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

            # 5. Запись
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
                last_check_at = NULL -- Сброс таймера для немедленной проверки
            """, uid_obj, enc, active, json_str)

        # 6. ЗАПУСК ВОРКЕРА (ЕСЛИ ЕЩЕ НЕ ЗАПУЩЕН)
        if WORKER_TASK is None or WORKER_TASK.done():
            log("Запускаем воркер впервые...")
            WORKER_TASK = asyncio.create_task(worker_loop(pool))
            
        return {"success": True, "message": "Конфигурация сохранена"}

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=200, content={"success": False, "message": f"Err: {str(e)}"})

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
