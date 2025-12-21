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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])

# Глобальная переменная для доступа к приложению (чтобы достать пул БД)
APP_INSTANCE = None

# --- ЛОГГЕР ---
def log(msg):
    t = datetime.now().strftime("%H:%M:%S")
    print(f"[{t}] [AutoRestock] {msg}", flush=True)

# --- HELPERS ---
def count_lines(text: str):
    if not text: return 0
    # Считаем непустые строки
    return len([l for l in text.split('\n') if l.strip()])

def parse_edit_page(html: str):
    """
    Парсит страницу редактирования.
    Возвращает: offer_id, secrets_text, csrf_token, is_active, is_auto
    """
    offer_id = None
    secrets = ""
    csrf = None
    is_active = False
    is_auto = False
    
    # 1. Offer ID
    m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', html)
    if not m_oid: m_oid = re.search(r'value=["\'](\d+)["\'][^>]*name=["\']offer_id["\']', html)
    if m_oid: offer_id = m_oid.group(1)
    
    # 2. Поле с товарами (secrets)
    m_sec = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    if m_sec: secrets = html_lib.unescape(m_sec.group(1))

    # 3. CSRF
    m_csrf = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', html)
    if not m_csrf: m_csrf = re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']csrf_token["\']', html)
    if m_csrf: csrf = m_csrf.group(1)

    # 4. Чекбоксы (Active / Auto Delivery)
    # Ищем input с именем auto_delivery, у которого есть атрибут checked
    if re.search(r'name=["\']active["\'][^>]*checked', html): is_active = True
    if re.search(r'name=["\']auto_delivery["\'][^>]*checked', html): is_auto = True

    return offer_id, secrets, csrf, is_active, is_auto

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

# --- ЗАПУСК ВОРКЕРА ПРИ СТАРТЕ ---
@router.on_event("startup")
async def start_restock_worker():
    # Эта функция запустится автоматически вместе с сервером
    log("Инициализация плагина...")
    # Нам нужно как-то получить доступ к app.state.pool. 
    # Обычно в FastAPI это делается через замыкание, но здесь мы запустим задачу, которая подождет инициализации.
    asyncio.create_task(worker_loop())

# --- API ---

