import asyncio
import re
import html as html_lib
import json
import aiohttp
import traceback
import uuid
import sys
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])

# Флаг, чтобы не запустить воркер дважды
WORKER_STARTED = False

# --- ЛОГГЕР ---
def log(msg):
    t = datetime.now().strftime("%H:%M:%S")
    print(f"[{t}] [AutoRestock] {msg}", flush=True)

# --- ПАРСИНГ ---
def count_lines(text: str):
    if not text: return 0
    return len([l for l in text.split('\n') if l.strip()])

def parse_edit_page(html: str):
    """Парсим: ID, Секреты, CSRF, Активность, Галочку автовыдачи"""
    offer_id, secrets, csrf = None, "", None
    is_active, is_auto = False, False
    
    # 1. ID
    m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', html)
    if not m_oid: m_oid = re.search(r'value=["\'](\d+)["\'][^>]*name=["\']offer_id["\']', html)
    if m_oid: offer_id = m_oid.group(1)
    
    # 2. Товары
    m_sec = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    if m_sec: secrets = html_lib.unescape(m_sec.group(1))

    # 3. CSRF
    m_csrf = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', html)
    if not m_csrf: m_csrf = re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']csrf_token["\']', html)
    if m_csrf: csrf = m_csrf.group(1)

    # 4. Галочки (ищем слово checked внутри тега)
    # Пример: <input type="checkbox" name="auto_delivery" checked="">
    if re.search(r'name=["\']active["\'][^>]*checked', html): is_active = True
    if re.search(r'name=["\']auto_delivery["\'][^>]*checked', html): is_auto = True

    return offer_id, secrets, csrf, is_active, is_auto

# --- БД ---
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
            # Добавляем колонки, если их нет (миграция)
            try: await conn.execute("ALTER TABLE autorestock_tasks ADD COLUMN IF NOT EXISTS lots_config JSONB;")
            except: pass
            try: await conn.execute("ALTER TABLE autorestock_tasks ADD COLUMN IF NOT EXISTS check_interval INTEGER DEFAULT 7200;")
            except: pass
    except: pass

async def update_status(pool, uid_obj, msg):
    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2::uuid", str(msg)[:100], uid_obj)
    except: pass

