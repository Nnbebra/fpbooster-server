import asyncio
import re
import html as html_lib
import json
import aiohttp
import traceback
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
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

# --- HELPERS ---
async def update_status(pool, uid, msg):
    try:
        async with pool.acquire() as conn:
            # Обрезаем сообщение, чтобы не переполнить поле
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", str(msg)[:100], uid)
    except: pass

def count_lines(text: str):
    if not text: return 0
    return len([l for l in text.split('\n') if l.strip()])

def parse_edit_page(html: str):
    """
    Парсит страницу offerEdit.
    Находит: ID, Название, Текущие товары, CSRF, Галочки.
    """
    offer_id = None
    name = "Без названия"
    secrets = ""
    csrf = None
    is_active = False
    is_auto = False
    
    # 1. Offer ID
    m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', html)
    if not m_oid: m_oid = re.search(r'value=["\'](\d+)["\'][^>]*name=["\']offer_id["\']', html)
    if m_oid: offer_id = m_oid.group(1)
    
    # 2. Название (RU > EN > Input)
    m_name = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', html)
    if m_name: name = html_lib.unescape(m_name.group(1))
    else:
        m_en = re.search(r'name=["\']fields\[summary\]\[en\]["\'][^>]*value=["\']([^"\']+)["\']', html)
        if m_en: name = html_lib.unescape(m_en.group(1))
        
    # 3. Secrets (Textarea)
    m_sec = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    if m_sec: secrets = html_lib.unescape(m_sec.group(1))

    # 4. CSRF
    m_csrf = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', html)
    if not m_csrf: m_csrf = re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']csrf_token["\']', html)
    if m_csrf: csrf = m_csrf.group(1)

    # 5. Checkboxes (Активность и Автовыдача)
    if 'name="active" checked' in html or "name='active' checked" in html: is_active = True
    if 'name="auto_delivery" checked' in html or "name='auto_delivery' checked" in html: is_auto = True

    return offer_id, name, secrets, csrf, is_active, is_auto

