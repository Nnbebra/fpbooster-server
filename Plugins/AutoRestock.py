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

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])

LOG_FILE = os.path.join(os.getcwd(), "restock_final_debug.log")

def log_debug(msg):
    try:
        t = datetime.now().strftime("%H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{t}] {msg}\n")
        print(f"[AutoRestock] {msg}", flush=True)
    except: pass

# --- ПАРСИНГ ВСЕЙ ФОРМЫ ---
def get_all_form_data(html: str):
    """
    Собирает ВСЕ данные из формы, чтобы FunPay принял запрос.
    Возвращает словарь data и основные параметры.
    """
    data = {}
    
    # 1. Собираем все input (hidden, text, etc)
    # Ищем теги <input ... name="..." value="...">
    inputs = re.findall(r'<input[^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']', html)
    for name, value in inputs:
        data[name] = html_lib.unescape(value)

    # 2. Собираем все textarea (описания, старые ключи)
    textareas = re.findall(r'<textarea[^>]*name=["\']([^"\']+)["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    for name, content in textareas:
        data[name] = html_lib.unescape(content)

    # 3. Извлекаем текущие настройки галочек
    is_active = bool(re.search(r'name=["\']active["\'][^>]*checked', html))
    is_auto = bool(re.search(r'name=["\']auto_delivery["\'][^>]*checked', html))

    # Извлекаем offer_id и node_id отдельно для удобства, хотя они есть в data
    offer_id = data.get("offer_id")
    
    # Текущие товары (для подсчета)
    current_secrets = data.get("secrets", "")

    return data, offer_id, current_secrets, is_active, is_auto

# --- API ---

@router.post("/fetch_offers")
async def fetch_offers(req: Request):
    try:
        body = await req.json()
        golden_key = body.get("golden_key") or body.get("GoldenKey")
        node_ids = body.get("node_ids") or body.get("NodeIds") or []
    except: return {"success": False, "message": "JSON Error"}

    results = []
    HEADERS = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in node_ids:
            node = str(node).strip()
            if not node.isdigit(): continue
            try:
                async with session.get(f"https://funpay.com/lots/{node}/trade", headers=HEADERS, cookies={"golden_key": golden_key}) as resp:
                    html = await resp.text()
                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html))
                if not found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=HEADERS, cookies={"golden_key": golden_key}) as r2:
                        h2 = await r2.text()
                        m = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', h2)
                        if m: found_ids.add(m.group(1))

                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies={"golden_key": golden_key}) as r_edit:
                        ht = await r_edit.text()
                        nm = "Товар"
                        m_nm = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', ht)
                        if m_nm: nm = html_lib.unescape(m_nm.group(1))
                        results.append({"node_id": node, "offer_id": oid, "name": nm, "valid": True})
                    await asyncio.sleep(0.1)
            except: pass
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(req: Request):
    from auth.guards import get_current_user
    from utils_crypto import encrypt_data
    try:
        u = await get_current_user(req.app, req)
        uid_obj = uuid.UUID(str(u['uid']))
        body = await req.json()
        
        # Подгружаем старое, чтобы не потерять source_text если придет пустое
        existing_conf = {}
        pool_obj = getattr(req.app.state, 'pool', None)
        if pool_obj:
            async with pool_obj.acquire() as conn:
                row = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1", uid_obj)
                if row and row['lots_config']:
                    try:
                        loaded = json.loads(row['lots_config']) if isinstance(row['lots_config'], str) else row['lots_config']
                        for l in loaded: existing_conf[str(l.get('offer_id'))] = l.get('secrets_source', [])
                    except: pass

        final_lots = []
        for lot in (body.get("lots") or []):
            oid = str(lot.get('offer_id', ''))
            raw_secrets_list = lot.get('add_secrets', [])
            clean_secrets = [s.strip() for s in raw_secrets_list if s.strip()]
            final_source = clean_secrets if clean_secrets else existing_conf.get(oid, [])

            final_lots.append({
                "node_id": str(lot.get('node_id', '')),
                "offer_id": oid,
                "name": str(lot.get('name', 'Lot')),
                "min_qty": int(lot.get('min_qty', 5)),
                "auto_enable": bool(lot.get('auto_enable', True)),
                "secrets_source": final_source
            })

        enc = encrypt_data(body.get("golden_key", ""))
        
        async with req.app.state.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
                VALUES ($1, $2, $3, $4::jsonb, NULL, 'Настройки сохранены')
                ON CONFLICT (user_uid) DO UPDATE SET
                encrypted_golden_key=EXCLUDED.encrypted_golden_key, is_active=EXCLUDED.is_active,
                lots_config=EXCLUDED.lots_config, status_message='Настройки обновлены', last_check_at=NULL
            """, uid_obj, enc, body.get("active", False), json.dumps(final_lots))
            
        return {"success": True, "message": "Сохранено"}
    except Exception as e: return JSONResponse(status_code=200, content={"success": False, "message": str(e)})

@router.get("/status")
async def get_status(req: Request):
    from auth.guards import get_current_user
    try:
        u = await get_current_user(req.app, req)
        uid_obj = uuid.UUID(str(u['uid']))
        async with req.app.state.pool.acquire() as conn:
            r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1", uid_obj)
        if not r: return {"active": False, "message": "Не настроено", "lots": []}
        
        lots = json.loads(r['lots_config']) if isinstance(r['lots_config'], str) else r['lots_config']
        display = []
        for l in lots:
            src = l.get('secrets_source', [])
            display.append({
                "node_id": l.get('node_id'), "offer_id": l.get('offer_id'),
                "name": l.get('name'), "min_qty": l.get('min_qty'),
                "auto_enable": l.get('auto_enable', True),
                "keys_in_db": len(src)
            })
        return {"active": r['is_active'], "message": r['status_message'], "lots": display}
    except: return {"active": False, "message": "Error", "lots": []}

# --- ВОРКЕР ---
async def worker(app):
    await asyncio.sleep(5)
    log_debug("Worker started (FULL FORM PARSE MODE).")
    from utils_crypto import decrypt_data
    
    HEADERS = {"User-Agent": "Mozilla/5.0"}
    
    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            
            async with app.state.pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT * FROM autorestock_tasks 
                    WHERE is_active = TRUE 
                    AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '30 seconds')
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
                        
                        log_msg = []
                        
                        for lot in lots_conf:
                            source_lines = lot.get('secrets_source', [])
                            if not source_lines: continue 

                            offer_id = lot['offer_id']
                            min_q = int(lot['min_qty'])
                            
                            # 1. Загрузка страницы
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=HEADERS, cookies=cookies) as r:
                                html = await r.text()
                            
                            if "login" in str(r.url): continue

                            # 2. ПАРСИМ ВСЕ ПОЛЯ
                            form_data, oid, cur_text, active_lot, auto_dlv = get_all_form_data(html)
                            
                            if not form_data.get("csrf_token"): 
                                log_debug(f"[{uid}] No CSRF/Form data for {offer_id}")
                                continue

                            # 3. Автовыдача
                            should_be_auto = lot.get('auto_enable', True)
                            final_auto = auto_dlv
                            if not auto_dlv and should_be_auto:
                                final_auto = True 

                            # 4. Анализ
                            cur_lines = [l for l in cur_text.split('\n') if l.strip()]
                            cur_count = len(cur_lines)
                            
                            # 5. Пополнение
                            if cur_count < min_q:
                                needed = min_q - cur_count
                                
                                to_add = []
                                src_len = len(source_lines)
                                for i in range(needed):
                                    to_add.append(source_lines[i % src_len])

                                new_full_text = cur_text.strip() + "\n" + "\n".join(to_add)
                                new_full_text = new_full_text.strip()

                                # 6. ПОДГОТОВКА PAYLOAD (Копируем всё, что было в форме)
                                payload = form_data.copy()
                                
                                # Обновляем то, что нам нужно
                                payload["secrets"] = new_full_text
                                payload["save"] = "Сохранить" # Кнопка сабмита

                                # Галочки (они передаются только если выбраны)
                                if final_auto: payload["auto_delivery"] = "on"
                                elif "auto_delivery" in payload: del payload["auto_delivery"]

                                if active_lot: payload["active"] = "on"
                                elif "active" in payload: del payload["active"]

                                # Заголовки (Важен Referer)
                                post_headers = HEADERS.copy()
                                post_headers["X-Requested-With"] = "XMLHttpRequest"
                                post_headers["Referer"] = edit_url

                                async with session.post("https://funpay.com/lots/offerSave", data=payload, headers=post_headers, cookies=cookies) as pr:
                                    resp_text = await pr.text()
                                    if pr.status == 200 and "error" not in resp_text.lower():
                                        log_msg.append(f"✅{offer_id}: +{len(to_add)}")
                                        log_debug(f"SAVED {offer_id}: added {len(to_add)}")
                                    else:
                                        log_msg.append(f"❌{offer_id}")
                                        log_debug(f"ERR {offer_id}: {pr.status}")
                            
                            await asyncio.sleep(1.5)

                        status = ", ".join(log_msg) if log_msg else "✅ Проверено"
                        async with app.state.pool.acquire() as c:
                            await c.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", status[:100], uid)

                    except Exception as e:
                        log_debug(f"Task Err {uid}: {e}")
            
            await asyncio.sleep(10)
        except: await asyncio.sleep(30)
