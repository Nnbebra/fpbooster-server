import asyncio
import re
import html as html_lib
import json
import aiohttp
import traceback
import uuid
import sys
import os
from datetime import datetime
from typing import Dict, Any, List

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

# ВАЖНО: Никаких глобальных импортов из auth/utils здесь, чтобы не было 502!

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])

# --- ЛОГИРОВАНИЕ ---
LOG_FILE = os.path.join(os.getcwd(), "restock_final_debug.log")

def log_debug(msg):
    try:
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg}\n")
        print(f"[AutoRestock] {msg}", flush=True)
    except: pass

# --- HELPERS ---
def count_lines(text: str):
    if not text: return 0
    return len([l for l in text.split('\n') if l.strip()])

def parse_edit_page(html: str):
    """Извлекает ВСЕ данные, необходимые для сохранения лота."""
    offer_id, secrets, csrf, node_id = None, "", None, None
    is_active, is_auto = False, False
    
    # Offer ID
    m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', html)
    if not m_oid: m_oid = re.search(r'value=["\'](\d+)["\'][^>]*name=["\']offer_id["\']', html)
    if m_oid: offer_id = m_oid.group(1)
    
    # Node ID (нужен для сохранения)
    m_node = re.search(r'name=["\']node_id["\'][^>]*value=["\'](\d+)["\']', html)
    if m_node: node_id = m_node.group(1)

    # Текущие товары
    m_sec = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    if m_sec: secrets = html_lib.unescape(m_sec.group(1))

    # CSRF Token
    m_csrf = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', html)
    if not m_csrf: m_csrf = re.search(r'value=["\']([^"\']+)["\']', html)
    if m_csrf: csrf = m_csrf.group(1)

    # Статусы галочек
    if re.search(r'name=["\']active["\'][^>]*checked', html): is_active = True
    if re.search(r'name=["\']auto_delivery["\'][^>]*checked', html): is_auto = True

    return offer_id, secrets, csrf, is_active, is_auto, node_id

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

async def update_status(pool, uid_obj, msg):
    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2::uuid", str(msg)[:100], uid_obj)
    except: pass

