import asyncio
import re
import html as html_lib
import json
import aiohttp
import random
import traceback
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])

class FetchRequest(BaseModel):
    golden_key: str
    node_ids: list[str]

class LotConfig(BaseModel):
    node_id: str
    offer_id: str
    name: str
    min_qty: int
    add_secrets: list[str] = []

class RestockSettings(BaseModel):
    golden_key: str
    active: bool
    lots: list[LotConfig]

async def update_status(pool, uid, msg):
    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", str(msg)[:150], uid)
    except: pass

def get_lot_info(html: str):
    offer_id = None
    lot_name = "Неизвестный лот"
    m_id = re.search(r'name=["\']offer_id["\'][^>]+value=["\'](\d+)["\']', html)
    if m_id: offer_id = m_id.group(1)
    m_name = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', html)
    if m_name: lot_name = html_lib.unescape(m_name.group(1))
    return offer_id, lot_name

def count_items(text: str) -> int:
    if not text: return 0
    return len([line for line in text.split('\n') if line.strip()])

@router.post("/fetch_offers")
async def fetch_offers(data: FetchRequest, req: Request, u=Depends(get_current_user_raw)):
    results = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in data.node_ids:
            try:
                url = f"https://funpay.com/lots/offerEdit?node={node}"
                async with session.get(url, headers=headers, cookies={"golden_key": data.golden_key}) as resp:
                    if "login" in str(resp.url): return {"success": False, "message": "Сессия невалидна"}
                    html = await resp.text()
                    oid, name = get_lot_info(html)
                    if oid: results.append({"node_id": node, "offer_id": oid, "name": name, "valid": True})
                    else: results.append({"node_id": node, "valid": False, "error": "OfferID не найден"})
            except: results.append({"node_id": node, "valid": False, "error": "Ошибка сети"})
            await asyncio.sleep(0.3)
    return {"success": True, "data": results}

@router.post("/set")
async def save_config(data: RestockSettings, req: Request, u=Depends(get_current_user_raw)):
    async with req.app.state.pool.acquire() as conn:
        current = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1", u['uid'])
        existing_pools = {}
        if current and current['lots_config']:
            for l in json.loads(current['lots_config']): existing_pools[l['offer_id']] = l.get('secrets_pool', [])
        
        final_lots = []
        for nl in data.lots:
            pool = existing_pools.get(nl.offer_id, []) + [s.strip() for s in nl.add_secrets if s.strip()]
            final_lots.append({"node_id": nl.node_id, "offer_id": nl.offer_id, "name": nl.name, "min_qty": nl.min_qty, "secrets_pool": pool})

        enc = encrypt_data(data.golden_key)
        await conn.execute("""
            INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
            VALUES ($1, $2, $3, $4, NOW(), 'Обновлено')
            ON CONFLICT (user_uid) DO UPDATE SET encrypted_golden_key=EXCLUDED.encrypted_golden_key, is_active=EXCLUDED.is_active, 
            lots_config=EXCLUDED.lots_config, status_message='Настройки сохранены'
        """, u['uid'], enc, data.active, json.dumps(final_lots))
    return {"success": True}

@router.get("/status")
async def get_status(req: Request, u=Depends(get_current_user_raw)):
    async with req.app.state.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1", u['uid'])
    if not r: return {"active": False, "message": "Не настроено", "lots": []}
    lots = []
    if r['lots_config']:
        for d in json.loads(r['lots_config']):
            lots.append({"node_id": d['node_id'], "offer_id": d['offer_id'], "name": d.get('name', 'Лот'), "min_qty": d['min_qty'], "keys_in_db": len(d['secrets_pool'])})
    return {"active": r['is_active'], "message": r['status_message'], "lots": lots}

async def worker(app):
    await asyncio.sleep(10)
    print(">>> [AutoRestock] WORKER STARTED", flush=True)
    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            async with app.state.pool.acquire() as conn:
                tasks = await conn.fetch("SELECT * FROM autorestock_tasks WHERE is_active=TRUE AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')")
            
            for t in tasks:
                uid = t['user_uid']
                key = decrypt_data(t['encrypted_golden_key'])
                lots = json.loads(t['lots_config'])
                is_changed = False
                log = []
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                    for lot in lots:
                        if not lot['secrets_pool']: continue
                        edit_url = f"https://funpay.com/lots/offerEdit?node={lot['node_id']}"
                        async with session.get(edit_url, cookies={"golden_key": key}) as r:
                            html = await r.text()
                            csrf = re.search(r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html)
                            m_text = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
                            if not csrf or not m_text: continue
                            
                            cur_txt = html_lib.unescape(m_text.group(1))
                            if count_items(cur_txt) < lot['min_qty']:
                                to_add = lot['secrets_pool'][:50] # Добавляем пачками по 50
                                lot['secrets_pool'] = lot['secrets_pool'][50:]
                                new_secrets = cur_txt.strip() + "\n" + "\n".join(to_add)
                                payload = {"csrf_token": csrf.group(1), "offer_id": lot['offer_id'], "node_id": lot['node_id'], "auto_delivery": "on", "secrets": new_secrets, "active": "on", "save": "Сохранить"}
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies={"golden_key": key}, headers={"Referer": edit_url}) as pr:
                                    if pr.status == 200:
                                        log.append(f"✅ {lot['node_id']}: +{len(to_add)}")
                                        is_changed = True
                if is_changed:
                    async with app.state.pool.acquire() as conn: await conn.execute("UPDATE autorestock_tasks SET lots_config=$1 WHERE user_uid=$2", json.dumps(lots), uid)
                await update_status(app.state.pool, uid, ", ".join(log) if log else "✅ Ожидание")
            await asyncio.sleep(60)
        except: traceback.print_exc(); await asyncio.sleep(10)
