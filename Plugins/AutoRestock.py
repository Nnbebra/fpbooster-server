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

# Модель для получения деталей
class FetchRequest(BaseModel):
    golden_key: str
    node_ids: list[str]

class LotConfig(BaseModel):
    node_id: str
    offer_id: str | None = None
    name: str | None = None
    min_qty: int
    add_secrets: list[str] = [] 

class RestockSettings(BaseModel):
    golden_key: str
    active: bool
    lots: list[LotConfig]

# --- DB HELPERS ---
async def update_status(pool, uid, msg):
    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", str(msg)[:150], uid)
    except: pass

# --- PARSERS ---
def get_tokens_and_info(html: str):
    csrf = None
    offer_id = None
    current_secrets = ""
    lot_name = "Без названия"

    # CSRF
    m = re.search(r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html)
    if m: csrf = m.group(1)
    
    # Offer ID
    m = re.search(r'name=["\']offer_id["\'][^>]+value=["\'](\d+)["\']', html)
    if m: offer_id = m.group(1)
    
    # Название лота (обычно в value инпута fields[summary][ru])
    m_name = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', html)
    if m_name: 
        lot_name = html_lib.unescape(m_name.group(1))
    else:
        # Попытка найти в заголовке
        m_h1 = re.search(r'<h1[^>]*>(.*?)</h1>', html)
        if m_h1: lot_name = re.sub('<[^<]+?>', '', m_h1.group(1)).strip()

    # Текущие товары
    m_text = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    if m_text: current_secrets = html_lib.unescape(m_text.group(1))
    
    return csrf, offer_id, current_secrets, lot_name

def count_items(text: str) -> int:
    if not text or not text.strip(): return 0
    return len([line for line in text.split('\n') if line.strip()])

# --- API ---
async def get_plugin_user(request: Request): return await get_current_user_raw(request.app, request)

@router.post("/fetch_offers")
async def fetch_offers(data: FetchRequest, req: Request, u=Depends(get_plugin_user)):
    """Проверяет NodeID и возвращает OfferID и название лота."""
    results = []
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    cookies = {"golden_key": data.golden_key}

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in data.node_ids:
            try:
                node = str(node).strip()
                if not node.isdigit(): continue
                
                url = f"https://funpay.com/lots/offerEdit?node={node}"
                async with session.get(url, headers=headers, cookies=cookies) as resp:
                    if resp.status != 200:
                        results.append({"node_id": node, "error": "Ошибка доступа"})
                        continue
                    
                    html = await resp.text()
                    if "login" in str(resp.url):
                        return {"success": False, "message": "Golden Key невалиден"}

                    _, offer_id, _, name = get_tokens_and_info(html)
                    
                    if offer_id:
                        results.append({
                            "node_id": node,
                            "offer_id": offer_id,
                            "name": name,
                            "valid": True
                        })
                    else:
                        results.append({"node_id": node, "valid": False, "error": "Не найден OfferID"})
            except:
                results.append({"node_id": node, "valid": False, "error": "Ошибка сети"})
            
            await asyncio.sleep(0.5) # Пауза чтобы не забанило при чеке

    return {"success": True, "data": results}

@router.post("/set")
async def save_config(data: RestockSettings, req: Request, u=Depends(get_plugin_user)):
    async with req.app.state.pool.acquire() as conn:
        # Получаем старый конфиг для сохранения ключей
        current_row = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1", u['uid'])
        existing_keys = {}
        
        if current_row and current_row['lots_config']:
            try:
                old_list = json.loads(current_row['lots_config'])
                for l in old_list:
                    # Ключ мапы - offer_id (так надежнее)
                    oid = str(l.get('offer_id') or l.get('node_id'))
                    existing_keys[oid] = l.get('secrets_pool', [])
            except: pass

        final_lots = []
        for new_lot in data.lots:
            # Определяем уникальный ID (предпочтительно offer_id)
            uid_key = str(new_lot.offer_id) if new_lot.offer_id else str(new_lot.node_id)
            
            old_pool = existing_keys.get(uid_key, [])
            new_pool = [k.strip() for k in new_lot.add_secrets if k.strip()]
            
            final_lots.append({
                "node_id": new_lot.node_id,
                "offer_id": new_lot.offer_id,
                "name": new_lot.name,
                "min_qty": new_lot.min_qty,
                "secrets_pool": old_pool + new_pool
            })

        enc = encrypt_data(data.golden_key)
        await conn.execute("""
            INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
            VALUES ($1, $2, $3, $4, NOW(), 'Обновлено')
            ON CONFLICT (user_uid) DO UPDATE SET
            encrypted_golden_key = EXCLUDED.encrypted_golden_key,
            is_active = EXCLUDED.is_active,
            lots_config = EXCLUDED.lots_config,
            status_message = 'Конфиг обновлен'
        """, u['uid'], enc, data.active, json.dumps(final_lots))
        
    return {"success": True, "message": "Сохранено"}