# --- WORKER ---
async def worker(app):
    """Фоновый процесс проверки и пополнения."""
    await asyncio.sleep(10) # Ждем старта БД
    log_debug("Worker: Фоновый процесс запущен.")

    # Хедеры как в браузере
    HEADERS_GET = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    HEADERS_POST = HEADERS_GET.copy()
    HEADERS_POST["X-Requested-With"] = "XMLHttpRequest"

    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            
            # 1. Поиск задач
            async with app.state.pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT * FROM autorestock_tasks 
                    WHERE is_active = TRUE 
                    AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')
                """)

            if not tasks:
                await asyncio.sleep(10)
                continue
            
            log_debug(f"Worker: Найдено задач: {len(tasks)}")

            # Ленивый импорт внутри цикла
            from utils_crypto import decrypt_data

            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                for t in tasks:
                    uid_val = t['user_uid']
                    try:
                        log_debug(f"[{uid_val}] Начинаю проверку...")
                        
                        # 2. Дешифровка ключа
                        key = decrypt_data(t['encrypted_golden_key'])
                        cookies = {"golden_key": key}
                        
                        # 3. Конфиг лотов
                        raw = t['lots_config']
                        lots = json.loads(raw) if isinstance(raw, str) else raw
                        if not isinstance(lots, list):
                            log_debug(f"[{uid_val}] Ошибка: lots_config не является списком.")
                            continue

                        is_changed = False
                        log_msg_list = []

                        for lot in lots:
                            offer_id = lot.get('offer_id')
                            min_q = int(lot.get('min_qty', 5))
                            pool = lot.get('secrets_pool', [])

                            if not pool:
                                log_msg_list.append(f"Empty:{offer_id}")
                                continue

                            # ШАГ A: Загрузка страницы редактирования
                            log_debug(f"[{uid_val}] Загружаю оффер {offer_id}...")
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=HEADERS_GET, cookies=cookies) as r:
                                if r.status != 200:
                                    log_debug(f"[{uid_val}] Ошибка HTTP {r.status} для оффера {offer_id}")
                                    continue
                                html = await r.text()
                            
                            # ШАГ B: Парсинг страницы
                            oid, cur_text, csrf, lot_active, lot_auto, real_node_id = parse_edit_page(html)
                            
                            if not csrf:
                                log_debug(f"[{uid_val}] Ошибка: Не найден CSRF для {offer_id}. Ключ валиден?")
                                log_msg_list.append(f"Error:{offer_id}")
                                continue

                            # ШАГ C: Проверка автовыдачи (по ТЗ)
                            if not lot_auto:
                                log_debug(f"[{uid_val}] Пропускаю {offer_id}: автовыдача выключена.")
                                log_msg_list.append(f"AutoOff:{offer_id}")
                                continue

                            # ШАГ D: Считаем строки
                            cur_qty = count_lines(cur_text)
                            log_debug(f"[{uid_val}] Оффер {offer_id}: в наличии {cur_qty}, нужно минимум {min_q}")

                            if cur_qty < min_q:
                                # ШАГ E: Доливаем из базы
                                to_add = pool[:50] # Не более 50 за раз
                                remaining_pool = pool[50:]
                                
                                log_debug(f"[{uid_val}] Доливаю {len(to_add)} шт. в оффер {offer_id}...")
                                new_text = cur_text.strip() + "\n" + "\n".join(to_add)
                                new_text = new_text.strip()
                                
                                # ШАГ F: Отправка сохранения
                                payload = {
                                    "csrf_token": csrf,
                                    "offer_id": oid,
                                    "node_id": real_node_id or lot.get('node_id'),
                                    "secrets": new_text,
                                    "auto_delivery": "on",
                                    "active": "on" if lot_active else "",
                                    "save": "Сохранить"
                                }
                                if not lot_active: payload.pop("active", None)
                                
                                post_headers = HEADERS_POST.copy()
                                post_headers["Referer"] = edit_url
                                
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=post_headers) as pr:
                                    if pr.status == 200:
                                        log_debug(f"[{uid_val}] Успешно пополнено: {offer_id}")
                                        log_msg_list.append(f"✅{offer_id}:+{len(to_add)}")
                                        lot['secrets_pool'] = remaining_pool
                                        is_changed = True
                                    else:
                                        log_debug(f"[{uid_val}] Ошибка сохранения {offer_id}: HTTP {pr.status}")
                                        log_msg_list.append(f"FailSave:{offer_id}")
                            
                            await asyncio.sleep(2) # Пауза между офферами

                        # 4. Обновляем базу данных, если были изменения в пуле ключей
                        if is_changed:
                            async with app.state.pool.acquire() as c:
                                await c.execute("UPDATE autorestock_tasks SET lots_config=$1::jsonb WHERE user_uid=$2", json.dumps(lots), uid_val)
                        
                        # 5. Обновляем статус в базе для софта
                        final_status = ", ".join(log_msg_list) if log_msg_list else "✅ Проверено"
                        await update_status(app.state.pool, uid_val, final_status)
                        log_debug(f"[{uid_val}] Проверка завершена: {final_status}")

                    except Exception as e:
                        log_debug(f"[{uid_val}] Критическая ошибка задачи: {e}")
                        log_debug(traceback.format_exc())
                        await update_status(app.state.pool, uid_val, "Ошибка")

            await asyncio.sleep(5)
        except Exception as e:
            log_debug(f"Worker Loop Error: {e}")
            await asyncio.sleep(10)

# --- API ENDPOINTS ---

@router.post("/fetch_offers")
async def fetch_offers(req: Request):
    try:
        body = await req.json()
        golden_key = body.get("golden_key") or body.get("GoldenKey")
        node_ids = body.get("node_ids") or body.get("NodeIds") or []
    except: return {"success": False, "message": "JSON Bad"}

    results = []
    HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    cookies = {"golden_key": golden_key}
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in node_ids:
            node = str(node).strip()
            if not node.isdigit(): continue
            try:
                async with session.get(f"https://funpay.com/lots/{node}/trade", headers=HEADERS, cookies=cookies) as resp:
                    if "login" in str(resp.url): return {"success": False, "message": "Key Expired"}
                    html = await resp.text()

                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html))
                if not found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=HEADERS, cookies=cookies) as r2:
                        h2 = await r2.text()
                        oid_m = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', h2)
                        if oid_m: found_ids.add(oid_m.group(1))

                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies=cookies) as r_edit:
                        ht = await r_edit.text()
                        m_nm = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', ht)
                        nm = html_lib.unescape(m_nm.group(1)) if m_nm else "Item"
                        results.append({"node_id": node, "offer_id": oid, "name": nm, "valid": True})
                    await asyncio.sleep(0.1)
            except: pass
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(req: Request):
    from auth.guards import get_current_user
    from utils_crypto import encrypt_data
    try:
        pool = getattr(req.app.state, 'pool', None)
        if not pool: return JSONResponse(status_code=200, content={"success": False, "message": "No Pool"})

        # Auth
        try:
            u = await get_current_user(req.app, req)
            uid_obj = uuid.UUID(str(u['uid']))
        except Exception as e:
            return JSONResponse(status_code=200, content={"success": False, "message": f"Auth Fail: {e}"})

        # Data
        try:
            body = await req.json()
            golden_key = body.get("golden_key") or body.get("GoldenKey")
            active = body.get("active") if "active" in body else body.get("Active", False)
            lots_data = body.get("lots") or body.get("Lots") or []
        except: return JSONResponse(status_code=200, content={"success": False, "message": "JSON Bad"})

        await ensure_table_exists(pool)

        async with pool.acquire() as conn:
            existing_pools = {}
            try:
                row = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1", uid_obj)
                if row and row['lots_config']:
                    raw_db = row['lots_config']
                    loaded = json.loads(raw_db) if isinstance(raw_db, str) else raw_db
                    if isinstance(loaded, list):
                        for l in loaded:
                            existing_pools[str(l.get('offer_id'))] = l.get('secrets_pool', [])
            except: pass

            final_lots = []
            for lot in lots_data:
                oid = str(lot.get('offer_id') or lot.get('OfferId', ''))
                nid = str(lot.get('node_id') or lot.get('NodeId', ''))
                nm = str(lot.get('name') or lot.get('Name', 'Lot'))
                mq = int(lot.get('min_qty') or lot.get('MinQty', 5))
                new_keys = [str(k).strip() for k in (lot.get('add_secrets') or lot.get('AddSecrets') or []) if str(k).strip()]
                
                if not oid: continue
                pool_keys = existing_pools.get(oid, []) + new_keys
                final_lots.append({"node_id": nid, "offer_id": oid, "name": nm, "min_qty": mq, "secrets_pool": pool_keys})

            enc = encrypt_data(golden_key)
            # ВАЖНО: last_check_at = NULL позволяет воркеру сработать мгновенно
            await conn.execute("""
                INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
                VALUES ($1, $2, $3, $4::jsonb, NULL, 'В очереди...')
                ON CONFLICT (user_uid) DO UPDATE SET
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                is_active = EXCLUDED.is_active,
                lots_config = EXCLUDED.lots_config,
                status_message = 'Обновлено', last_check_at = NULL
            """, uid_obj, enc, active, json.dumps(final_lots))

        return {"success": True, "message": "Настройки сохранены!"}
    except Exception as e:
        log_debug(f"SAVE ERROR: {traceback.format_exc()}")
        return JSONResponse(status_code=200, content={"success": False, "message": f"Err: {str(e)}"})

@router.get("/status")
async def get_status(req: Request):
    from auth.guards import get_current_user
    try:
        u = await get_current_user(req.app, req)
        uid_obj = uuid.UUID(str(u['uid']))
        async with req.app.state.pool.acquire() as conn:
            r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1", uid_obj)
        if not r: return {"active": False, "message": "Не настроено", "lots": []}
        
        display_lots = []
        if r['lots_config']:
            raw_db = r['lots_config']
            loaded = json.loads(raw_db) if isinstance(raw_db, str) else raw_db
            if isinstance(loaded, list):
                for l in loaded:
                    display_lots.append({
                        "node_id": l.get('node_id'), "offer_id": l.get('offer_id'),
                        "name": l.get('name', 'Лот'), "min_qty": l.get('min_qty'),
                        "keys_in_db": len(l.get('secrets_pool', []))
                    })
        return {"active": r['is_active'], "message": r['status_message'], "lots": display_lots}
    except: return {"active": False, "message": "Error", "lots": []}
