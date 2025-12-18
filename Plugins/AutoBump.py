import asyncio
import re
import html as html_lib
import random
import json
import aiohttp
import traceback
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autobump", tags=["AutoBump Plugin"])

class CloudBumpSettings(BaseModel):
    golden_key: str
    node_ids: list[str]
    active: bool

# --- DB HELPERS ---
async def update_status(pool, uid, msg, next_delay=None, disable=False):
    """
    Обновляет статус задачи в БД.
    next_delay: время ожидания в секундах (например, 14400 для 4 часов).
    disable: если True, выключает авто-поднятие (например, при ошибке ключа).
    """
    try:
        # Обрезаем сообщение, чтобы влезло в БД
        clean_msg = str(msg)[:150]
        
        # Логируем важные события в консоль сервера
        if "❌" in clean_msg or "✅" in clean_msg:
            print(f"[AutoBump {uid}] {clean_msg}", flush=True)
            
        async with pool.acquire() as conn:
            if disable:
                # Критическая ошибка -> выключаем задачу
                await conn.execute(
                    "UPDATE autobump_tasks SET status_message=$1, is_active=FALSE WHERE user_uid=$2", 
                    clean_msg, uid
                )
            elif next_delay is not None:
                # === УМНЫЙ ТАЙМЕР ===
                # Добавляем 2-3 минуты (120-180 сек) к ЛЮБОМУ ожиданию.
                # Это имитирует "человеческую" задержку и защищает от спама.
                human_jitter = random.randint(120, 180) 
                
                # Итоговое время ожидания
                final_delay = next_delay + human_jitter
                
                # Обновляем время следующего запуска
                await conn.execute(
                    "UPDATE autobump_tasks SET status_message=$1, last_bump_at=NOW(), next_bump_at=NOW() + interval '1 second' * $2 WHERE user_uid=$3", 
                    clean_msg, final_delay, uid
                )
            else:
                # Просто обновляем текст статуса (без изменения таймера)
                await conn.execute("UPDATE autobump_tasks SET status_message=$1 WHERE user_uid=$2", clean_msg, uid)
    except Exception as e:
        print(f"[AutoBump DB Error] {e}")

# --- PARSERS ---
def parse_wait_time(text: str) -> int:
    """Парсит время ожидания из текста ошибки FunPay (напр. 'Подождите 1 час')."""
    if not text: return 14400 # Дефолт 4 часа
    text = text.lower()
    
    h = re.search(r'(\d+)\s*(?:ч|h|hour)', text)
    m = re.search(r'(\d+)\s*(?:м|min|мин)', text)
    
    hours = int(h.group(1)) if h else 0
    minutes = int(m.group(1)) if m else 0
    
    total = (hours * 3600) + (minutes * 60)
    
    # Если цифр нет, но есть слова "подождите", ставим 1 час
    if total == 0 and ("подож" in text or "wait" in text): return 3600
    
    return total if total > 0 else 14400

def get_tokens_smart(html: str):
    """Умный парсер CSRF-токена и GameID со страницы лота."""
    csrf, gid = None, None
    
    # 1. Ищем CSRF (Приоритет: data-app-data -> meta tags -> window._csrf)
    m = re.search(r'data-app-data="([^"]+)"', html)
    if m:
        try:
            blob = html_lib.unescape(m.group(1))
            t = re.search(r'"csrf-token"\s*:\s*"([^"]+)"', blob) or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', blob)
            if t: csrf = t.group(1)
        except: pass

    if not csrf:
        patterns = [
            r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']',
            r'name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
            r'window\._csrf\s*=\s*["\']([^"\']+)["\']'
        ]
        for p in patterns:
            m = re.search(p, html)
            if m: csrf = m.group(1); break

    # 2. Ищем GameID (для запроса на поднятие)
    m = re.search(r'class="[^"]*js-lot-raise"[^>]*data-game=["\'](\d+)["\']', html)
    if m: gid = m.group(1)
    
    if not gid:
        m = re.search(r'data-game-id=["\'](\d+)["\']', html) or re.search(r'data-game=["\'](\d+)["\']', html)
        if m: gid = m.group(1)

    return gid, csrf

