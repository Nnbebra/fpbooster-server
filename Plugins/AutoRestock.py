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

# Инициализируем роутер с правильным префиксом
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
    """Извлекает данные лота, CSRF и статусы галочек"""
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

# --- API ENDPOINTS ---

@router.post("/fetch_offers")
async def fetch_offers(req: Request):
    """Получение списка офферов пользователя по категориям"""
    try:
        body = await req.json()
        golden_key = body.get("golden_key") or body.get("GoldenKey")
        node_ids = body.get("node_ids") or body.get("NodeIds") or []
    except: return {"success": False, "message": "JSON Parse Error"}

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
                        m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', h2)
                        if m_oid: found_ids.add(m_oid.group(1))

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
    """Сохранение конфигурации пополнения с поддержкой auto_enable"""
    from auth.guards import get_current_user
    from utils_crypto import encrypt_data
    try:
        pool = getattr(req.app.state, 'pool', None)
        u = await get_current_user(req.app, req)
        uid_obj = uuid.UUID(str(u['uid']))

        body = await req.json()
        golden_key = body.get("golden_key") or body.get("GoldenKey")
        active = body.get("active") if "active" in body else body.get("Active", False)
        lots_data = body.get("lots") or body.get("Lots") or []

        final_lots = []
        async with pool.acquire() as conn:
            existing_pools = {}
            row = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1", uid_obj)
            if row and row['lots_config']:
                loaded = json.loads(row['lots_config']) if isinstance(row['lots_config'], str) else row['lots_config']
                if isinstance(loaded, list):
                    for l in loaded: existing_pools[str(l.get('offer_id'))] = l.get('secrets_pool', [])

            for lot in lots_data:
                oid = str(lot.get('offer_id') or lot.get('OfferId', ''))
                if not oid: continue
                new_keys = [str(k).strip() for k in (lot.get('add_secrets') or lot.get('AddSecrets') or []) if str(k).strip()]
                pool_keys = existing_pools.get(oid, []) + new_keys
                final_lots.append({
                    "node_id": str(lot.get('node_id') or lot.get('NodeId', '')),
                    "offer_id": oid,
                    "name": str(lot.get('name') or lot.get('Name', 'Lot')),
                    "min_qty": int(lot.get('min_qty') or lot.get('MinQty', 5)),
                    "auto_enable": bool(lot.get('auto_enable', lot.get('AutoEnable', True))), # Сохраняем настройку
                    "secrets_pool": pool_keys
                })

            enc = encrypt_data(golden_key)
            await conn.execute("""
                INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
                VALUES ($1, $2, $3, $4::jsonb, NULL, 'Настройки сохранены')
                ON CONFLICT (user_uid) DO UPDATE SET
                encrypted_golden_key = EXCLUDED.encrypted_golden_key, is_active = EXCLUDED.is_active,
                lots_config = EXCLUDED.lots_config, status_message = 'Обновлено', last_check_at = NULL
            """, uid_obj, enc, active, json.dumps(final_lots))

        return {"success": True, "message": "Настройки сохранены!"}
    except Exception as e:
        log_debug(f"SAVE ERROR: {traceback.format_exc()}")
        return JSONResponse(status_code=200, content={"success": False, "message": str(e)})

@router.get("/status")
async def get_status(req: Request):
    """Получение текущего статуса и конфигурации"""
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
                        "auto_enable": l.get('auto_enable', True), # Возвращаем настройку софту
                        "keys_in_db": len(l.get('secrets_pool', []))
                    })
        return {"active": r['is_active'], "message": r['status_message'], "lots": display_lots}
    except: return {"active": False, "message": "Error", "lots": []}

# --- WORKER (ОБНОВЛЕННАЯ ЛОГИКА) ---

