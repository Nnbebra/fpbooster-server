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

# --- ВОРКЕР (ОБНОВЛЕННАЯ ЛОГИКА) ---
async def worker(app):
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
                        
                        is_changed = False
                        log_msg = []

                        for lot in lots_conf:
                            pool = lot.get('secrets_pool', [])
                            offer_id = lot['offer_id']
                            min_q = int(lot.get('min_qty', 5))
                            
                            # Загрузка страницы
                            edit_url = f"https://funpay.com/lots/offerEdit?offer={offer_id}"
                            async with session.get(edit_url, headers=HEADERS, cookies=cookies) as r:
                                html = await r.text()
                            
                            oid, current_text, csrf, is_act, is_aut, real_node = parse_edit_page(html)
                            if not csrf: continue

                            # Разбиваем текущее содержимое на строки
                            current_lines = [l.strip() for l in current_text.split('\n') if l.strip()]
                            
                            # --- УМНАЯ ПРОВЕРКА СООТВЕТСТВИЯ ---
                            # Если в пуле (в софте) есть товары, проверяем, не изменились ли они
                            if pool and current_lines:
                                # Сравниваем первую строку на FunPay с первой строкой в нашем пуле.
                                # Если они разные — значит пользователь сменил ссылку в софте.
                                if current_lines[0] != pool[0]:
                                    log_debug(f"[{uid}] Смена товара в оффере {offer_id}. Очищаю старые ссылки.")
                                    current_lines = [] 

                            # --- ПОПОЛНЕНИЕ ---
                            if len(current_lines) < min_q and pool:
                                needed_count = min_q - len(current_lines)
                                # Берем нужное кол-во ссылок из пула
                                to_add = pool[:needed_count]
                                remaining_pool = pool[needed_count:]

                                # Объединяем (дубликаты РАЗРЕШЕНЫ, так как это бесконечный товар)
                                final_list = current_lines + to_add
                                new_secrets_text = "\n".join(final_list)

                                # Сохранение
                                payload = {
                                    "csrf_token": csrf, "offer_id": oid, "node_id": real_node or lot['node_id'],
                                    "secrets": new_secrets_text, 
                                    "auto_delivery": "on", # ВСЕГДА ВКЛЮЧАЕМ
                                    "active": "on" if is_act else "", 
                                    "save": "Сохранить"
                                }
                                if not is_act: payload.pop("active", None)

                                post_h = POST_HEADERS.copy()
                                post_h["Referer"] = edit_url
                                
                                async with session.post("https://funpay.com/lots/offerSave", data=payload, cookies=cookies, headers=post_h) as pr:
                                    if pr.status == 200:
                                        log_msg.append(f"✅ {offer_id}: +{len(to_add)}")
                                        lot['secrets_pool'] = remaining_pool
                                        is_changed = True
                                    else:
                                        log_msg.append(f"❌ {offer_id}: {pr.status}")
                            
                            await asyncio.sleep(2)

                        if is_changed:
                            async with app.state.pool.acquire() as c:
                                await c.execute("UPDATE autorestock_tasks SET lots_config=$1::jsonb WHERE user_uid=$2", json.dumps(lots_conf), uid)
                        
                        status = ", ".join(log_msg) if log_msg else "✅ Проверено"
                        async with app.state.pool.acquire() as c_upd:
                            await c_upd.execute("UPDATE autorestock_tasks SET status_message=$1, last_check_at=NOW() WHERE user_uid=$2", status[:100], uid)

                    except Exception as e:
                        log_debug(f"Worker task error {uid}: {e}")
            
            await asyncio.sleep(20)
        except Exception as e:
            log_debug(f"Worker critical error: {e}")
            await asyncio.sleep(30)
