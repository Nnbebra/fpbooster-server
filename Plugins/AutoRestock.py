import asyncio, re, html as html_lib, json, aiohttp, traceback, uuid, sys, os
from datetime import datetime
from typing import Dict, Any, List
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])
LOG_FILE = os.path.join(os.getcwd(), "restock_final_debug.log")

def log_debug(msg):
    try:
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg}\n")
        print(f"[AutoRestock] {msg}", flush=True)
    except: pass

def count_lines(text: str):
    return len([l for l in text.split('\n') if l.strip()])

def parse_edit_page(html: str):
    offer_id, secrets, csrf, node_id = None, "", None, None
    is_active, is_auto = False, False
    m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', html)
    if not m_oid: m_oid = re.search(r'value=["\'](\d+)["\'][^>]*name=["\']offer_id["\']', html)
    if m_oid: offer_id = m_oid.group(1)
    m_node = re.search(r'name=["\']node_id["\'][^>]*value=["\'](\d+)["\']', html)
    if m_node: node_id = m_node.group(1)
    m_sec = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    if m_sec: secrets = html_lib.unescape(m_sec.group(1))
    m_csrf = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', html)
    if not m_csrf: m_csrf = re.search(r'value=["\']([^"\']+)["\']', html)
    if m_csrf: csrf = m_csrf.group(1)
    if re.search(r'name=["\']active["\'][^>]*checked', html): is_active = True
    if re.search(r'name=["\']auto_delivery["\'][^>]*checked', html): is_auto = True
    return offer_id, secrets, csrf, is_active, is_auto, node_id

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
                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies={"golden_key": golden_key}) as r_edit:
                        ht = await r_edit.text()
                        m_nm = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', ht)
                        results.append({"node_id": node, "offer_id": oid, "name": html_lib.unescape(m_nm.group(1)) if m_nm else "Item", "valid": True})
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
        lots_data = body.get("lots") or []
        final_lots = []
        for lot in lots_data:
            final_lots.append({
                "node_id": str(lot.get('node_id', '')),
                "offer_id": str(lot.get('offer_id', '')),
                "name": str(lot.get('name', 'Lot')),
                "min_qty": int(lot.get('min_qty', 5)),
                "auto_enable": bool(lot.get('auto_enable', True)),
                "secrets_pool": [str(k).strip() for k in lot.get('add_secrets', []) if str(k).strip()]
            })
        enc = encrypt_data(body.get("golden_key", ""))
        async with req.app.state.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
                VALUES ($1, $2, $3, $4::jsonb, NULL, 'Настройки сохранены')
                ON CONFLICT (user_uid) DO UPDATE SET encrypted_golden_key=EXCLUDED.encrypted_golden_key, is_active=EXCLUDED.is_active,
                lots_config=EXCLUDED.lots_config, status_message='Обновлено', last_check_at=NULL
            """, uid_obj, enc, body.get("active", False), json.dumps(final_lots))
        return {"success": True, "message": "Настройки сохранены!"}
    except Exception as e: return JSONResponse(status_code=200, content={"success": False, "message": str(e)})

@router.get("/status")
async def get_status(req: Request):
    from auth.guards import get_current_user
    try:
        u = await get_current_user(req.app, req)
        async with req.app.state.pool.acquire() as conn:
            r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1", uuid.UUID(str(u['uid'])))
        if not r: return {"active": False, "message": "Не настроено", "lots": []}
        lots = json.loads(r['lots_config']) if isinstance(r['lots_config'], str) else r['lots_config']
        display = [{"node_id": l['node_id'], "offer_id": l['offer_id'], "name": l['name'], "min_qty": l['min_qty'], "auto_enable": l['auto_enable'], "keys_in_db": len(l['secrets_pool'])} for l in lots]
        return {"active": r['is_active'], "message": r['status_message'], "lots": display}
    except: return {"active": False, "message": "Error", "lots": []}

async def worker(app):
    await asyncio.sleep(10)
    from utils_crypto import decrypt_data
    HEADERS = {"User-Agent": "Mozilla/5.0"}
    POST_H = HEADERS.copy()
    POST_H["X-Requested-With"] = "XMLHttpRequest"
    while True:
        try:
            async with app.state.pool.acquire() as conn:
                tasks = await conn.fetch("SELECT * FROM autorestock_tasks WHERE is_active=TRUE AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')")
            if tasks:
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                    for t in tasks:
                        uid = t['user_uid']
                        key = decrypt_data(t['encrypted_golden_key'])
                        lots_conf = json.loads(t['lots_config']) if isinstance(t['lots_config'], str) else t['lots_config']
                        is_changed = False
                        log_msg = []
                        for lot in lots_conf:
                            pool = lot.get('secrets_pool', [])
                            if not pool: continue
                            async with session.get(f"https://funpay.com/lots/offerEdit?offer={lot['offer_id']}", headers=HEADERS, cookies={"golden_key": key}) as r:
                                html = await r.text()
                            oid, current_text, csrf, is_act, is_aut, real_node = parse_edit_page(html)
                            if not csrf: continue
                            
                            # ЛОГИКА ЗАМЕНЫ: Если товар не совпадает с софтом - полная замена
                            current_lines = [l.strip() for l in current_text.split('\n') if l.strip()]
                            if current_lines and current_lines[0] != pool[0]:
                                log_debug(f"[{uid}] Смена товара в {lot['offer_id']}. Очищаю и заливаю новое.")
                                current_lines = []

                            # ЛОГИКА ПОПОЛНЕНИЯ И РАЗМНОЖЕНИЯ
                            target_qty = int(lot['min_qty'])
                            if len(current_lines) < target_qty:
                                needed = target_qty - len(current_lines)
                                # Размножаем ссылку из пула до нужного количества
                                to_add = []
                                while len(to_add) < needed:
                                    to_add.extend(pool[:(needed - len(to_add))])
                                    if not pool: break
                                payload = {
                                    "csrf_token": csrf, "offer_id": oid, "node_id": real_node or lot['node_id'],
                                    "secrets": "\n".join(current_lines + to_add), "auto_delivery": "on" if lot['auto_enable'] else ("on" if is_aut else ""),
                                    "active": "on" if is_act else "", "save": "Сохранить"
                                }
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies={"golden_key": key}, headers=POST_H) as pr:
                                    if pr.status == 200:
                                        log_msg.append(f"✅{lot['offer_id']}:{target_qty}")
                                        is_changed = True
                            await asyncio.sleep(2)
                        status = ", ".join(log_msg) if log_msg else "✅ Проверено"
                        async with app.state.pool.acquire() as c:
                            await c.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", status[:100], uid)
            await asyncio.sleep(20)
        except Exception as e: log_debug(f"Worker Err: {e}"); await asyncio.sleep(30)
