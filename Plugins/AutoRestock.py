import asyncio
import re
import html as html_lib
import json
import aiohttp
import traceback
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
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", str(msg)[:100], uid)
    except: pass

def count_lines(text: str):
    if not text: return 0
    return len([l for l in text.split('\n') if l.strip()])

def parse_edit_page(html: str):
    offer_id = None
    name = "Без названия"
    m_oid = re.search(r'name=["\']offer_id["\'][^>]+value=["\'](\d+)["\']', html)
    if m_oid: offer_id = m_oid.group(1)
    
    m_name = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', html)
    if m_name: name = html_lib.unescape(m_name.group(1))
    else:
        m_en = re.search(r'name=["\']fields\[summary\]\[en\]["\'][^>]*value=["\']([^"\']+)["\']', html)
        if m_en: name = html_lib.unescape(m_en.group(1))
        
    m_sec = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    secrets = html_lib.unescape(m_sec.group(1)) if m_sec else ""
    
    m_csrf = re.search(r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html)
    csrf = m_csrf.group(1) if m_csrf else None
    
    active = 'name="active" checked' in html
    auto = 'name="auto_delivery" checked' in html
    
    return offer_id, name, secrets, csrf, active, auto

async def get_user_id(session, headers, cookies):
    try:
        async with session.get("https://funpay.com/", headers=headers, cookies=cookies) as r:
            html = await r.text()
            m = re.search(r'href="https://funpay.com/users/(\d+)/"', html)
            if m: return m.group(1)
            
            m_json = re.search(r'data-app-data="([^"]+)"', html)
            if m_json:
                d = json.loads(html_lib.unescape(m_json.group(1)))
                if d.get("userId"): return str(d.get("userId"))
    except: pass
    return None

@router.post("/fetch_offers")
async def fetch_offers(data: FetchRequest, req: Request):
    results = []
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    cookies = {"golden_key": data.golden_key}
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        user_id = await get_user_id(session, HEADERS, cookies)
        if not user_id: return {"success": False, "message": "Не удалось определить UserID"}

        for node in data.node_ids:
            node = str(node).strip()
            if not node.isdigit(): continue
            try:
                # Фильтруем таблицу по юзеру!
                trade_url = f"https://funpay.com/lots/{node}/trade?user={user_id}"
                async with session.get(trade_url, headers=HEADERS, cookies=cookies) as resp:
                    if "login" in str(resp.url): return {"success": False, "message": "Login Error"}
                    html = await resp.text()

                found_ids = set(re.findall(r'offerEdit\?offer=(\d+)', html))
                
                # Fallback для одиночных лотов
                if not found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=HEADERS, cookies=cookies) as r2:
                        h2 = await r2.text()
                        oid, name, _, _, _, _ = parse_edit_page(h2)
                        if oid: found_ids.add(oid)

                if not found_ids:
                    results.append({"node_id": node, "valid": False, "error": "Лоты не найдены"})
                    continue

                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies=cookies) as r3:
                        oid_real, name, _, _, _, _ = parse_edit_page(await r3.text())
                        if oid_real:
                            results.append({"node_id": node, "offer_id": oid_real, "name": name, "valid": True})
                    await asyncio.sleep(0.1)
            except Exception as e:
                results.append({"node_id": node, "valid": False, "error": str(e)[:20]})
            await asyncio.sleep(0.5)
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(data: RestockSettings, req: Request, u=Depends(get_current_user_raw)):
    async with req.app.state.pool.acquire() as conn:
        current = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1", u['uid'])
        existing_pools = {}
        if current and current['lots_config']:
            try:
                for l in json.loads(current['lots_config']): existing_pools[str(l['offer_id'])] = l.get('secrets_pool', [])
            except: pass
        
        final_lots = []
        for nl in data.lots:
            oid = str(nl.offer_id)
            pool = existing_pools.get(oid, []) + [s.strip() for s in nl.add_secrets if s.strip()]
            final_lots.append({"node_id": nl.node_id, "offer_id": oid, "name": nl.name, "min_qty": nl.min_qty, "secrets_pool": pool})

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
        for l in json.loads(r['lots_config']):
            lots.append({"node_id": l['node_id'], "offer_id": l['offer_id'], "name": l.get('name', 'Лот'), "min_qty": l['min_qty'], "keys_in_db": len(l['secrets_pool'])})
    return {"active": r['is_active'], "message": r['status_message'], "lots": lots}

async def worker(app):
    await asyncio.sleep(5)
    print(">>> [AutoRestock] WORKER STARTED", flush=True)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    
    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            async with app.state.pool.acquire() as conn:
                tasks = await conn.fetch("SELECT * FROM autorestock_tasks WHERE is_active=TRUE AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')")
            if not tasks: await asyncio.sleep(10); continue
            
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                for t in tasks:
                    uid = t['user_uid']
                    try:
                        key = decrypt_data(t['encrypted_golden_key'])
                        lots = json.loads(t['lots_config'])
                        is_changed = False
                        log = []
                        cookies = {"golden_key": key}
                        for lot in lots:
                            if not lot['secrets_pool']: continue
                            
                            # 1. Загружаем (обычный GET)
                            url = f"https://funpay.com/lots/offerEdit?offer={lot['offer_id']}"
                            async with session.get(url, headers=headers, cookies=cookies) as r:
                                html = await r.text()
                            
                            oid, _, txt, csrf, _, is_auto = parse_edit_page(html)
                            if not csrf or not is_auto: continue # Нет прав или нет автовыдачи
                            
                            # 2. Проверяем
                            if count_lines(txt) < lot['min_qty']:
                                to_add = lot['secrets_pool'][:50]
                                lot['secrets_pool'] = lot['secrets_pool'][50:]
                                new_txt = txt.strip() + "\n" + "\n".join(to_add)
                                
                                # 3. Сохраняем (AJAX)
                                payload = {"csrf_token": csrf, "offer_id": oid, "node_id": lot['node_id'], "secrets": new_txt, "auto_delivery": "on", "active": "on", "save": "Сохранить"}
                                post_h = headers.copy(); post_h["X-Requested-With"] = "XMLHttpRequest"; post_h["Referer"] = url
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, headers=post_h, cookies=cookies) as pr:
                                    if pr.status == 200:
                                        log.append(f"✅ {lot['offer_id']}: +{len(to_add)}")
                                        is_changed = True
                            await asyncio.sleep(1)
                        
                        if is_changed:
                            async with app.state.pool.acquire() as c:
                                await c.execute("UPDATE autorestock_tasks SET lots_config=$1 WHERE user_uid=$2", json.dumps(lots), uid)
                        await update_status(app.state.pool, uid, ", ".join(log) if log else "✅ ОК")
                    except: await update_status(app.state.pool, uid, "Ошибка")
            await asyncio.sleep(5)
        except: await asyncio.sleep(5)
