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

# --- МОДЕЛИ ---
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

# --- HELPERS ---
async def update_status(pool, uid, msg):
    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", str(msg)[:100], uid)
    except: pass

def count_lines(text: str):
    if not text: return 0
    return len([l for l in text.split('\n') if l.strip()])

def parse_edit_page(html: str):
    """Парсит страницу редактирования лота (offerEdit)"""
    offer_id = None
    name = "Без названия"
    
    # Offer ID
    m_oid = re.search(r'name=["\']offer_id["\'][^>]+value=["\'](\d+)["\']', html)
    if m_oid: offer_id = m_oid.group(1)
    
    # Name (RU)
    m_name = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', html)
    if m_name: 
        name = html_lib.unescape(m_name.group(1))
    else:
        # Name (EN) - Fallback
        m_en = re.search(r'name=["\']fields\[summary\]\[en\]["\'][^>]*value=["\']([^"\']+)["\']', html)
        if m_en: name = html_lib.unescape(m_en.group(1))
        
    # Textarea (Secrets)
    secrets = ""
    m_sec = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    if m_sec: secrets = html_lib.unescape(m_sec.group(1))

    # CSRF
    csrf = None
    m_csrf = re.search(r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html)
    if m_csrf: csrf = m_csrf.group(1)

    return offer_id, name, secrets, csrf

# --- API ---

@router.post("/fetch_offers")
async def fetch_offers(data: FetchRequest, req: Request):
    """
    Умный поиск: 
    1. Заходит в категорию (Node).
    2. Ищет ссылки на редактирование (значит лот твой).
    3. Вытаскивает инфу по каждому лоту.
    """
    results = []
    
    # Заголовки как у браузера
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }
    cookies = {"golden_key": data.golden_key}
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in data.node_ids:
            node = str(node).strip()
            if not node.isdigit(): continue
            
            try:
                # 1. Заходим на страницу категории (Trade)
                trade_url = f"https://funpay.com/lots/{node}/trade"
                async with session.get(trade_url, headers=HEADERS, cookies=cookies) as resp:
                    if "login" in str(resp.url): 
                        return {"success": False, "message": "Golden Key невалиден (Login Redirect)"}
                    
                    html_trade = await resp.text()

                # 2. Ищем ссылки вида offerEdit?offer=12345
                # Это самый надежный способ найти СВОИ лоты в таблице
                found_offers = set(re.findall(r'offerEdit\?offer=(\d+)', html_trade))
                
                # Fallback: Если это одиночный лот (аккаунт), ссылок в таблице может не быть,
                # или редирект сразу на offerEdit. Попробуем прямой заход.
                if not found_offers:
                    direct_url = f"https://funpay.com/lots/offerEdit?node={node}"
                    async with session.get(direct_url, headers=HEADERS, cookies=cookies) as r2:
                        h2 = await r2.text()
                        oid, name, _, _ = parse_edit_page(h2)
                        if oid: found_offers.add(oid)

                if not found_offers:
                    results.append({"node_id": node, "valid": False, "error": "Лоты не найдены"})
                    continue

                # 3. Проходим по каждому найденному OfferID и берем детали
                for oid in found_offers:
                    edit_url = f"https://funpay.com/lots/offerEdit?offer={oid}"
                    async with session.get(edit_url, headers=HEADERS, cookies=cookies) as r_edit:
                        h_edit = await r_edit.text()
                        real_oid, real_name, _, _ = parse_edit_page(h_edit)
                        
                        if real_oid:
                            results.append({
                                "node_id": node,
                                "offer_id": real_oid,
                                "name": real_name,
                                "valid": True
                            })
                    await asyncio.sleep(0.2) # Небольшая пауза, чтобы не спамить

            except Exception as e:
                results.append({"node_id": node, "valid": False, "error": f"Ошибка: {str(e)[:30]}"})
                
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
            final_lots.append({
                "node_id": nl.node_id, 
                "offer_id": oid, 
                "name": nl.name, 
                "min_qty": nl.min_qty, 
                "secrets_pool": pool
            })

        enc = encrypt_data(data.golden_key)
        await conn.execute("""
            INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
            VALUES ($1, $2, $3, $4, NOW(), 'Обновлено')
            ON CONFLICT (user_uid) DO UPDATE SET
            encrypted_golden_key = EXCLUDED.encrypted_golden_key,
            is_active = EXCLUDED.is_active,
            lots_config = EXCLUDED.lots_config,
            status_message = 'Настройки сохранены'
        """, u['uid'], enc, data.active, json.dumps(final_lots))
        
    return {"success": True}

