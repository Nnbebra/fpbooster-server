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

# --- ЛОГИРОВАНИЕ (Только в консоль, без файла) ---
def log_console(msg):
    # Вывод только критических ошибок или стартов, чтобы не спамить
    # print(f"[AutoRestock] {msg}", flush=True) 
    pass

# --- ПАРСИНГ ---
def parse_edit_page(html: str):
    offer_id, secrets, csrf, node_id, location = None, "", None, None, "trade"
    is_active, is_auto = False, False
    
    m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', html)
    if not m_oid: m_oid = re.search(r'value=["\'](\d+)["\'][^>]*name=["\']offer_id["\']', html)
    if m_oid: offer_id = m_oid.group(1)
    
    m_node = re.search(r'name=["\']node_id["\'][^>]*value=["\'](\d+)["\']', html)
    if m_node: node_id = m_node.group(1)

    m_loc = re.search(r'name=["\']location["\'][^>]*value=["\']([^"\']*)["\']', html)
    if m_loc: location = m_loc.group(1)

    m_sec = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    if m_sec: secrets = html_lib.unescape(m_sec.group(1))

    m_csrf = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', html)
    if not m_csrf: m_csrf = re.search(r'value=["\']([^"\']+)["\']', html)
    if m_csrf: csrf = m_csrf.group(1)

    if re.search(r'name=["\']active["\'][^>]*checked', html): is_active = True
    if re.search(r'name=["\']auto_delivery["\'][^>]*checked', html): is_auto = True

    return offer_id, secrets, csrf, is_active, is_auto, node_id, location

def get_all_form_data(html: str):
    data = {}
    inputs = re.findall(r'<input[^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']', html)
    for name, value in inputs: data[name] = html_lib.unescape(value)
    textareas = re.findall(r'<textarea[^>]*name=["\']([^"\']+)["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    for name, content in textareas: data[name] = html_lib.unescape(content)
    is_active = bool(re.search(r'name=["\']active["\'][^>]*checked', html))
    is_auto = bool(re.search(r'name=["\']auto_delivery["\'][^>]*checked', html))
    return data, data.get("offer_id"), data.get("secrets", ""), is_active, is_auto

# --- API ENDPOINTS ---

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
                # 1. Загружаем страницу трейда (категории)
                async with session.get(f"https://funpay.com/lots/{node}/trade", headers=HEADERS, cookies={"golden_key": golden_key}) as resp:
                    html = await resp.text()
                
                # Парсим название категории из H1
                cat_name = f"Раздел {node}"
                m_h1 = re.search(r'<h1[^>]*>(.*?)</h1>', html)
                if m_h1: cat_name = html_lib.unescape(m_h1.group(1)).strip()

                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html))
                if not found_ids:
                    # Fallback
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
                        # Возвращаем node_name для группировки в софте
                        results.append({
                            "node_id": node, 
                            "node_name": cat_name, 
                            "offer_id": oid, 
                            "name": nm, 
                            "valid": True
                        })
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
            clean_secrets = [s.strip() for s in lot.get('add_secrets', []) if s.strip()]
            final_source = clean_secrets if clean_secrets else existing_conf.get(oid, [])

            final_lots.append({
                "node_id": str(lot.get('node_id', '')),
                "node_name": str(lot.get('node_name', '')), # Сохраняем имя категории
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
            display.append({
                "node_id": l.get('node_id'), 
                "node_name": l.get('node_name', f"Cat {l.get('node_id')}"), # Отдаем имя категории
                "offer_id": l.get('offer_id'),
                "name": l.get('name'), 
                "min_qty": l.get('min_qty'),
                "auto_enable": l.get('auto_enable', True),
                "keys_in_db": len(l.get('secrets_source', []))
            })
        return {"active": r['is_active'], "message": r['status_message'], "lots": display}
    except: return {"active": False, "message": "Error", "lots": []}

# --- ВОРКЕР ---
async def worker(app):
    await asyncio.sleep(5)
    log_console("Worker started.")
    from utils_crypto import decrypt_data
    
    HEADERS = {"User-Agent": "Mozilla/5.0"}
    
    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            
            # ИНТЕРВАЛ 2 ЧАСА (как просили)
            async with app.state.pool.acquire() as conn:
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
                        
                        log_msg = []
                        
                        for lot in lots_conf:
                            source_lines = lot.get('secrets_source', [])
                            if not source_lines: continue 

                            offer_id = lot['offer_id']
                            min_q = int(lot['min_qty'])
                            
                            # 1. Загрузка
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=HEADERS, cookies=cookies) as r:
                                html = await r.text()
                            
                            if "login" in str(r.url): break

                            # 2. Полный парсинг (включая location)
                            form_data, oid, cur_text, active_lot, auto_dlv = get_all_form_data(html)
                            if not form_data.get("csrf_token"): continue

                            # 3. Автовыдача
                            should_be_auto = lot.get('auto_enable', True)
                            final_auto = auto_dlv
                            if not auto_dlv and should_be_auto: final_auto = True 

                            # 4. Анализ и пополнение
                            cur_lines = [l for l in cur_text.split('\n') if l.strip()]
                            cur_count = len(cur_lines)
                            
                            if cur_count < min_q:
                                needed = min_q - cur_count
                                to_add = []
                                src_len = len(source_lines)
                                for i in range(needed):
                                    to_add.append(source_lines[i % src_len])

                                new_full_text = cur_text.strip() + "\n" + "\n".join(to_add)
                                new_full_text = new_full_text.strip()

                                payload = form_data.copy()
                                payload["secrets"] = new_full_text
                                payload["save"] = "Сохранить"
                                if final_auto: payload["auto_delivery"] = "on"
                                elif "auto_delivery" in payload: del payload["auto_delivery"]
                                if active_lot: payload["active"] = "on"
                                elif "active" in payload: del payload["active"]

                                post_headers = HEADERS.copy()
                                post_headers["X-Requested-With"] = "XMLHttpRequest"
                                post_headers["Referer"] = edit_url

                                async with session.post("https://funpay.com/lots/offerSave", data=payload, headers=post_headers, cookies=cookies) as pr:
                                    resp_text = await pr.text()
                                    if pr.status == 200 and "error" not in resp_text.lower():
                                        log_msg.append(f"✅{offer_id}: +{len(to_add)}")
                                    else:
                                        log_msg.append(f"❌{offer_id}")
                            
                            await asyncio.sleep(2)

                        status = ", ".join(log_msg) if log_msg else "✅ Проверено"
                        async with app.state.pool.acquire() as c:
                            await c.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", status[:100], uid)

                    except: pass
            
            await asyncio.sleep(10)
        except: await asyncio.sleep(30)
