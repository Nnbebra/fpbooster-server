import asyncio
import re
import html as html_lib
import random
from datetime import datetime, timedelta
import aiohttp
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from auth.guards import get_current_user as get_current_user_raw 
from utils_crypto import encrypt_data, decrypt_data 

router = APIRouter(prefix="/api/plus/autobump", tags=["AutoBump Plugin"])

# --- Модели данных ---
class CloudBumpSettings(BaseModel):
    golden_key: str
    node_ids: list[str]
    active: bool

# --- Вспомогательные функции ---

def parse_funpay_wait_time(text: str) -> int:
    """
    Парсит сообщение FunPay (напр. 'Подождите 3 ч. 15 мин.') и возвращает секунды.
    Если время не найдено, возвращает дефолтные 4 часа (14400 сек).
    """
    if not text: return 14400
    text = text.lower()
    
    hours = 0
    minutes = 0
    
    # Поиск часов (ч, час, hour, h)
    h_match = re.search(r'(\d+)\s*(?:ч|h|hour|час)', text)
    if h_match: hours = int(h_match.group(1))
    
    # Поиск минут (м, мин, min, m)
    m_match = re.search(r'(\d+)\s*(?:м|min|мин)', text)
    if m_match: minutes = int(m_match.group(1))
    
    total_seconds = (hours * 3600) + (minutes * 60)
    
    # Если цифр не нашлось, но есть слово "подождите", считаем что это 1 час (на всякий случай)
    if total_seconds == 0 and ("подож" in text or "wait" in text):
        return 3600
        
    return total_seconds if total_seconds > 0 else 14400 # Дефолт 4 часа

async def update_task_status(pool, uid, message, next_run_in_seconds=None):
    """Обновляет статус задачи и время следующего запуска в БД"""
    async with pool.acquire() as conn:
        if next_run_in_seconds is not None:
            # Добавляем 2-5 минут рандома к времени ожидания для безопасности
            jitter = random.randint(120, 300) 
            final_delay = next_run_in_seconds + jitter
            
            await conn.execute("""
                UPDATE autobump_tasks 
                SET status_message = $1, 
                    last_bump_at = NOW(),
                    next_bump_at = NOW() + interval '1 second' * $2
                WHERE user_uid = $3
            """, message, final_delay, uid)
        else:
            # Просто обновляем текст статуса
            await conn.execute("UPDATE autobump_tasks SET status_message = $1 WHERE user_uid = $2", message, uid)

# --- Основной Воркер ---