@router.get("/status")
async def get_status(req: Request, u=Depends(get_plugin_user)):
    async with req.app.state.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1", u['uid'])
    
    if not r: return {"active": False, "message": "Не настроено", "lots": []}
    
    lots_info = []
    if r['lots_config']:
        try:
            data = json.loads(r['lots_config'])
            for d in data:
                lots_info.append({
                    "node_id": d.get("node_id"),
                    "offer_id": d.get("offer_id"),
                    "name": d.get("name", "Лот"),
                    "min_qty": d.get("min_qty"),
                    "keys_in_db": len(d.get("secrets_pool", []))
                })
        except: pass

    return {"active": r['is_active'], "message": r['status_message'], "lots": lots_info}

# --- WORKER ---
async def worker(app):
    await asyncio.sleep(10)
    print(">>> [AutoRestock] WORKER STARTED (OfferID Support)", flush=True)
    
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "X-Requested-With": "XMLHttpRequest"}

    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            pool = app.state.pool
            
            tasks = []
            async with pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT user_uid, encrypted_golden_key, lots_config 
                    FROM autorestock_tasks 
                    WHERE is_active = TRUE 
                    AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours') 
                """) # Интервал 2 часа (7200 сек) из вашего запроса

            if not tasks: await asyncio.sleep(10); continue

            for task in tasks:
                uid = task['user_uid']
                try:
                    lots_config = json.loads(task['lots_config'])
                    key = decrypt_data(task['encrypted_golden_key'])
                    cookies = {"golden_key": key}
                    
                    is_modified = False
                    log = []
                    
                    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                        for lot in lots_config:
                            node_id = str(lot.get('node_id'))
                            offer_id = str(lot.get('offer_id', '')) # Теперь используем OfferID
                            pool = lot.get('secrets_pool', [])
                            min_qty = int(lot.get('min_qty', 0))
                            
                            if not pool: continue # Нечего заливать
                            
                            # Если OfferID нет, пробуем получить его на лету (fallback)
                            if not offer_id:
                                async with session.get(f"https://funpay.com/lots/offerEdit?node={node_id}", headers=HEADERS, cookies=cookies) as r:
                                    _, offer_id, _, _ = get_tokens_and_info(await r.text())
                            
                            if not offer_id:
                                log.append(f"❌ {node_id}: Нет OfferID")
                                continue

                            # Заходим в редактор
                            edit_url = f"https://funpay.com/lots/offerEdit?node={node_id}"
                            async with session.get(edit_url, headers=HEADERS, cookies=cookies) as r:
                                html = await r.text()
                            
                            csrf, _, current_text, _ = get_tokens_and_info(html)
                            curr_qty = count_items(current_text)
                            
                            if curr_qty < min_qty:
                                needed = min_qty - curr_qty + 3
                                to_add = pool[:needed]
                                remaining = pool[needed:]
                                
                                new_text = current_text.strip() + "\n" + "\n".join(to_add)
                                
                                # Сохраняем
                                payload = {
                                    "csrf_token": csrf,
                                    "offer_id": offer_id,
                                    "node_id": node_id,
                                    "auto_delivery": "on",
                                    "secrets": new_text,
                                    "active": "on",
                                    "save": "Сохранить"
                                }
                                
                                post_hdrs = HEADERS.copy(); post_hdrs["Referer"] = edit_url
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, headers=post_hdrs, cookies=cookies) as sv:
                                    if sv.status == 200:
                                        log.append(f"✅ {node_id}: +{len(to_add)}")
                                        lot['secrets_pool'] = remaining
                                        is_modified = True
                                    else:
                                        log.append(f"❌ {node_id}: Err {sv.status}")
                            
                            await asyncio.sleep(random.uniform(2, 5))

                    if is_modified:
                        async with app.state.pool.acquire() as c:
                            await c.execute("UPDATE autorestock_tasks SET lots_config=$1 WHERE user_uid=$2", json.dumps(lots_config), uid)
                    
                    msg = ", ".join(log) if log else "✅ Проверка завершена"
                    await update_status(app.state.pool, uid, msg)

                except Exception as e:
                    traceback.print_exc()
                    await update_status(app.state.pool, uid, "Ошибка воркера")

            await asyncio.sleep(5)
        except: await asyncio.sleep(10)