@router.post("/fetch_offers")
async def fetch_offers(req: Request):
    """Получение списка офферов с FunPay"""
    # Сохраняем ссылку на приложение для воркера
    global APP_INSTANCE
    APP_INSTANCE = req.app

    try:
        body = await req.json()
        golden_key = body.get("golden_key") or body.get("GoldenKey")
        node_ids = body.get("node_ids") or body.get("NodeIds") or []
    except:
        return {"success": False, "message": "JSON Error"}

    results = []
    HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    cookies = {"golden_key": golden_key}
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in node_ids:
            node = str(node).strip()
            if not node.isdigit(): continue
            try:
                # 1. Заходим в категорию
                async with session.get(f"https://funpay.com/lots/{node}/trade", headers=HEADERS, cookies=cookies) as resp:
                    if "login" in str(resp.url): return {"success": False, "message": "Golden Key невалиден"}
                    html = await resp.text()

                # 2. Ищем ссылки на редактирование
                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html))
                
                # 3. Если пусто, пробуем прямой заход (для одиночных лотов)
                if not found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=HEADERS, cookies=cookies) as r2:
                        h2 = await r2.text()
                        m_oid = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', h2)
                        if m_oid: found_ids.add(m_oid.group(1))

                if not found_ids:
                    results.append({"node_id": node, "valid": False, "error": "Лоты не найдены"})
                    continue

                # 4. Парсим названия
                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies=cookies) as r_edit:
                        ht = await r_edit.text()
                        # Ищем название
                        m_name = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', ht)
                        nm = "Без названия"
                        if m_name: nm = html_lib.unescape(m_name.group(1))
                        
                        results.append({"node_id": node, "offer_id": oid, "name": nm, "valid": True})
                    await asyncio.sleep(0.1)
            except Exception as e:
                results.append({"node_id": node, "valid": False, "error": str(e)[:20]})
            await asyncio.sleep(0.5)
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(req: Request):
    """Сохранение настроек (Безопасное)"""
    global APP_INSTANCE
    APP_INSTANCE = req.app

    try:
        pool = getattr(req.app.state, 'pool', None)
        if not pool: return JSONResponse(status_code=200, content={"success": False, "message": "DB not ready"})

        # 1. Авторизация вручную
        try:
            u = await get_current_user(req.app, req)
            uid_obj = uuid.UUID(str(u['uid']))
        except Exception as e:
            return JSONResponse(status_code=200, content={"success": False, "message": f"Auth Error: {e}"})

        # 2. Парсинг
        try:
            body = await req.json()
            golden_key = body.get("golden_key") or body.get("GoldenKey")
            active = body.get("active") if "active" in body else body.get("Active", False)
            lots_data = body.get("lots") or body.get("Lots") or []
        except:
            return JSONResponse(status_code=200, content={"success": False, "message": "JSON Error"})

        await ensure_table_exists(pool)

        async with pool.acquire() as conn:
            # 3. Чтение старого конфига (Merge)
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

            # 4. Сборка нового
            final_lots = []
            for lot in lots_data:
                oid = str(lot.get('offer_id') or lot.get('OfferId', ''))
                nid = str(lot.get('node_id') or lot.get('NodeId', ''))
                nm = str(lot.get('name') or lot.get('Name', 'Lot'))
                
                # Min Qty
                mq_val = lot.get('min_qty') if 'min_qty' in lot else lot.get('MinQty', 5)
                try: mq = int(mq_val)
                except: mq = 5

                # Keys
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

            # 5. Шифрование и запись
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
                last_check_at = NULL  -- Сбрасываем время проверки, чтобы воркер сработал сразу
            """, uid_obj, enc, active, json_str)
            
        return {"success": True, "message": "Конфигурация сохранена"}

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=200, content={"success": False, "message": f"Err: {str(e)}"})

@router.get("/status")
async def get_status(req: Request):
    global APP_INSTANCE
    APP_INSTANCE = req.app 
    try:
        u = await get_current_user(req.app, req)
        uid_obj = uuid.UUID(str(u['uid']))
        pool = req.app.state.pool
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
    except: return {"active": False, "message": "Err", "lots": []}

# --- WORKER LOOP ---
async def worker_loop():
    """Основной цикл проверки"""
    log("Воркер запущен и ждет пул БД...")
    await asyncio.sleep(5) # Ждем старта БД
    
    # Пытаемся найти пул в глобальной переменной
    pool = None
    if APP_INSTANCE and hasattr(APP_INSTANCE.state, 'pool'):
        pool = APP_INSTANCE.state.pool
    
    # Если не нашли, ждем еще
    while not pool:
        if APP_INSTANCE and hasattr(APP_INSTANCE.state, 'pool'):
            pool = APP_INSTANCE.state.pool
            break
        await asyncio.sleep(5)
    
    await ensure_table_exists(pool)
    log("Воркер подключился к БД!")

    # Хедеры для запросов
    HEADERS_GET = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }
    HEADERS_POST = HEADERS_GET.copy()
    HEADERS_POST["X-Requested-With"] = "XMLHttpRequest" # Важно для AJAX FunPay

    while True:
        try:
            # 1. Ищем задачи (активные + время проверки > 2 часов или NULL)
            tasks = []
            async with pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT * FROM autorestock_tasks 
                    WHERE is_active = TRUE 
                    AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')
                """)
            
            if not tasks:
                await asyncio.sleep(10)
                continue
            
            log(f"Найдено задач: {len(tasks)}")

            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                for t in tasks:
                    uid_val = t['user_uid'] # UUID object
                    try:
                        # Дешифруем ключ
                        golden_key = decrypt_data(t['encrypted_golden_key'])
                        cookies = {"golden_key": golden_key}
                        
                        # Парсим конфиг
                        raw_conf = t['lots_config']
                        lots_conf = json.loads(raw_conf) if isinstance(raw_conf, str) else raw_conf
                        if not isinstance(lots_conf, list): lots_conf = []

                        is_changed = False # Флаг изменений (потратили ключи?)
                        logs = []

                        for lot in lots_conf:
                            # Пропускаем, если нет ключей в базе (нечего заливать)
                            pool_keys = lot.get('secrets_pool', [])
                            if not pool_keys: continue 

                            offer_id = lot.get('offer_id')
                            node_id = lot.get('node_id')
                            min_q = int(lot.get('min_qty', 5))

                            # ШАГ 1: Идем на страницу редактирования
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=HEADERS_GET, cookies=cookies) as r:
                                if r.status != 200:
                                    logs.append(f"Err HTTP {r.status}")
                                    continue
                                html = await r.text()

                            # ШАГ 2: Парсим данные
                            real_oid, secrets_text, csrf, is_active, is_auto = parse_edit_page(html)

                            if not csrf:
                                logs.append(f"⚠️ {offer_id} Access Denied")
                                continue
                            
                            # ШАГ 3: Проверка галочки "Автоматическая выдача"
                            if not is_auto:
                                # Галочка выключена -> ничего не делаем с этим лотом
                                # (Пользователь должен сам включить её, либо мы можем допилить включение)
                                continue

                            # ШАГ 4: Считаем количество строк
                            current_qty = count_lines(secrets_text)

                            # ШАГ 5: Нужно ли пополнять?
                            if current_qty < min_q:
                                # Берем ключи (макс 50 за раз, чтобы не перегрузить запрос)
                                to_add = pool_keys[:50]
                                remaining_pool = pool_keys[50:]

                                # Формируем новый текст: Старый + \n + Новые
                                new_secrets_text = secrets_text.strip() + "\n" + "\n".join(to_add)
                                new_secrets_text = new_secrets_text.strip()

                                # ШАГ 6: Отправляем запрос на сохранение
                                payload = {
                                    "csrf_token": csrf,
                                    "offer_id": real_oid,
                                    "node_id": node_id,
                                    "secrets": new_secrets_text,
                                    "auto_delivery": "on", # Подтверждаем автовыдачу
                                    "active": "on" if is_active else "", # Сохраняем активность как была
                                    "save": "Сохранить" # Кнопка сабмита
                                }
                                # Если лот был неактивен, параметр 'active' не отправляем (иначе он включится)
                                if not is_active: del payload['active']

                                # Важно указать Referer, иначе FunPay может отклонить
                                post_headers = HEADERS_POST.copy()
                                post_headers["Referer"] = edit_url

                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=post_headers) as pr:
                                    if pr.status == 200:
                                        logs.append(f"✅ {offer_id}: +{len(to_add)} шт.")
                                        # Обновляем пул в памяти
                                        lot['secrets_pool'] = remaining_pool
                                        is_changed = True
                                    else:
                                        logs.append(f"❌ {offer_id}: err {pr.status}")
                            
                            # Пауза между лотами, чтобы не спамить
                            await asyncio.sleep(2)

                        # Если были изменения, обновляем базу данных
                        if is_changed:
                            json_dump = json.dumps(lots_conf)
                            async with pool.acquire() as c:
                                await c.execute("UPDATE autorestock_tasks SET lots_config=$1::jsonb WHERE user_uid=$2::uuid", json_dump, uid_val)
                        
                        # Обновляем статус задачи
                        final_msg = ", ".join(logs) if logs else f"✅ Проверено {datetime.now().strftime('%H:%M')}"
                        await update_status(pool, uid_val, final_msg)

                    except Exception as e:
                        print(f"Worker Error for {t['user_uid']}: {e}")
                        await update_status(pool, uid_val, "Ошибка воркера")
            
            # Ждем 5 сек перед следующим циклом поиска задач
            await asyncio.sleep(5)

        except Exception as e:
            print(f"Global Worker Error: {e}")
            await asyncio.sleep(5)
