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

# --- МОДЕЛИ ДАННЫХ ---
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

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def update_status(pool, uid, msg):
    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", str(msg)[:100], uid)
    except: pass

def parse_lot_data(html: str):
    """
    Улучшенный парсинг, аналогичный AutoRestockCore.cs.
    1. Ищет offer_id.
    2. Ищет название в RU поле.
    3. Если нет - в EN поле.
    4. Если нет - в заголовке H1.
    """
    offer_id = None
    name = None
    
    # 1. Offer ID
    # Ищем <input type="hidden" name="offer_id" value="123">
    m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', html)
    if not m_oid:
        # Иногда атрибуты меняются местами
        m_oid = re.search(r'value=["\'](\d+)["\'][^>]*name=["\']offer_id["\']', html)
    
    if m_oid: offer_id = m_oid.group(1)
    
    # 2. Название (RU) - Приоритет 1
    m_ru = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', html)
    if m_ru: 
        name = html_lib.unescape(m_ru.group(1))
    
    # 3. Название (EN) - Приоритет 2 (как в C# плагине)
    if not name:
        m_en = re.search(r'name=["\']fields\[summary\]\[en\]["\'][^>]*value=["\']([^"\']+)["\']', html)
        if m_en:
            name = html_lib.unescape(m_en.group(1))
            
    # 4. Fallback (H1) - если поля скрыты или изменилась верстка
    if not name:
        m_h1 = re.search(r'<div[^>]*class=["\']page-header["\'][^>]*>\s*<h1>(.*?)</h1>', html, re.DOTALL)
        if m_h1:
            raw = m_h1.group(1)
            # Удаляем теги внутри H1 (например, <small>)
            name = re.sub(r'<[^>]+>', '', raw).strip()
            name = html_lib.unescape(name)

    return offer_id, name or "Без названия"

def count_lines(text: str):
    if not text: return 0
    return len([l for l in text.split('\n') if l.strip()])

# --- API ---

