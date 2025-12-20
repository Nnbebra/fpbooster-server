import asyncio
import re
import html as html_lib
import json
import aiohttp
import traceback
import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

# Настройка логгера в файл, чтобы не потерять ошибку
logging.basicConfig(
    filename='restock_error.log', 
    level=logging.ERROR, 
    format='%(asctime)s %(levelname)s: %(message)s'
)

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
async def ensure_table_exists(pool):
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS autorestock_tasks (
                    user_uid UUID PRIMARY KEY,
                    encrypted_golden_key TEXT,
                    is_active BOOLEAN DEFAULT FALSE,
                    check_interval INTEGER DEFAULT 7200,
                    lots_config JSONB,
                    status_message TEXT,
                    last_check_at TIMESTAMP WITHOUT TIME ZONE
                );
            """)
    except: pass

async def update_status(pool, uid, msg):
    try:
        async with pool.acquire() as conn:
            # Принудительно кастим UUID
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2::uuid", str(msg)[:100], str(uid))
    except: pass

def count_lines(text: str):
    if not text: return 0
    return len([l for l in text.split('\n') if l.strip()])

def parse_edit_page(html: str):
    offer_id, name, secrets, csrf = None, "Без названия", "", None
    is_active, is_auto = False, False
    
    m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', html)
    if not m_oid: m_oid = re.search(r'value=["\'](\d+)["\'][^>]*name=["\']offer_id["\']', html)
    if m_oid: offer_id = m_oid.group(1)
    
    m_name = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', html)
    if m_name: name = html_lib.unescape(m_name.group(1))
    else:
        m_en = re.search(r'name=["\']fields\[summary\]\[en\]["\'][^>]*value=["\']([^"\']+)["\']', html)
        if m_en: name = html_lib.unescape(m_en.group(1))
        
    m_sec = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    if m_sec: secrets = html_lib.unescape(m_sec.group(1))

    m_csrf = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', html)
    if not m_csrf: m_csrf = re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']csrf_token["\']', html)
    if m_csrf: csrf = m_csrf.group(1)

    if 'name="active" checked' in html or "name='active' checked" in html: is_active = True
    if 'name="auto_delivery" checked' in html or "name='auto_delivery' checked" in html: is_auto = True

    return offer_id, name, secrets, csrf, is_active, is_auto

# --- API ---

@router.post("/fetch_offers")
async def fetch_offers(data: FetchRequest, req: Request):
    """Ищет офферы через страницу /trade"""
    results = []
    HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    cookies = {"golden_key": data.golden_key}
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in data.node_ids:
            node = str(node).strip()
            if not node.isdigit(): continue
            try:
                # 1. Загружаем таблицу
                async with session.get(f"https://funpay.com/lots/{node}/trade", headers=HEADERS, cookies=cookies) as resp:
                    if "login" in str(resp.url): return {"success": False, "message": "Golden Key невалиден"}
                    html = await resp.text()

                # 2. Ссылки (свои лоты)
                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html))
                
                # Fallback для одиночных
                if not found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=HEADERS, cookies=cookies) as r2:
                        h2 = await r2.text()
                        if "offer_id" in h2:
                            oid, name, _, _, _, _ = parse_edit_page(h2)
                            if oid: found_ids.add(oid)

                if not found_ids:
                    results.append({"node_id": node, "valid": False, "error": "Лоты не найдены"})
                    continue

                # 3. Детали
                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies=cookies) as r_edit:
                        oid_real, name, _, _, _, _ = parse_edit_page(await r_edit.text())
                        if oid_real:
                            results.append({"node_id": node, "offer_id": oid_real, "name": name, "valid": True})
                    await asyncio.sleep(0.1)
            except Exception as e:
                results.append({"node_id": node, "valid": False, "error": str(e)[:20]})
            await asyncio.sleep(0.5)
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(data: RestockSettings, req: Request, u=Depends(get_current_user_raw)):
    # Весь блок в try-except, чтобы поймать 500 ошибку
    try:
        pool = getattr(req.app.state, 'pool', None)
        if not pool: raise Exception("DB Pool is missing")

        await ensure_table_exists(pool)
        
        # Принудительно конвертируем UUID в строку для безопасности
        uid_str = str(u['uid'])

        async with pool.acquire() as conn:
            existing_pools = {}
            try:
                # Читаем старый конфиг. Используем ::uuid
                row = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1::uuid", uid_str)
                if row and row['lots_config']:
                    raw = row['lots_config']
                    # Обработка разных вариантов возврата JSON
                    loaded = json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(loaded, list):
                        for l in loaded:
                            existing_pools[str(l.get('offer_id'))] = l.get('secrets_pool', [])
            except Exception as e:
                logging.error(f"Read error: {e}")

            # Формируем новый список
            final_lots = []
            for nl in data.lots:
                oid = str(nl.offer_id)
                new_keys = [s.strip() for s in nl.add_secrets if s.strip()]
                pool_keys = existing_pools.get(oid, []) + new_keys
                
                final_lots.append({
                    "node_id": nl.node_id, 
                    "offer_id": oid, 
                    "name": nl.name, 
                    "min_qty": nl.min_qty, 
                    "secrets_pool": pool_keys
                })

            enc = encrypt_data(data.golden_key)
            
            # Сериализуем в JSON строку
            json_payload = json.dumps(final_lots)
            
            # ЗАПИСЬ: Используем явные приведения типов ::uuid и ::jsonb
            await conn.execute("""
                INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
                VALUES ($1::uuid, $2, $3, $4::jsonb, NOW(), 'Обновлено')
                ON CONFLICT (user_uid) DO UPDATE SET
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                is_active = EXCLUDED.is_active,
                lots_config = EXCLUDED.lots_config,
                status_message = 'Настройки сохранены'
            """, uid_str, enc, data.active, json_payload)
            
        return {"success": True}

    except Exception as e:
        # Логируем
        err_msg = f"SAVE ERROR: {str(e)}"
        print(err_msg)
        logging.error(err_msg + "\n" + traceback.format_exc())
        
        # ВАЖНО: Возвращаем 200, чтобы клиент показал текст ошибки, а не "Internal Server Error"
        return JSONResponse(
            status_code=200, 
            content={"success": False, "message": f"Server Err: {str(e)}"}
        )

@router.get("/status")
async def get_status(req: Request, u=Depends(get_current_user_raw)):
    try:
        pool = req.app.state.pool
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1::uuid", str(u['uid']))
        
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
    except Exception as e:
        return {"active": False, "message": f"Err: {str(e)}", "lots": []}

# --- WORKER ---
async def worker(app):
    await asyncio.sleep(5)
    print(">>> [AutoRestock] WORKER STARTED", flush=True)
    if hasattr(app.state, 'pool'): await ensure_table_exists(app.state.pool)

    HEADERS = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"}
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
                    uid = str(t['user_uid'])
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
                            
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=GET_HEADERS, cookies=cookies) as r:
                                html = await r.text()
                                
                            oid, _, cur_text, csrf, is_active, is_auto = parse_edit_page(html)
                            
                            if not csrf: 
                                log_msg.append(f"⚠️ {lot['node_id']}: нет доступа")
                                continue
                            if not is_auto: continue 
                            
                            if count_lines(cur_text) < min_q:
                                to_add = pool[:50]
                                lot['secrets_pool'] = pool[50:]
                                new_text = cur_text.strip() + "\n" + "\n".join(to_add)
                                
                                payload = {"csrf_token": csrf, "offer_id": oid, "node_id": node_id, "secrets": new_text, "auto_delivery": "on", "active": "on" if is_active else "", "save": "Сохранить"}
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
                                await c.execute("UPDATE autorestock_tasks SET lots_config=$1::jsonb WHERE user_uid=$2::uuid", json.dumps(lots), uid)
                        
                        await update_status(app.state.pool, uid, ", ".join(log_msg) if log_msg else "✅ Проверено")
                    except Exception as e:
                        traceback.print_exc()
                        await update_status(app.state.pool, uid, "Ошибка")
            await asyncio.sleep(5)
        except: await asyncio.sleep(5)
