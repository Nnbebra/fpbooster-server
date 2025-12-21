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

router = APIRouter(prefix="/api/plus/autorestock", tags=["AutoRestock Plugin"])

# --- ЛОГИРОВАНИЕ ---
LOG_FILE = os.path.join(os.getcwd(), "restock_final_debug.log")

def log_debug(msg):
    try:
        t = datetime.now().strftime("%H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{t}] {msg}\n")
        print(f"[AutoRestock] {msg}", flush=True)
    except: pass

# --- ПАРСИНГ ---
def parse_edit_page(html: str):
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

# --- API ---

@router.post("/fetch_offers")
async def fetch_offers(req: Request):
    try:
        body = await req.json()
        golden_key = body.get("golden_key") or body.get("GoldenKey")
        node_ids = body.get("node_ids") or body.get("NodeIds") or []
    except: return {"success": False, "message": "JSON Error"}

    results = []
    HEADERS = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for node in node_ids:
            node = str(node).strip()
            if not node.isdigit(): continue
            try:
                async with session.get(f"https://funpay.com/lots/{node}/trade", headers=HEADERS, cookies={"golden_key": golden_key}) as resp:
                    html = await resp.text()
                found_ids = set(re.findall(r'offerEdit\?[^"\']*offer=(\d+)', html))
                if not found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?node={node}", headers=HEADERS, cookies={"golden_key": golden_key}) as r2:
                        h2 = await r2.text()
                        m = re.search(r'name=["\']offer_id["\'][^>]*value=["\'](\d+)["\']', h2)
                        if m: found_ids.add(m.group(1))

                for oid in found_ids:
                    async with session.get(f"https://funpay.com/lots/offerEdit?offer={oid}", headers=HEADERS, cookies={"golden_key": golden_key}) as r_edit:
                        ht = await r_edit.text()
                        nm = "Товар"
                        m_nm = re.search(r'name=["\']fields\[summary\]\[ru\]["\'][^>]*value=["\']([^"\']+)["\']', ht)
                        if m_nm: nm = html_lib.unescape(m_nm.group(1))
                        results.append({"node_id": node, "offer_id": oid, "name": nm, "valid": True})
                    await asyncio.sleep(0.1)
            except: pass
    return {"success": True, "data": results}