# --- ВОРКЕР ---
async def background_worker(pool):
    log("Воркер запущен в фоне.")
    await ensure_table_exists(pool)

    # Хедеры как у браузера
    HEADERS_GET = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }
    # Хедеры для AJAX запроса (сохранение)
    HEADERS_POST = HEADERS_GET.copy()
    HEADERS_POST["X-Requested-With"] = "XMLHttpRequest"

    while True:
        try:
            # 1. Ищем задачи. (Раз в 2 часа или если никогда не проверялось)
            tasks = []
            async with pool.acquire() as conn:
                try:
                    tasks = await conn.fetch("""
                        SELECT * FROM autorestock_tasks 
                        WHERE is_active = TRUE 
                        AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')
                    """)
                except: pass
            
            if not tasks:
                await asyncio.sleep(5)
                continue

            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                for t in tasks:
                    try:
                        # Получаем UUID из базы
                        uid_val = t['user_uid'] 
                        
                        # Расшифровка
                        key = decrypt_data(t['encrypted_golden_key'])
                        cookies = {"golden_key": key}
                        
                        # Парсинг конфига
                        raw_conf = t['lots_config']
                        lots = json.loads(raw_conf) if isinstance(raw_conf, str) else raw_conf
                        if not isinstance(lots, list): lots = []

                        is_changed = False
                        logs = []

                        for lot in lots:
                            # Есть ли ключи для залива?
                            pool_keys = lot.get('secrets_pool', [])
                            if not pool_keys: continue 

                            offer_id = lot.get('offer_id')
                            node_id = lot.get('node_id')
                            min_q = int(lot.get('min_qty', 5))

                            # -------------------------------------------------
                            # 1. ЗАГРУЗКА (GET)
                            # -------------------------------------------------
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=HEADERS_GET, cookies=cookies) as r:
                                html = await r.text()

                            # 2. ПАРСИНГ
                            real_oid, secrets_text, csrf, is_active, is_auto = parse_edit_page(html)

                            if not csrf:
                                logs.append(f"⚠️ {offer_id} нет доступа")
                                continue
                            
                            # 3. ПРОВЕРКА ГАЛОЧКИ АВТОВЫДАЧИ
                            if not is_auto:
                                # Галочка выключена -> ничего не делаем
                                continue

                            # 4. ПРОВЕРКА КОЛИЧЕСТВА
                            cur_qty = count_lines(secrets_text)
                            
                            if cur_qty < min_q:
                                # Нужно долить!
                                to_add = pool_keys[:50]
                                remaining_pool = pool_keys[50:]
                                
                                new_text = secrets_text.strip() + "\n" + "\n".join(to_add)
                                new_text = new_text.strip()
                                
                                # 5. СОХРАНЕНИЕ (POST)
                                payload = {
                                    "csrf_token": csrf,
                                    "offer_id": real_oid,
                                    "node_id": node_id,
                                    "secrets": new_text,
                                    "auto_delivery": "on", # Обязательно передаем, чтобы не выключилось
                                    "active": "on" if is_active else "",
                                    "save": "Сохранить"
                                }
                                if not is_active: payload.pop("active", None)
                                
                                # Важен Referer
                                req_h = HEADERS_POST.copy()
                                req_h["Referer"] = edit_url
                                
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=req_h) as pr:
                                    if pr.status == 200:
                                        logs.append(f"✅ {offer_id}: +{len(to_add)}")
                                        # Обновляем конфиг в памяти (убираем залитые ключи)
                                        lot['secrets_pool'] = remaining_pool
                                        is_changed = True
                                    else:
                                        logs.append(f"❌ {offer_id}: {pr.status}")
                            
                            await asyncio.sleep(2)

                        # Если залили ключи - обновляем базу (сохраняем остаток ключей)
                        if is_changed:
                            async with app.state.pool.acquire() as c:
                                await c.execute("UPDATE autorestock_tasks SET lots_config=$1::jsonb WHERE user_uid=$2::uuid", json.dumps(lots), uid_val)
                        
                        # Обновляем статус
                        final_msg = ", ".join(logs) if logs else f"✅ Проверено {datetime.now().strftime('%H:%M')}"
                        await update_status(pool, uid_val, final_msg)

                    except Exception as e:
                        log(f"Err task: {e}")
                        await update_status(pool, uid_val, "Ошибка воркера")
            
            await asyncio.sleep(5)
        except Exception as e:
            log(f"Err loop: {e}")
            await asyncio.sleep(5)

# --- API ---

