import asyncio
import re
import html as html_lib
import json
import aiohttp
import traceback
import uuid
import sys
import os
from datetime import datetime, timedelta
from typing import Dict, Any, List

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])

# --- ПАРСИНГ ---
def get_all_form_data(html: str):
    data = {}
    inputs = re.findall(r'<input[^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']', html)
    for name, value in inputs: data[name] = html_lib.unescape(value)
    textareas = re.findall(r'<textarea[^>]*name=["\']([^"\']+)["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    for name, content in textareas: data[name] = html_lib.unescape(content)
    is_active = bool(re.search(r'name=["\']active["\'][^>]*checked', html))
    is_auto = bool(re.search(r'name=["\']auto_delivery["\'][^>]*checked', html))
    return data, data.get("offer_id"), data.get("secrets", ""), is_active, is_auto

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
                cat_name = f"Раздел {node}"
                try:
                    async with session.get(f"https://funpay.com/lots/{node}/", headers=HEADERS) as resp_pub:
                        if resp_pub.status == 200:
                            m = re.search(r'<h1[^>]*>(.*?)</h1>', await resp_pub.text())
                            if m: cat_name = html_lib.unescape(m.group(1)).strip()
                except: pass

                async with session.get(f"https://funpay.com/lots/{node}/trade", headers=HEADERS, cookies={"golden_key": golden_key}) as resp:
                    html = await resp.text()
                
                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html))
                if not found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=HEADERS, cookies={"golden_key": golden_key}) as r2:
                        if re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', await r2.text()): found_ids.add(re.search(r'value=["\'](\d+)["\'][^>]*name=["\']offer_id["\']', await r2.text()).group(1))

                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies={"golden_key": golden_key}) as r_edit:
                        ht = await r_edit.text()
                        nm = "Товар"
                        m_nm = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', ht)
                        if m_nm: nm = html_lib.unescape(m_nm.group(1))
                        results.append({"node_id": node, "node_name": cat_name, "offer_id": oid, "name": nm, "valid": True})
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
                        for l in (json.loads(row['lots_config']) if isinstance(row['lots_config'], str) else row['lots_config']):
                            existing_conf[str(l.get('offer_id'))] = l.get('secrets_source', [])
                    except: pass

        final_lots = []
        for lot in (body.get("lots") or []):
            oid = str(lot.get('offer_id', ''))
            clean_secrets = [s.strip() for s in lot.get('add_secrets', []) if s.strip()]
            final_source = clean_secrets if clean_secrets else existing_conf.get(oid, [])
            final_lots.append({
                "node_id": str(lot.get('node_id', '')),
                "node_name": str(lot.get('node_name', '')),
                "offer_id": oid,
                "name": str(lot.get('name', 'Lot')),
                "min_qty": int(lot.get('min_qty', 5)),
                "auto_enable": bool(lot.get('auto_enable', True)),
                "secrets_source": final_source
            })

        enc = encrypt_data(body.get("golden_key", ""))
        async with req.app.state.pool.acquire() as conn:
            # При сохранении сбрасываем таймер (last_check_at=NULL), чтобы проверка пошла сразу
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
            # ДОБАВЛЕНО: last_check_at в выборку
            r = await conn.fetchrow("SELECT is_active, status_message, lots_config, last_check_at FROM autorestock_tasks WHERE user_uid=$1", uid_obj)
        if not r: return {"active": False, "message": "Не настроено", "lots": [], "next_check": None}
        
        # Рассчитываем время следующей проверки
        next_check_time = None
        if r['last_check_at']:
            # Интервал проверки 2 часа
            next_check_time = r['last_check_at'] + timedelta(hours=2)

        lots = json.loads(r['lots_config']) if isinstance(r['lots_config'], str) else r['lots_config']
        display = []
        for l in lots:
            display.append({
                "node_id": l.get('node_id'), "node_name": l.get('node_name'),
                "offer_id": l.get('offer_id'), "name": l.get('name'), 
                "min_qty": l.get('min_qty'), "auto_enable": l.get('auto_enable', True),
                "keys_in_db": len(l.get('secrets_source', [])),
                "source_text": l.get('secrets_source', [])
            })
        
        return {
            "active": r['is_active'], 
            "message": r['status_message'], 
            "lots": display,
            "next_check": next_check_time.isoformat() if next_check_time else None
        }
    except: return {"active": False, "message": "Error", "lots": []}

# --- ВОРКЕР ---
async def worker(app):
    await asyncio.sleep(5)
    from utils_crypto import decrypt_data
    HEADERS = {"User-Agent": "Mozilla/5.0"}
    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            async with app.state.pool.acquire() as conn:
                tasks = await conn.fetch("SELECT * FROM autorestock_tasks WHERE is_active = TRUE AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')") # Интервал 2 часа
            if not tasks:
                await asyncio.sleep(20); continue

            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                for t in tasks:
                    try:
                        cookies = {"golden_key": decrypt_data(t['encrypted_golden_key'])}
                        lots_conf = json.loads(t['lots_config']) if isinstance(t['lots_config'], str) else t['lots_config']
                        log_msg = []
                        for lot in lots_conf:
                            src = lot.get('secrets_source', [])
                            if not src: continue 
                            
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={lot['offer_id']}"
                            async with session.get(edit_url, headers=HEADERS, cookies=cookies) as r: html = await r.text()
                            if "login" in str(r.url): break
                            
                            data, oid, cur, act, auto = get_all_form_data(html)
                            if not data.get("csrf_token"): continue
                            
                            should_auto = lot.get('auto_enable', True)
                            if not auto and should_auto: auto = True 
                            
                            cur_lines = [l for l in cur.split('\n') if l.strip()]
                            needed = int(lot['min_qty']) - len(cur_lines)
                            
                            if needed > 0:
                                to_add = []
                                for i in range(needed): to_add.append(src[i % len(src)])
                                payload = data.copy()
                                payload["secrets"] = cur.strip() + "\n" + "\n".join(to_add)
                                payload["save"] = "Сохранить"
                                if auto: payload["auto_delivery"] = "on"; 
                                elif "auto_delivery" in payload: del payload["auto_delivery"]
                                if act: payload["active"] = "on"; 
                                elif "active" in payload: del payload["active"]
                                
                                ph = HEADERS.copy(); ph["X-Requested-With"] = "XMLHttpRequest"; ph["Referer"] = edit_url
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, headers=ph, cookies=cookies) as pr:
                                    if pr.status == 200: log_msg.append(f"✅{lot['offer_id']}: +{needed}")
                                    else: log_msg.append(f"❌{lot['offer_id']}")
                            await asyncio.sleep(2)
                        
                        async with app.state.pool.acquire() as c:
                            await c.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", ", ".join(log_msg) or "✅ Проверено", t['user_uid'])
                    except: pass
            await asyncio.sleep(10)
        except: await asyncio.sleep(30)