@router.post("/set")
async def save_settings(req: Request):
    from auth.guards import get_current_user
    from utils_crypto import encrypt_data
    try:
        u = await get_current_user(req.app, req)
        uid_obj = uuid.UUID(str(u['uid']))
        body = await req.json()
        
        # Загружаем старое, чтобы не тереть конфиг
        existing_conf = {}
        pool_obj = getattr(req.app.state, 'pool', None)
        if pool_obj:
            async with pool_obj.acquire() as conn:
                row = await conn.fetchrow("SELECT lots_config FROM autorestock_tasks WHERE user_uid=$1", uid_obj)
                if row and row['lots_config']:
                    try:
                        loaded = json.loads(row['lots_config']) if isinstance(row['lots_config'], str) else row['lots_config']
                        for l in loaded: existing_conf[str(l.get('offer_id'))] = l.get('source_text', '')
                    except: pass

        final_lots = []
        for lot in (body.get("lots") or []):
            oid = str(lot.get('offer_id', ''))
            
            # ТЕПЕРЬ МЫ ХРАНИМ ИСХОДНЫЙ ТЕКСТ (ССЫЛКУ) КАК ОН ЕСТЬ
            raw_secrets = lot.get('add_secrets', [])
            # Если с клиента пришел список строк, собираем его обратно в текст
            new_text = "\n".join(raw_secrets).strip()
            
            # Если пользователь прислал пустой текст, оставляем старый
            final_text = new_text if new_text else existing_conf.get(oid, "")

            final_lots.append({
                "node_id": str(lot.get('node_id', '')),
                "offer_id": oid,
                "name": str(lot.get('name', 'Lot')),
                "min_qty": int(lot.get('min_qty', 5)),
                "auto_enable": bool(lot.get('auto_enable', True)),
                "source_text": final_text # Храним исходник (ссылку)
            })

        enc = encrypt_data(body.get("golden_key", ""))
        
        async with req.app.state.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO autorestock_tasks (user_uid, encrypted_golden_key, is_active, lots_config, last_check_at, status_message)
                VALUES ($1, $2, $3, $4::jsonb, NULL, 'Настройки сохранены')
                ON CONFLICT (user_uid) DO UPDATE SET
                encrypted_golden_key=EXCLUDED.encrypted_golden_key, is_active=EXCLUDED.is_active,
                lots_config=EXCLUDED.lots_config, status_message='Обновлено', last_check_at=NULL
            """, uid_obj, enc, body.get("active", False), json.dumps(final_lots))
            
        return {"success": True, "message": "Сохранено"}
    except Exception as e: return JSONResponse(status_code=200, content={"success": False, "message": str(e)})

@router.get("/status")
async def get_status(req: Request):
    from auth.guards import get_current_user
    try:
        u = await get_current_user(req.app, req)
        uid_obj = uuid.UUID(str(u['uid']))
        async with req.app.state.pool.acquire() as conn:
            r = await conn.fetchrow("SELECT is_active, status_message, lots_config FROM autorestock_tasks WHERE user_uid=$1", uid_obj)
        if not r: return {"active": False, "message": "Не настроено", "lots": []}
        
        lots = json.loads(r['lots_config']) if isinstance(r['lots_config'], str) else r['lots_config']
        display = []
        for l in lots:
            display.append({
                "node_id": l.get('node_id'), "offer_id": l.get('offer_id'),
                "name": l.get('name'), "min_qty": l.get('min_qty'),
                "auto_enable": l.get('auto_enable', True),
                "keys_in_db": 1 if l.get('source_text') else 0 # Просто флаг есть ли текст
            })
        return {"active": r['is_active'], "message": r['status_message'], "lots": display}
    except: return {"active": False, "message": "Error", "lots": []}

# --- ВОРКЕР (СТРОГО ПО ЛОГИКЕ КЛИЕНТА) ---
async def worker(app):
    await asyncio.sleep(10)
    log_debug("Worker started (Direct Logic).")
    from utils_crypto import decrypt_data
    
    HEADERS = {"User-Agent": "Mozilla/5.0"}
    POST_H = HEADERS.copy()
    POST_H["X-Requested-With"] = "XMLHttpRequest"

    while True:
        try:
            if not hasattr(app.state, 'pool'): await asyncio.sleep(5); continue
            
            async with app.state.pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT * FROM autorestock_tasks 
                    WHERE is_active = TRUE 
                    AND (last_check_at IS NULL OR last_check_at <= NOW() - INTERVAL '2 hours')
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
                        
                        log_msg = []
                        
                        for lot in lots_conf:
                            # Получаем исходный текст (ссылку), который пользователь ввел
                            source_text = lot.get('source_text', '').strip()
                            if not source_text: continue 

                            offer_id = lot['offer_id']
                            min_q = int(lot['min_qty'])
                            
                            # 1. Загрузка страницы
                            async with session.get(f"https://funpay.com/lots/offerEdit?offer={offer_id}", headers=HEADERS, cookies=cookies) as r:
                                html = await r.text()
                            
                            oid, cur_text, csrf, active_lot, auto_dlv, real_node = parse_edit_page(html)
                            if not csrf: continue

                            # 2. Автовыдача (Если выключена, а должна работать -> включаем)
                            should_be_auto = lot.get('auto_enable', True)
                            final_auto = auto_dlv
                            if not auto_dlv and should_be_auto:
                                final_auto = True 

                            # 3. Считаем текущее количество строк
                            cur_lines = [l for l in cur_text.split('\n') if l.strip()]
                            cur_count = len(cur_lines)

                            # 4. Проверяем валидность (как в твоем C# коде)
                            # В C# логика: если товара < min_qty -> добавляем
                            if cur_count < min_q:
                                needed = min_q - cur_count
                                
                                # Генерируем строки для добавления из source_text
                                # Если source_text это одна ссылка - она будет дублироваться
                                # Если source_text это список ключей - они будут браться по кругу
                                source_lines = [l for l in source_text.split('\n') if l.strip()]
                                if not source_lines: continue

                                to_add = []
                                src_len = len(source_lines)
                                for i in range(needed):
                                    to_add.append(source_lines[i % src_len])

                                # Формируем итоговый текст (Старое + Новое)
                                new_full_text = cur_text.strip() + "\n" + "\n".join(to_add)
                                new_full_text = new_full_text.strip()

                                # 5. Сохраняем
                                payload = {
                                    "csrf_token": csrf, "offer_id": oid, "node_id": real_node or lot['node_id'],
                                    "secrets": new_full_text,
                                    "auto_delivery": "on" if final_auto else "",
                                    "active": "on" if active_lot else "",
                                    "save": "Сохранить"
                                }
                                if not active_lot: payload.pop("active", None)

                                async with session.post("https://funpay.com/lots/offerSave", data=payload, headers=POST_H, cookies=cookies) as pr:
                                    if pr.status == 200:
                                        log_msg.append(f"✅{offer_id}: +{len(to_add)}")
                                    else:
                                        log_msg.append(f"❌{offer_id}: {pr.status}")
                            
                            await asyncio.sleep(2)

                        # Обновляем статус
                        status = ", ".join(log_msg) if log_msg else "✅ Проверено"
                        async with app.state.pool.acquire() as c:
                            await c.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", status[:100], uid)

                    except Exception as e:
                        log_debug(f"Task Err {uid}: {e}")
            
            await asyncio.sleep(20)
        except: await asyncio.sleep(30)
