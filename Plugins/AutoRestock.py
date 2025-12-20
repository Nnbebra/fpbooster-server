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

class LotConfig(BaseModel):
    node_id: str
    min_qty: int
    add_secrets: list[str] = [] # Ключи, которые добавляет юзер (только новые)

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
    
    # 1. CSRF
    m = re.search(r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']', html)
    if m: csrf = m.group(1)
    
    # 2. Offer ID (нужен для сохранения)
    m = re.search(r'name=["\']offer_id["\'][^>]+value=["\'](\d+)["\']', html)
    if m: offer_id = m.group(1)
    
    # 3. Текущие товары (textarea)
    # Ищем содержимое внутри <textarea name="secrets">...</textarea>
    # Используем DOTALL, так как там много переносов строк
    m = re.search(r'<textarea[^>]*name=["\']secrets["\'][^>]*>(.*?)</textarea>', html, re.DOTALL)
    if m: 
        current_secrets = html_lib.unescape(m.group(1))
    
    return csrf, offer_id, current_secrets

def count_items(text: str) -> int:
    if not text or not text.strip(): return 0
    # Считаем непустые строки
    return len([line for line in text.split('\n') if line.strip()])

# --- WORKER ---
async def worker(app):
    await asyncio.sleep(10) # Даем серверу запуститься
    print(">>> [AutoRestock] WORKER STARTED", flush=True)
    
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=60)
    
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest"
    }

    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            pool = app.state.pool
            
            # Берем активные задачи
            tasks = []
            async with pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT user_uid, encrypted_golden_key, lots_config 
                    FROM autorestock_tasks 
                    WHERE is_active = TRUE 
                    -- Проверка раз в 5-10 минут, чтобы не спамить
                    AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '5 minutes')
                """)

            if not tasks: await asyncio.sleep(5); continue

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                for task in tasks:
                    uid = task['user_uid']
                    lots_config = json.loads(task['lots_config'])
                    new_config_to_save = [] # Сюда будем сохранять обновленный пул ключей
                    
                    is_modified_db = False
                    status_log = []

                    try:
                        try: key = decrypt_data(task['encrypted_golden_key'])
                        except: await update_status(pool, uid, "❌ Ошибка ключа"); continue

                        cookies = {"golden_key": key}
                        
                        for lot in lots_config:
                            node_id = str(lot.get('node_id'))
                            min_qty = int(lot.get('min_qty', 0))
                            pool_keys = lot.get('secrets_pool', [])
                            
                            # Если ключей в базе нет, пропускаем проверку (нечего заливать)
                            if not pool_keys:
                                new_config_to_save.append(lot)
                                continue

                            # 1. Заходим в редактирование
                            edit_url = f"https://funpay.com/lots/offerEdit?node={node_id}"
                            async with session.get(edit_url, headers=HEADERS, cookies=cookies) as resp:
                                if "login" in str(resp.url): 
                                    status_log.append("❌ Логин"); break
                                html = await resp.text()

                            csrf, offer_id, current_text = get_tokens_and_info(html)
                            
                            if not csrf or not offer_id:
                                status_log.append(f"⚠️ Лот {node_id}: Ошибка парсинга")
                                new_config_to_save.append(lot)
                                continue

                            current_qty = count_items(current_text)
                            
                            # 2. Проверяем нужно ли пополнять
                            if current_qty < min_qty:
                                # Нужно залить
                                needed = min_qty - current_qty + 2 # Доливаем с небольшим запасом (+2)
                                if needed > len(pool_keys): needed = len(pool_keys) # Если ключей мало, льем все что есть
                                
                                if needed > 0:
                                    keys_to_add = pool_keys[:needed]
                                    remaining_keys = pool_keys[needed:]
                                    
                                    # Формируем новый текст: старый + новые с новой строки
                                    new_text = current_text.strip() + "\n" + "\n".join(keys_to_add)
                                    
                                    # 3. Сохраняем на FunPay
                                    payload = {
                                        "csrf_token": csrf,
                                        "offer_id": offer_id,
                                        "node_id": node_id,
                                        "auto_delivery": "on", # Включаем галочку
                                        "secrets": new_text,
                                        "active": "on", # Активен
                                        "save": "Сохранить" # Имитация кнопки
                                    }
                                    
                                    # Нужно добавить остальные поля формы, иначе FunPay может ругаться?
                                    # Обычно хватает offer_id, node_id, secrets, csrf.
                                    # Но лучше распарсить все input hidden, если будут ошибки.
                                    # Пока пробуем минимальный payload.

                                    try:
                                        post_hdrs = HEADERS.copy()
                                        post_hdrs["Referer"] = edit_url
                                        save_url = "https://funpay.com/lots/offerSave"
                                        
                                        async with session.post(save_url, data=payload, headers=post_hdrs, cookies=cookies) as save_resp:
                                            if save_resp.status == 200:
                                                status_log.append(f"✅ {node_id}: +{needed} шт.")
                                                # Обновляем конфиг для БД (удаляем залитые ключи)
                                                lot['secrets_pool'] = remaining_keys
                                                is_modified_db = True
                                            else:
                                                status_log.append(f"❌ {node_id}: Ошибка HTTP {save_resp.status}")
                                    except Exception as e:
                                        status_log.append(f"❌ {node_id}: {str(e)[:20]}")
                                else:
                                    # Ключей нет в базе
                                    status_log.append(f"⚠️ {node_id}: База пуста")
                            
                            new_config_to_save.append(lot)
                            await asyncio.sleep(random.uniform(2, 5))

                        # Сохраняем изменения в БД (если потратили ключи)
                        if is_modified_db:
                            async with pool.acquire() as conn:
                                await conn.execute("UPDATE autorestock_tasks SET lots_config=$1 WHERE user_uid=$2", json.dumps(new_config_to_save), uid)
                        
                        final_msg = ", ".join(status_log) if status_log else "⏳ Мониторинг..."
                        await update_status(pool, uid, final_msg)

                    except Exception as e:
                        traceback.print_exc()
                        await update_status(pool, uid, "Ошибка воркера")

            await asyncio.sleep(5)
        except Exception as ex:
            print(f"Main loop error: {ex}")
            await asyncio.sleep(10)

# --- API ---
async def get_plugin_user(request: Request): return await get_current_user_raw(request.app, request)

@router.post("/set")
async def save_config(data: RestockSettings, req: Request, u=Depends(get_plugin_user)):
    async with req.app.state.pool.acquire() as conn:
        # 1. Получаем текущий конфиг, чтобы не стереть старые ключи, если юзер просто меняет настройки
        current_row = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1", u['uid'])
        
        final_lots = []
        existing_map = {}
        
        if current_row and current_row['lots_config']:
            try:
                existing_lots = json.loads(current_row['lots_config'])
                for l in existing_lots: existing_map[str(l.get('node_id'))] = l.get('secrets_pool', [])
            except: pass

        # 2. Мержим новые данные
        for new_lot in data.lots:
            nid = str(new_lot.node_id)
            # Берем старые ключи
            old_keys = existing_map.get(nid, [])
            # Добавляем новые (если прислали)
            added_keys = [k.strip() for k in new_lot.add_secrets if k.strip()]
            
            combined_keys = old_keys + added_keys
            
            final_lots.append({
                "node_id": nid,
                "min_qty": new_lot.min_qty,
                "secrets_pool": combined_keys
            })

        enc = encrypt_data(data.golden_key)
        await conn.execute("""
            INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
            VALUES ($1, $2, $3, $4, NOW(), 'Сохранено')
            ON CONFLICT (user_uid) DO UPDATE SET
            encrypted_golden_key = EXCLUDED.encrypted_golden_key,
            is_active = EXCLUDED.is_active,
            lots_config = EXCLUDED.lots_config,
            status_message = 'Настройки обновлены'
        """, u['uid'], enc, data.active, json.dumps(final_lots))
        
    return {"success": True, "message": "Настройки и ключи сохранены"}

@router.get("/status")
async def get_status(req: Request, u=Depends(get_plugin_user)):
    async with req.app.state.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1", u['uid'])
    
    if not r: return {"active": False, "message": "Не настроено", "lots": []}
    
    # Возвращаем клиенту инфу о лотах (сколько ключей в базе), но САМИ КЛЮЧИ не отдаем (безопасность/трафик)
    lots_info = []
    if r['lots_config']:
        try:
            data = json.loads(r['lots_config'])
            for d in data:
                lots_info.append({
                    "node_id": d.get("node_id"),
                    "min_qty": d.get("min_qty"),
                    "keys_in_db": len(d.get("secrets_pool", []))
                })
        except: pass

    return {"active": r['is_active'], "message": r['status_message'], "lots": lots_info}