async def worker(app):
    """Фоновый воркер для проверки и пополнения товаров"""
    await asyncio.sleep(10)
    log_debug("Worker: Запущен. Ожидаю задачи...")
    from utils_crypto import decrypt_data

    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    POST_HEADERS = HEADERS.copy()
    POST_HEADERS["X-Requested-With"] = "XMLHttpRequest"

    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            async with app.state.pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT * FROM autorestock_tasks 
                    WHERE is_active = TRUE AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')
                """)

            if not tasks:
                await asyncio.sleep(15)
                continue
            
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                for t in tasks:
                    uid = t['user_uid']
                    try:
                        key = decrypt_data(t['encrypted_golden_key'])
                        cookies = {"golden_key": key}
                        lots_conf = json.loads(t['lots_config']) if isinstance(t['lots_config'], str) else t['lots_config']
                        is_changed = False
                        log_msg = []

                        for lot in lots_conf:
                            pool = lot.get('secrets_pool', [])
                            if not pool: continue # Нет товара в пуле - нечего делать

                            offer_id = lot['offer_id']
                            target_qty = int(lot.get('min_qty', 5))
                            auto_enable_cfg = lot.get('auto_enable', True)
                            
                            # 1. Загрузка страницы редактирования
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=HEADERS, cookies=cookies) as r:
                                html_txt = await r.text()
                            
                            oid, current_text, csrf, is_act, is_aut, real_node = parse_edit_page(html_txt)
                            if not csrf:
                                log_debug(f"[{uid}] Ошибка: Нет доступа к офферу {offer_id}")
                                continue

                            # Разбиваем текущее содержимое на строки
                            current_lines = [l.strip() for l in current_text.split('\n') if l.strip()]
                            
                            # ЛОГИКА: Если первая строка на FunPay не совпадает с пулом - товар сменился в софте. Очищаем старое.
                            if current_lines and current_lines[0] != pool[0]:
                                log_debug(f"[{uid}] Смена товара в оффере {offer_id}. Очищаю и заменяю.")
                                current_lines = []

                            # ЛОГИКА: Проверка галочки автовыдачи
                            effective_auto = is_aut
                            if not is_aut:
                                if auto_enable_cfg:
                                    log_debug(f"[{uid}] Оффер {offer_id}: автовыдача была выключена. ВКЛЮЧАЮ.")
                                    effective_auto = True
                                else:
                                    log_msg.append(f"AutoOff:{offer_id}")
                                    continue

                            # ЛОГИКА: Пополнение до ТОЧНОГО количества target_qty (размножение ссылки из пула)
                            if len(current_lines) < target_qty:
                                needed = target_qty - len(current_lines)
                                
                                # Размножаем ссылки из пула до нужного количества
                                to_add = []
                                while len(to_add) < needed:
                                    portion = pool[:(needed - len(to_add))]
                                    to_add.extend(portion)
                                    if len(portion) == 0: break # Пул пуст

                                final_list = current_lines + to_add
                                new_secrets_text = "\n".join(final_list)

                                payload = {
                                    "csrf_token": csrf, "offer_id": oid, "node_id": real_node or lot.get('node_id'),
                                    "secrets": new_secrets_text, 
                                    "auto_delivery": "on" if effective_auto else "",
                                    "active": "on" if is_act else "", 
                                    "save": "Сохранить"
                                }
                                if not is_act: payload.pop("active", None)

                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=POST_HEADERS) as pr:
                                    if pr.status == 200:
                                        log_debug(f"[{uid}] Оффер {offer_id} успешно пополнен до {target_qty} строк.")
                                        log_msg.append(f"✅{offer_id}:{target_qty}")
                                        
                                        # Обновляем пул в памяти (убираем использованные элементы, если они были уникальными)
                                        # Но так как мы их размножали, мы просто имитируем потребление части пула
                                        lot['secrets_pool'] = pool[len(to_add):] if len(pool) > len(to_add) else []
                                        is_changed = True
                                    else:
                                        log_debug(f"[{uid}] Ошибка сохранения {offer_id}: {pr.status}")
                            
                            await asyncio.sleep(2)

                        if is_changed:
                            async with app.state.pool.acquire() as c:
                                await c.execute("UPDATE autorestock_tasks SET lots_config=$1::jsonb WHERE user_uid=$2", json.dumps(lots_conf), uid)
                        
                        status = ", ".join(log_msg) if log_msg else "✅ Проверено"
                        async with app.state.pool.acquire() as c_upd:
                            await c_upd.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", status[:100], uid)
                    except Exception as e:
                        log_debug(f"Ошибка задачи {uid}: {traceback.format_exc()}")
            await asyncio.sleep(20)
        except Exception as e:
            log_debug(f"Критическая ошибка воркера: {e}")
            await asyncio.sleep(30)