async def worker(app):
    print(">>> [AutoBump] Cloud Worker Started")
    
    # Заголовки как у браузера, чтобы не палиться
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://funpay.com"
    }

    while True:
        try:
            pool = app.state.pool
            
            # 1. Выбираем задачи, у которых наступило время (next_bump_at <= NOW) ИЛИ они только созданы (NULL)
            async with pool.acquire() as conn:
                tasks = await conn.fetch("""
                    SELECT user_uid, encrypted_golden_key, node_ids 
                    FROM autobump_tasks 
                    WHERE is_active = TRUE 
                    AND (next_bump_at IS NULL OR next_bump_at <= NOW())
                    ORDER BY next_bump_at ASC
                    LIMIT 10
                """)

            if not tasks:
                await asyncio.sleep(5)
                continue

            async with aiohttp.ClientSession() as session:
                for task in tasks:
                    uid = task['user_uid']
                    try:
                        # Дешифруем ключ
                        golden_key = decrypt_data(task['encrypted_golden_key'])
                        cookies = {"golden_key": golden_key}
                        
                        # Парсим ID лотов
                        nodes = [n.strip() for n in task['node_ids'].split(',') if n.strip()]
                        if not nodes:
                            await update_task_status(pool, uid, "❌ Нет NodeID", 3600)
                            continue

                        # Берем первый лот для проверки (обычно поднимаются все разом через кнопку, но проверим первый)
                        node_id = nodes[0]
                        
                        await update_task_status(pool, uid, "🔄 Проверка FunPay...")

                        # 1. Получаем страницу трейда для Game ID и CSRF
                        async with session.get(f"https://funpay.com/lots/{node_id}/trade", headers=HEADERS, cookies=cookies) as resp:
                            if resp.status != 200:
                                await update_task_status(pool, uid, f"Ошибка доступа ({resp.status})", 600)
                                continue
                            html = await resp.text()

                        # 2. Ищем сообщение "Подождите..." прямо на странице (бывает и такое)
                        if "ajax-alert-danger" in html and "Подождите" in html:
                             # Вытаскиваем текст из div
                             match = re.search(r'class="ajax-alert-danger"[^>]*>(.*?)</div>', html)
                             msg = match.group(1) if match else "Подождите..."
                             wait_sec = parse_funpay_wait_time(msg)
                             await update_task_status(pool, uid, f"⏳ {msg}", wait_sec)
                             continue

                        # 3. Парсим CSRF и GameID (используем упрощенную логику, аналогичную C#)
                        csrf = None
                        game_id = None
                        
                        # (Упрощенный парсинг для примера, лучше взять регулярки из твоего C# кода)
                        app_data_match = re.search(r'data-app-data="([^"]+)"', html)
                        if app_data_match:
                            app_data = html_lib.unescape(app_data_match.group(1))
                            if '"csrf-token"' in app_data:
                                csrf = re.search(r'"csrf-token":"([^"]+)"', app_data).group(1)
                            if '"game-id"' in app_data:
                                game_id = re.search(r'"game-id":(\d+)', app_data).group(1)

                        if not csrf or not game_id:
                            # Пробуем fallback на data-атрибуты
                            gid_match = re.search(r'data-game-id="(\d+)"', html)
                            if gid_match: game_id = gid_match.group(1)
                            
                            if not csrf or not game_id:
                                await update_task_status(pool, uid, "❌ Ошибка парсинга данных", 1800) # Повтор через 30 мин
                                continue

                        # 4. Пробуем поднять
                        payload = {
                            "game_id": game_id,
                            "node_id": node_id,
                            "csrf_token": csrf
                        }
                        
                        async with session.post("https://funpay.com/lots/raise", data=payload, headers=HEADERS, cookies=cookies) as post_resp:
                            resp_json = await post_resp.json(content_type=None) # content_type=None чтобы не падало если text/html
                            
                            if not post_resp.ok:
                                await update_task_status(pool, uid, f"HTTP Error {post_resp.status}", 600)
                                continue

                            # Анализ ответа
                            # {"msg": "Подождите 3 часа.", "error": 1} или {"msg": "Поднято", "error": 0}
                            msg = resp_json.get("msg", "")
                            error = resp_json.get("error", 0)

                            if error == 0:
                                # УСПЕХ -> ставим таймер на 4 часа
                                await update_task_status(pool, uid, "✅ Успешно поднято", 14400) # 4 часа
                            else:
                                # ОШИБКА (Скорее всего таймер)
                                wait_sec = parse_funpay_wait_time(msg)
                                await update_task_status(pool, uid, f"⏳ FunPay: {msg}", wait_sec)

                    except Exception as e:
                        print(f"[ERR] Task {uid}: {e}")
                        await update_task_status(pool, uid, "⚠️ Сбой воркера", 300)

            # Небольшая пауза между пачками задач
            await asyncio.sleep(2)
            
        except Exception as global_ex:
            print(f"[CRIT] Worker Loop Error: {global_ex}")
            await asyncio.sleep(10)

# --- API Эндпоинты ---

async def get_plugin_user(request: Request):
    return await get_current_user_raw(request.app, request)

@router.post("/set")
async def set_autobump(data: CloudBumpSettings, request: Request, user=Depends(get_plugin_user)):
    async with request.app.state.pool.acquire() as conn:
        enc_key = encrypt_data(data.golden_key)
        nodes_str = ",".join(data.node_ids)
        
        # Upsert (Вставка или Обновление)
        await conn.execute("""
            INSERT INTO autobump_tasks (user_uid, encrypted_golden_key, node_ids, is_active, next_bump_at, status_message)
            VALUES ($1, $2, $3, $4, NOW(), 'Инициализация...')
            ON CONFLICT (user_uid) DO UPDATE SET 
                encrypted_golden_key = EXCLUDED.encrypted_golden_key,
                node_ids = EXCLUDED.node_ids,
                is_active = EXCLUDED.is_active,
                next_bump_at = NOW(), -- Сбрасываем таймер на "сейчас" при обновлении настроек
                status_message = 'Настройки обновлены'
        """, user['uid'], enc_key, nodes_str, data.active)
        
    return {"status": "success", "active": data.active}

@router.post("/force_check")
async def force_check_autobump(request: Request, user=Depends(get_plugin_user)):
    """Кнопка 'Проверить сейчас': сбрасывает таймер, чтобы воркер подхватил задачу немедленно"""
    async with request.app.state.pool.acquire() as conn:
        await conn.execute("""
            UPDATE autobump_tasks 
            SET next_bump_at = NOW(), 
                status_message = 'Запрос проверки...' 
            WHERE user_uid = $1
        """, user['uid'])
    return {"status": "success"}

@router.get("/status")
async def get_autobump_status(request: Request, user=Depends(get_plugin_user)):
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_active, last_bump_at, next_bump_at, status_message FROM autobump_tasks WHERE user_uid=$1", user['uid'])
    
    if not row: return {"is_active": False}
    
    return {
        "is_active": row['is_active'],
        "last_bump": row['last_bump_at'],
        "next_bump": row['next_bump_at'],
        "status_message": row['status_message'] or "Ожидание"
    }