async def ensure_table(pool):
    """Создает таблицу, если ее нет"""
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS autorestock_tasks (
                    user_uid UUID PRIMARY KEY,
                    encrypted_golden_key TEXT,
                    is_active BOOLEAN DEFAULT FALSE,
                    lots_config JSONB,
                    status_message TEXT,
                    last_check_at TIMESTAMP WITHOUT TIME ZONE
                );
            """)
            # Добавляем колонки, если это миграция со старой версии
            await conn.execute("ALTER TABLE autorestock_tasks ADD COLUMN IF NOT EXISTS lots_config JSONB;")
            await conn.execute("ALTER TABLE autorestock_tasks ADD COLUMN IF NOT EXISTS check_interval INTEGER DEFAULT 7200;")
    except Exception as e:
        print(f"DB Error: {e}")

# --- API ---

@router.post("/fetch_offers")
async def fetch_offers(data: FetchRequest, req: Request):
    """
    1. Заходит на /lots/{node}/trade
    2. Ищет ВСЕ ссылки редактирования (они видны только владельцу).
    3. Заходит внутрь каждого и парсит данные.
    """
    results = []
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
                # Шаг 1: Страница торгов
                trade_url = f"https://funpay.com/lots/{node}/trade"
                async with session.get(trade_url, headers=HEADERS, cookies=cookies) as resp:
                    if "login" in str(resp.url): return {"success": False, "message": "Golden Key невалиден"}
                    html_trade = await resp.text()

                # Шаг 2: Поиск ссылок offerEdit (только мои лоты)
                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html_trade))
                
                # Если список пуст, возможно это одиночный лот
                if not found_ids:
                    direct_url = f"https://funpay.com/lots/offerEdit?node={node}"
                    async with session.get(direct_url, headers=HEADERS, cookies=cookies) as r2:
                        h2 = await r2.text()
                        if "offer_id" in h2:
                            oid, name, _, _, _, _ = parse_edit_page(h2)
                            if oid: found_ids.add(oid)

                if not found_ids:
                    results.append({"node_id": node, "valid": False, "error": "Ваши лоты не найдены"})
                    continue

                # Шаг 3: Детальный парсинг
                for oid in found_ids:
                    edit_url = f"https://funpay.com/lots/offerEdit?offer={oid}"
                    async with session.get(edit_url, headers=HEADERS, cookies=cookies) as r_edit:
                        h_edit = await r_edit.text()
                        real_oid, real_name, _, _, _, _ = parse_edit_page(h_edit)
                        if real_oid:
                            results.append({"node_id": node, "offer_id": real_oid, "name": real_name, "valid": True})
                    await asyncio.sleep(0.1)
            except Exception as e:
                results.append({"node_id": node, "valid": False, "error": str(e)[:30]})
            await asyncio.sleep(0.5)
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(data: RestockSettings, req: Request, u=Depends(get_current_user_raw)):
    # Гарантируем таблицу
    await ensure_table(req.app.state.pool)

    try:
        async with req.app.state.pool.acquire() as conn:
            # --- БЛОК ЧТЕНИЯ СТАРЫХ КЛЮЧЕЙ (ЗАЩИЩЕННЫЙ) ---
            existing_pools = {}
            try:
                current = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1", u['uid'])
                if current and current['lots_config']:
                    # asyncpg может вернуть строку или уже объект. Обрабатываем оба варианта.
                    raw = current['lots_config']
                    loaded = json.loads(raw) if isinstance(raw, str) else raw
                    
                    if isinstance(loaded, list):
                        for l in loaded:
                            existing_pools[str(l.get('offer_id'))] = l.get('secrets_pool', [])
            except Exception as e:
                print(f"[AutoRestock] Warning reading old config: {e}")
                # Если не удалось прочитать старые, просто идем дальше (existing_pools останется пустым)
            # ----------------------------------------------------

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
            
            # Upsert в БД
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
    except Exception as e:
        print("!!! SERVER ERROR [AutoRestock Save]:")
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"success": False, "message": f"Server Error: {str(e)}"})

@router.get("/status")
async def get_status(req: Request, u=Depends(get_current_user_raw)):
    try:
        async with req.app.state.pool.acquire() as conn:
            try:
                r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1", u['uid'])
            except:
                return {"active": False, "message": "Не инициализировано", "lots": []}
        
        if not r: return {"active": False, "message": "Не настроено", "lots": []}
        
        display_lots = []
        if r['lots_config']:
            try:
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
            except: pass
                
        return {"active": r['is_active'], "message": r['status_message'], "lots": display_lots}
    except:
        return {"active": False, "message": "Ошибка сервера", "lots": []}

# --- WORKER ---
async def worker(app):
    await asyncio.sleep(5)
    print(">>> [AutoRestock] WORKER STARTED", flush=True)
    if hasattr(app.state, 'pool'): await ensure_table(app.state.pool)

    # Используем заголовки как у браузера, но для AJAX - XMLHttpRequest
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "X-Requested-With": "XMLHttpRequest"}
    # Для GET (HTML) убираем X-Requested-With
    GET_HEADERS = {k:v for k,v in HEADERS.items() if k != "X-Requested-With"}
    
    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            
            async with app.state.pool.acquire() as conn:
                try:
                    tasks = await conn.fetch("SELECT * FROM autorestock_tasks WHERE is_active = TRUE AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '1 hour')")
                except: tasks = []

            if not tasks: await asyncio.sleep(10); continue
            
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                for t in tasks:
                    uid = t['user_uid']
                    try:
                        key = decrypt_data(t['encrypted_golden_key'])
                        
                        raw = t['lots_config']
                        lots = json.loads(raw) if isinstance(raw, str) else raw
                        if not isinstance(lots, list): lots = []

                        is_changed = False
                        log_msg = []
                        cookies = {"golden_key": key}

                        for lot in lots:
                            pool = lot.get('secrets_pool', [])
                            if not pool: continue
                            
                            offer_id = lot['offer_id']
                            node_id = lot['node_id']
                            min_q = lot['min_qty']
                            
                            # 1. Загружаем страницу (GET)
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=GET_HEADERS, cookies=cookies) as r:
                                html = await r.text()
                                
                            oid, _, cur_text, csrf, is_active, is_auto = parse_edit_page(html)
                            
                            if not csrf: 
                                log_msg.append(f"⚠️ {lot['node_id']}: нет доступа")
                                continue
                            
                            # 2. Проверяем галочку авто-выдачи
                            if not is_auto: 
                                continue 
                            
                            # 3. Проверяем количество
                            if count_lines(cur_text) < min_q:
                                to_add = pool[:50]
                                lot['secrets_pool'] = pool[50:]
                                new_text = cur_text.strip() + "\n" + "\n".join(to_add)
                                
                                # 4. Сохраняем (POST)
                                payload = {
                                    "csrf_token": csrf, "offer_id": oid, "node_id": node_id, "secrets": new_text,
                                    "auto_delivery": "on", "active": "on" if is_active else "", "save": "Сохранить"
                                }
                                if not is_active: payload.pop("active", None)
                                
                                post_h = HEADERS.copy(); post_h["Referer"] = edit_url
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=post_h) as pr:
                                    if pr.status == 200:
                                        log_msg.append(f"✅ {lot['node_id']}: +{len(to_add)}")
                                        is_changed = True
                                    else:
                                        log_msg.append(f"❌ {lot['node_id']}: {pr.status}")
                            
                            await asyncio.sleep(2)

                        if is_changed:
                            async with app.state.pool.acquire() as c:
                                await c.execute("UPDATE autorestock_tasks SET lots_config=$1 WHERE user_uid=$2", json.dumps(lots), uid)
                        
                        status = ", ".join(log_msg) if log_msg else "✅ Проверено"
                        await update_status(app.state.pool, uid, status)
                        
                    except Exception as e:
                        traceback.print_exc()
                        await update_status(app.state.pool, uid, "Ошибка")
            await asyncio.sleep(5)
        except: await asyncio.sleep(5)