@router.post("/fetch_offers")
async def fetch_offers(data: FetchRequest, req: Request):
    """Принимает NodeID, возвращает OfferID и Имя (для UI)"""
    results = []
    # Используем хедеры как у обычного браузера
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in data.node_ids:
            try:
                node = str(node).strip()
                if not node.isdigit(): continue
                
                async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=headers, cookies={"golden_key": data.golden_key}) as resp:
                    if "login" in str(resp.url): 
                        # Если редирект на логин - ключ невалиден, останавливаемся сразу
                        return {"success": False, "message": "Golden Key невалиден"}
                    
                    html = await resp.text()
                    oid, name = parse_lot_data(html)
                    
                    if oid:
                        results.append({"node_id": node, "offer_id": oid, "name": name, "valid": True})
                    else:
                        # Если не нашли OfferID, возможно это не тот тип лота или ошибка доступа
                        results.append({"node_id": node, "valid": False, "error": "Не найден OfferID"})
            except:
                results.append({"node_id": node, "valid": False, "error": "Ошибка сети"})
            
            await asyncio.sleep(0.5) # Небольшая задержка
            
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(data: RestockSettings, req: Request, u=Depends(get_current_user_raw)):
    async with req.app.state.pool.acquire() as conn:
        # 1. Получаем старые ключи
        current = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1", u['uid'])
        existing_pools = {} 
        
        if current and current['lots_config']:
            try:
                old_list = json.loads(current['lots_config'])
                for l in old_list: existing_pools[str(l.get('offer_id'))] = l.get('secrets_pool', [])
            except: pass
            
        # 2. Формируем новый конфиг
        final_lots = []
        for new_lot in data.lots:
            oid = str(new_lot.offer_id)
            old_keys = existing_pools.get(oid, [])
            new_keys = [k.strip() for k in new_lot.add_secrets if k.strip()]
            
            final_lots.append({
                "node_id": new_lot.node_id,
                "offer_id": oid,
                "name": new_lot.name,
                "min_qty": new_lot.min_qty,
                "secrets_pool": old_keys + new_keys 
            })

        # 3. Сохраняем
        enc = encrypt_data(data.golden_key)
        await conn.execute("""
            INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
            VALUES ($1, $2, $3, $4, NOW(), 'Обновлено')
            ON CONFLICT (user_uid) DO UPDATE SET
            encrypted_golden_key = EXCLUDED.encrypted_golden_key,
            is_active = EXCLUDED.is_active,
            lots_config = EXCLUDED.lots_config,
            status_message = 'Конфиг сохранен'
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
                    "name": l.get('name', '???'),
                    "min_qty": l['min_qty'],
                    "keys_in_db": len(l.get('secrets_pool', []))
                })
        except: pass
            
    return {"active": r['is_active'], "message": r['status_message'], "lots": display_lots}

# --- WORKER ---
async def worker(app):
    await asyncio.sleep(5)
    print(">>> [AutoRestock] WORKER STARTED", flush=True)
    
    # Для редактирования нужен XMLHttpRequest
    EDIT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest"
    }
    
    # Для просмотра страницы нужен обычный заголовок
    VIEW_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    
    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            
            async with app.state.pool.acquire() as conn:
                tasks = await conn.fetch("SELECT * FROM autorestock_tasks WHERE is_active = TRUE AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')")
            
            if not tasks: await asyncio.sleep(10); continue
            
            async with aiohttp.ClientSession() as session:
                for t in tasks:
                    uid = t['user_uid']
                    try:
                        key = decrypt_data(t['encrypted_golden_key'])
                        lots = json.loads(t['lots_config'])
                        is_changed = False
                        log_msg = []
                        
                        cookies = {"golden_key": key}

                        for lot in lots:
                            pool = lot.get('secrets_pool', [])
                            if not pool: continue 
                            
                            node = lot['node_id']
                            offer = lot['offer_id']
                            min_q = lot['min_qty']
                            
                            # 1. Загружаем редактор (Как браузер)
                            url = f"https://funpay.com/lots/offerEdit?node={node}"
                            async with session.get(url, headers=VIEW_HEADERS, cookies=cookies) as r:
                                html = await r.text()
                                
                            # 2. Парсим
                            csrf, _ = parse_lot_data(html) # Используем наш умный парсер для токенов тоже
                            if not csrf:
                                # Fallback поиск токена
                                m_csrf = re.search(r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html)
                                if m_csrf: csrf = m_csrf.group(1)
                            
                            if not csrf: 
                                log_msg.append(f"⚠️ {node}: нет токена")
                                continue
                            
                            m_text = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
                            current_text = html_lib.unescape(m_text.group(1)) if m_text else ""
                            current_qty = count_lines(current_text)
                            
                            # 3. Проверяем остаток
                            if current_qty < min_q:
                                to_add = pool[:50]
                                remaining = pool[50:]
                                
                                new_content = current_text.strip() + "\n" + "\n".join(to_add)
                                
                                # 4. Сохраняем (Как AJAX)
                                payload = {
                                    "csrf_token": csrf,
                                    "offer_id": offer,
                                    "node_id": node,
                                    "secrets": new_content,
                                    "auto_delivery": "on",
                                    "active": "on",
                                    "save": "Сохранить"
                                }
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=EDIT_HEADERS) as pr:
                                    if pr.status == 200:
                                        log_msg.append(f"✅ {node}: +{len(to_add)}")
                                        lot['secrets_pool'] = remaining
                                        is_changed = True
                                    else:
                                        log_msg.append(f"❌ {node}: {pr.status}")
                            
                            await asyncio.sleep(random.uniform(1, 3))

                        if is_changed:
                            async with app.state.pool.acquire() as c:
                                await c.execute("UPDATE autorestock_tasks SET lots_config=$1 WHERE user_uid=$2", json.dumps(lots), uid)
                        
                        status = ", ".join(log_msg) if log_msg else "✅ Проверка завершена"
                        await update_status(app.state.pool, uid, status)
                        
                    except Exception as ex: 
                        print(f"Error task {uid}: {ex}")
                        await update_status(app.state.pool, uid, "Ошибка выполнения")
                    
            await asyncio.sleep(5)
        except: await asyncio.sleep(10)