# --- WORKER (SMART & SECURE) ---
async def worker(app):
    await asyncio.sleep(5) # Ждем старта БД
    print(">>> [AutoBump] WORKER STARTED (Smart Timer Enabled)", flush=True)
    
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=60)

    # Заголовки как у браузера
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://funpay.com"
    }

    while True:
        try:
            # Ждем инициализации пула БД
            if not hasattr(app.state, 'pool') or not app.state.pool:
                await asyncio.sleep(2); continue
            pool = app.state.pool
            
            tasks = []
            async with pool.acquire() as conn:
                # Выбираем задачи, у которых подошло время (next_bump_at <= NOW)
                # И только если у пользователя есть активная лицензия!
                tasks = await conn.fetch("""
                    SELECT t.user_uid, t.encrypted_golden_key, t.node_ids 
                    FROM autobump_tasks t
                    WHERE t.is_active = TRUE 
                    AND (t.next_bump_at IS NULL OR t.next_bump_at <= NOW())
                    AND EXISTS (
                        SELECT 1 FROM licenses l 
                        WHERE l.user_uid = t.user_uid 
                        AND l.status = 'active' 
                        AND (l.expires IS NULL OR l.expires >= CURRENT_DATE)
                    )
                    ORDER BY t.next_bump_at ASC NULLS FIRST
                    LIMIT 3
                """)

            if not tasks:
                await asyncio.sleep(3); continue

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                for task in tasks:
                    uid = task['user_uid']
                    
                    # 1. Ставим статус "В работе" (на 15 мин), чтобы другой воркер не взял эту же задачу
                    await update_status(pool, uid, "⚡ Поднимаю...", 900)

                    try:
                        # Расшифровываем ключ
                        try:
                            key = decrypt_data(task['encrypted_golden_key'])
                        except:
                            await update_status(pool, uid, "❌ Ошибка ключа", disable=True)
                            continue

                        cookies = {"golden_key": key}
                        # Парсим список лотов
                        raw_nodes = str(task['node_ids']).split(',')
                        nodes = [n.strip() for n in raw_nodes if n.strip().isdigit()]

                        if not nodes:
                            await update_status(pool, uid, "❌ Нет лотов", disable=True)
                            continue

                        final_msg = ""
                        final_delay = 0 # Если останется 0 -> будет 4 часа по дефолту
                        success_cnt = 0
                        global_csrf = None # Кэш CSRF для этого прохода

                        for idx, node in enumerate(nodes):
                            url = f"https://funpay.com/lots/{node}/trade"
                            get_hdrs = HEADERS.copy()
                            get_hdrs["Referer"] = url

                            # --- GET ЗАПРОС ---
                            html = ""
                            for attempt in range(2):
                                try:
                                    async with session.get(url, headers=get_hdrs, cookies=cookies) as resp:
                                        # Проверка на слет сессии
                                        if "login" in str(resp.url):
                                            final_msg = "❌ Сессия истекла"
                                            await update_status(pool, uid, "❌ Сессия истекла (выключено)", disable=True)
                                            break 
                                        
                                        if resp.status == 404: break # Лот удален, идем дальше
                                        if resp.status != 200:
                                            if attempt == 1: 
                                                final_msg = f"❌ HTTP {resp.status}"; final_delay = 600
                                            await asyncio.sleep(1); continue
                                        
                                        html = await resp.text()
                                        break
                                except:
                                    if attempt == 1: 
                                        final_msg = "❌ Ошибка сети"; final_delay = 600
                                    await asyncio.sleep(1)
                            
                            if "Сессия истекла" in final_msg: break 

                            # --- ПАРСИНГ ---
                            gid, csrf = get_tokens_smart(html)
                            
                            # Если CSRF не нашли, пробуем взять с главной (Fallback)
                            if not csrf:
                                if global_csrf: 
                                    csrf = global_csrf
                                else:
                                    try:
                                        async with session.get("https://funpay.com/", headers=get_hdrs, cookies=cookies) as r_home:
                                            _, h_csrf = get_tokens_smart(await r_home.text())
                                            if h_csrf: global_csrf = h_csrf; csrf = h_csrf
                                    except: pass

                            # Проверка на таймер (без ID лота)
                            if not gid and ("Подождите" in html or "Wait" in html):
                                m = re.search(r'class="[^"]*ajax-alert-danger"[^>]*>(.*?)</div>', html, re.DOTALL)
                                alert = m.group(1).strip() if m else "Таймер FunPay"
                                w = parse_wait_time(alert)
                                # Если нашли таймер, запоминаем максимальное время ожидания
                                if w > final_delay: 
                                    final_delay = w
                                    final_msg = f"⏳ {alert}"
                                continue
                            
                            if not gid: continue # Не нашли ничего полезного

                            # --- POST ЗАПРОС (ПОДНЯТИЕ) ---
                            post_hdrs = HEADERS.copy()
                            post_hdrs["Referer"] = url
                            post_hdrs["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
                            if csrf: post_hdrs["X-CSRF-Token"] = csrf
                            
                            payload = {"game_id": gid, "node_id": node}
                            if csrf: payload["csrf_token"] = csrf

                            try:
                                async with session.post("https://funpay.com/lots/raise", data=payload, cookies=cookies, headers=post_hdrs) as p_resp:
                                    txt = await p_resp.text()
                                    try:
                                        js = json.loads(txt)
                                        if not js.get("error"):
                                            success_cnt += 1
                                        else:
                                            # Ошибка от FP (часто таймер)
                                            msg = js.get("msg", "")
                                            w = parse_wait_time(msg)
                                            if w > 0:
                                                if w > final_delay: 
                                                    final_delay = w
                                                    final_msg = f"⏳ {msg}"
                                            else:
                                                final_msg = f"⚠️ FP: {msg[:30]}"
                                    except:
                                        # Если вернулся не JSON, ищем слово "поднято"
                                        if "поднято" in txt.lower(): success_cnt += 1
                            except:
                                final_msg = "❌ сбой POST"; final_delay = 600

                            # Пауза между лотами
                            await asyncio.sleep(random.uniform(1.2, 2.5))

                        # --- ИТОГИ ЦИКЛА ---
                        if "Сессия истекла" in final_msg:
                            pass # Уже обработано (disable=True)
                        elif final_delay > 0:
                            # Есть явный таймер от FP
                            msg = final_msg or "⏳ Ожидание"
                            # update_status сама добавит +2-3 минуты к final_delay
                            await update_status(pool, uid, msg, final_delay)
                        elif success_cnt > 0:
                            # Успешно подняли -> ждем 4 часа
                            # update_status добавит +2-3 минуты
                            await update_status(pool, uid, f"✅ Поднято: {success_cnt}", 14400) 
                        elif final_msg:
                            # Какая-то ошибка
                            await update_status(pool, uid, final_msg, 1800)
                        else:
                            # Ничего не произошло (например, лоты удалены)
                            await update_status(pool, uid, "⚠️ Нет действий", 3600)

                    except Exception as e:
                        traceback.print_exc()
                        await update_status(pool, uid, f"⚠️ Сбой воркера: {str(e)[:50]}", 600)

            await asyncio.sleep(1) # Короткая пауза перед следующим поиском задач

        except Exception as ex:
            print(f"[CRITICAL WORKER ERROR] {ex}")
            await asyncio.sleep(5)

# --- API ---
async def get_plugin_user(request: Request):
    return await get_current_user_raw(request.app, request)

@router.post("/set")
async def set_bump(data: CloudBumpSettings, req: Request, u=Depends(get_plugin_user)):
    async with req.app.state.pool.acquire() as conn:
        enc = encrypt_data(data.golden_key)
        ns = ",".join(data.node_ids)
        
        # При сохранении настроек сразу ставим задачу в очередь (next_bump_at = NOW)
        await conn.execute("""
            INSERT INTO autobump_tasks 
            (user_uid, encrypted_golden_key, node_ids, is_active, next_bump_at, status_message) 
            VALUES ($1, $2, $3, $4, NOW(), 'Запуск...') 
            ON CONFLICT (user_uid) DO UPDATE SET 
                encrypted_golden_key=EXCLUDED.encrypted_golden_key, 
                node_ids=EXCLUDED.node_ids, 
                is_active=EXCLUDED.is_active, 
                next_bump_at=NOW(), 
                status_message='Настройки обновлены'
        """, u['uid'], enc, ns, data.active)
    return {"status": "success"}

@router.post("/force_check")
async def force(req: Request, u=Depends(get_plugin_user)):
    async with req.app.state.pool.acquire() as conn:
        # Принудительный сброс таймера
        await conn.execute("UPDATE autobump_tasks SET next_bump_at=NOW(), status_message='Принудительный запуск...' WHERE user_uid=$1", u['uid'])
    return {"status": "success"}

@router.get("/status")
async def get_stat(req: Request, u=Depends(get_plugin_user)):
    """
    Возвращает текущий статус задачи для клиента.
    Важно возвращать реальное next_bump_at, чтобы клиент мог показать таймер.
    """
    async with req.app.state.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT is_active, next_bump_at, status_message FROM autobump_tasks WHERE user_uid=$1", u['uid'])
    
    if not r: 
        return {
            "is_active": False, 
            "next_bump": None, 
            "status_message": "Не настроено"
        }
    
    return {
        "is_active": r['is_active'], 
        "next_bump": r['next_bump_at'], # FastAPI автоматически сериализует datetime в ISO строку
        "status_message": r['status_message']
    }