@router.get("/status")
async def get_status(req: Request, u=Depends(get_current_user_raw)):
    async with req.app.state.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1", u['uid'])
    
    if not r: return {"active": False, "message": "Не настроено", "lots": []}
    
    display_lots = []
    if r['lots_config']:
        try:
            for l in json.loads(r['lots_config']):
                display_lots.append({
                    "node_id": l['node_id'],
                    "offer_id": l['offer_id'],
                    "name": l.get('name', 'Лот'),
                    "min_qty": l['min_qty'],
                    "keys_in_db": len(l.get('secrets_pool', []))
                })
        except: pass
            
    return {"active": r['is_active'], "message": r['status_message'], "lots": display_lots}

# --- WORKER ---
async def worker(app):
    await asyncio.sleep(5)
    print(">>> [AutoRestock] WORKER STARTED (Deep Parsing)", flush=True)
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    
    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            
            async with app.state.pool.acquire() as conn:
                tasks = await conn.fetch("SELECT * FROM autorestock_tasks WHERE is_active = TRUE AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')")
            
            if not tasks: await asyncio.sleep(10); continue
            
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                for t in tasks:
                    uid = t['user_uid']
                    try:
                        key = decrypt_data(t['encrypted_golden_key'])
                        lots = json.loads(t['lots_config'])
                        is_changed = False
                        log_msg = []
                        cookies = {"golden_key": key}

                        for lot in lots:
                            if not lot.get('secrets_pool'): continue
                            
                            offer_id = lot['offer_id']
                            node_id = lot['node_id']
                            min_q = lot['min_qty']
                            
                            # 1. Загружаем Edit Page
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=headers, cookies=cookies) as r:
                                html = await r.text()
                                
                            real_oid, _, cur_text, csrf = parse_edit_page(html)
                            
                            if not csrf or not real_oid:
                                log_msg.append(f"⚠️ {offer_id}: Ошибка доступа")
                                continue
                                
                            cur_qty = count_lines(cur_text)
                            
                            # 2. Проверяем лимит
                            if cur_qty < min_q:
                                to_add = lot['secrets_pool'][:50]
                                remaining = lot['secrets_pool'][50:]
                                
                                new_text = cur_text.strip() + "\n" + "\n".join(to_add)
                                
                                # 3. Сохраняем
                                payload = {
                                    "csrf_token": csrf,
                                    "offer_id": offer_id,
                                    "node_id": node_id,
                                    "secrets": new_text,
                                    "auto_delivery": "on",
                                    "active": "on",
                                    "save": "Сохранить"
                                }
                                # Важно: X-Requested-With
                                post_hdrs = headers.copy()
                                post_hdrs["X-Requested-With"] = "XMLHttpRequest"
                                post_hdrs["Referer"] = edit_url
                                
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=post_hdrs) as pr:
                                    if pr.status == 200:
                                        log_msg.append(f"✅ {offer_id}: +{len(to_add)}")
                                        lot['secrets_pool'] = remaining
                                        is_changed = True
                                    else:
                                        log_msg.append(f"❌ {offer_id}: {pr.status}")
                            
                            await asyncio.sleep(random.uniform(2, 4))

                        if is_changed:
                            async with app.state.pool.acquire() as c:
                                await c.execute("UPDATE autorestock_tasks SET lots_config=$1 WHERE user_uid=$2", json.dumps(lots), uid)
                        
                        status = ", ".join(log_msg) if log_msg else "✅ Проверка завершена"
                        await update_status(app.state.pool, uid, status)
                        
                    except Exception as e:
                        traceback.print_exc()
                        await update_status(app.state.pool, uid, "Ошибка воркера")
                        
            await asyncio.sleep(5)
        except: await asyncio.sleep(5)