@router.post("/fetch_offers")
async def fetch_offers(req: Request):
    """Получает офферы с сайта"""
    try:
        body = await req.json()
        golden_key = body.get("golden_key") or body.get("GoldenKey")
        node_ids = body.get("node_ids") or body.get("NodeIds") or []
    except: return {"success": False, "message": "JSON Error"}

    results = []
    HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    cookies = {"golden_key": golden_key}
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in node_ids:
            node = str(node).strip()
            if not node.isdigit(): continue
            try:
                async with session.get(f"https://funpay.com/lots/{node}/trade", headers=HEADERS, cookies=cookies) as resp:
                    if "login" in str(resp.url): return {"success": False, "message": "Key invalid"}
                    html = await resp.text()

                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html))
                if not found_ids:
                    # Проверка одиночного лота
                    async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=HEADERS, cookies=cookies) as r2:
                        h2 = await r2.text()
                        m = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', h2)
                        if m: found_ids.add(m.group(1))

                if not found_ids:
                    results.append({"node_id": node, "valid": False, "error": "Нет лотов"})
                    continue

                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies=cookies) as r_edit:
                        ht = await r_edit.text()
                        nm_m = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', ht)
                        nm = html_lib.unescape(nm_m.group(1)) if nm_m else "Без названия"
                        results.append({"node_id": node, "offer_id": oid, "name": nm, "valid": True})
                    await asyncio.sleep(0.1)
            except Exception as e:
                results.append({"node_id": node, "valid": False, "error": str(e)[:20]})
            await asyncio.sleep(0.5)
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(req: Request, u=Depends(get_current_user_raw)):
    """Сохраняет настройки и запускает воркер, если он стоит"""
    global WORKER_STARTED
    try:
        pool = getattr(req.app.state, 'pool', None)
        if not pool: return JSONResponse(content={"success": False, "message": "DB Error"}, status_code=200)

        # 1. Данные
        try:
            body = await req.json()
            golden_key = body.get("golden_key") or body.get("GoldenKey")
            active = body.get("active") if "active" in body else body.get("Active", False)
            lots_data = body.get("lots") or body.get("Lots") or []
        except: return JSONResponse(content={"success": False, "message": "JSON Bad"}, status_code=200)

        # 2. UUID
        try:
            uid_obj = uuid.UUID(str(u['uid']))
        except: return JSONResponse(content={"success": False, "message": "UID Error"}, status_code=200)

        await ensure_table_exists(pool)

        async with pool.acquire() as conn:
            # 3. Читаем старые
            existing_pools = {}
            try:
                row = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1::uuid", uid_obj)
                if row and row['lots_config']:
                    raw = row['lots_config']
                    loaded = json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(loaded, list):
                        for l in loaded:
                            existing_pools[str(l.get('offer_id'))] = l.get('secrets_pool', [])
            except: pass

            # 4. Собираем новые
            final_lots = []
            for lot in lots_data:
                oid = str(lot.get('offer_id') or lot.get('OfferId', ''))
                nid = str(lot.get('node_id') or lot.get('NodeId', ''))
                nm = str(lot.get('name') or lot.get('Name', 'Lot'))
                
                mq_val = lot.get('min_qty') if 'min_qty' in lot else lot.get('MinQty', 5)
                try: mq = int(mq_val)
                except: mq = 5

                new_keys_raw = lot.get('add_secrets') or lot.get('AddSecrets') or []
                new_keys = [str(k).strip() for k in new_keys_raw if str(k).strip()]
                
                if not oid: continue
                
                pool_keys = existing_pools.get(oid, []) + new_keys
                
                final_lots.append({
                    "node_id": nid, 
                    "offer_id": oid, 
                    "name": nm, 
                    "min_qty": mq, 
                    "secrets_pool": pool_keys
                })

            # 5. Пишем
            enc = encrypt_data(golden_key)
            json_str = json.dumps(final_lots)
            
            await conn.execute("""
                INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
                VALUES ($1::uuid, $2, $3, $4::jsonb, NULL, 'В очереди...')
                ON CONFLICT (user_uid) DO UPDATE SET
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                is_active = EXCLUDED.is_active,
                lots_config = EXCLUDED.lots_config,
                status_message = 'Обновлено',
                last_check_at = NULL
            """, uid_obj, enc, active, json_str)

        # 6. ЗАПУСК ВОРКЕРА (Один раз на весь сервер)
        if not WORKER_STARTED:
            asyncio.create_task(background_worker(pool))
            WORKER_STARTED = True
            
        return {"success": True, "message": "Сохранено"}

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(content={"success": False, "message": f"Err: {str(e)}"}, status_code=200)

@router.get("/status")
async def get_status(req: Request, u=Depends(get_current_user_raw)):
    try:
        pool = getattr(req.app.state, 'pool', None)
        uid_obj = uuid.UUID(str(u['uid']))
        
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1::uuid", uid_obj)
        
        if not r: return {"active": False, "message": "Не настроено", "lots": []}
        
        display_lots = []
        if r['lots_config']:
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
        return {"active": r['is_active'], "message": r['status_message'], "lots": display_lots}
    except Exception as e:
        return {"active": False, "message": f"Err: {str(e)}", "lots": []}
