import os
import secrets
import pathlib
import asyncio
import json
from typing import Optional, Literal
from datetime import date, datetime, timedelta

import asyncpg
from fastapi import FastAPI, HTTPException, Request, Depends, Form, Header
from fastapi.responses import HTMLResponse, RedirectResponse, Response, PlainTextResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, validator

# --- ИМПОРТЫ ПРОЕКТА ---
from guards import admin_guard_ui           # Админка
from auth.jwt_utils import verify_password, make_jwt
# Добавляем этот импорт:
from auth.guards import get_current_user 
import groups_router

# --- Вспомогательная функция (ВСТАВИТЬ ПЕРЕД ОБЪЯВЛЕНИЕМ РОУТОВ) ---
async def get_user_safe(request: Request):
    try:
        return await get_current_user(request)
    except:
        return None

# --- ИМПОРТ ПЛАГИНОВ ---
from Plugins import AutoBump, AutoRestock

async def get_current_user_raw(app, request: Request):
    try:
        # Убираем app из вызова
        return await get_current_user(request)
    except:
        return None


async def current_user(request: Request):
    return await get_current_user(request)

app = FastAPI(title="FPBooster License Server", version="1.6.0")
templates = Jinja2Templates(directory="templates")

async def ui_guard(request: Request):
    # Исправлено: убрали второй аргумент, добавили await
    return await admin_guard_ui(request)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # 1. Получаем пользователя (безопасно)
    user = await get_user_safe(request)
    
    # 2. Логика статистики (оставляем твою или берем эту)
    async with request.app.state.pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
    
    stats = {"users": users_count, "runs": users_count * 12} 

    # 3. Передаем user в шаблон
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "user": user,       # <--- ВАЖНО: это чинит шапку
        "stats": stats
    })

# --- РОУТЕРЫ АВТОРИЗАЦИИ ---
from auth.users_router import router as users_router
from auth.email_confirm import router as email_confirm_router

app.include_router(users_router, tags=["auth"])
app.include_router(email_confirm_router, tags=["email"])

# --- КОНФИГУРАЦИЯ ---
DOWNLOAD_URL = os.getenv("DOWNLOAD_URL", "").strip()
app.state.DOWNLOAD_URL = DOWNLOAD_URL

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
if not ADMIN_TOKEN:
    raise RuntimeError("ADMIN_TOKEN is not set")
app.state.ADMIN_TOKEN = ADMIN_TOKEN

DB_URL = os.getenv("DATABASE_URL", "").strip()
if not DB_URL:
    raise RuntimeError("DATABASE_URL is not set")

# --- РОУТЕРЫ ФУНКЦИОНАЛА ---
from creators import router as creators_router
from admin_creators import router as admin_creators_router
from referrals import router as referrals_router
from purchases_router import router as purchases_router
from buy import router as buy_router
from payments import router as payments_router

app.include_router(payments_router, tags=["payments"])
app.include_router(buy_router, tags=["buy"])
app.include_router(creators_router)
app.include_router(admin_creators_router)
app.include_router(referrals_router)
app.include_router(purchases_router, tags=["purchases"])
app.include_router(groups_router.router)

# --- ПОДКЛЮЧЕНИЕ ПЛАГИНОВ ---
# Это добавит API методы плагина (например /api/plus/autobump/set)
app.include_router(AutoBump.router)
app.include_router(AutoRestock.router)

# --- СТАТИКА ---
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/templates_css", StaticFiles(directory="templates_css"), name="templates_css")
app.mount("/JavaScript", StaticFiles(directory="JavaScript"), name="javascript")

# --- МОДЕЛИ ---

class LauncherLogin(BaseModel):
    email: str
    password: str
    hwid: str

# ==========================================================
#             STARTUP / SHUTDOWN
# ==========================================================

@app.on_event("startup")
async def startup():
    # 1. Подключение к БД
    app.state.pool = await asyncpg.create_pool(dsn=DB_URL, min_size=1, max_size=5, command_timeout=10)
    
    # 2. Запуск фоновых задач плагинов (AutoBump)
    # Передаем 'app', чтобы воркер имел доступ к пулу БД (app.state.pool)
    asyncio.create_task(AutoBump.worker(app))
    asyncio.create_task(AutoRestock.worker(app))

@app.on_event("shutdown")
async def shutdown():
    pool = app.state.pool
    if pool: await pool.close()

def admin_guard_api(request: Request):
    token = request.headers.get("x-admin-token")
    if not app.state.ADMIN_TOKEN: raise HTTPException(500, "ADMIN_TOKEN not configured")
    if token != app.state.ADMIN_TOKEN: raise HTTPException(403, "Invalid admin token")
    return True

@app.get("/api/health")
async def health():
    try:
        async with app.state.pool.acquire() as conn: await conn.execute("SELECT 1;")
        return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}
    except Exception as e: raise HTTPException(500, f"DB error: {e}")

# ==========================================================
#             ЭНДПОИНТЫ ДЛЯ ЛАУНЧЕРА
# ==========================================================


# --- ФУНКЦИЯ ЗАЩИТЫ API (Добавить после импортов) ---
async def get_current_user_api(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization Header")
    try:
        scheme, token = auth_header.split()
        if scheme.lower() != 'bearer':
            raise HTTPException(status_code=401, detail="Invalid Auth Scheme")
        
        # Расшифровываем токен
        payload = decode_jwt(token)
        if not payload:
             raise HTTPException(status_code=401, detail="Invalid Token")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Auth Failed")




@app.post("/api/launcher/login")
async def launcher_login(data: LauncherLogin, request: Request):
    email = data.email.strip().lower()
    async with request.app.state.pool.acquire() as conn:
        # 1. Проверяем пользователя
        user = await conn.fetchrow("SELECT id, uid, password_hash, username FROM users WHERE email=$1", email)
        
        if not user or not verify_password(data.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Неверный логин или пароль")

        # 2. Проверяем наличие АКТИВНОЙ группы (Подписки)
        # Берем самую "крутую" группу (с максимальным access_level), если их несколько
        active_sub = await conn.fetchrow("""
            SELECT ug.expires_at, g.name as group_name, g.access_level
            FROM user_groups ug
            JOIN groups g ON ug.group_id = g.id
            WHERE ug.user_uid = $1 
              AND ug.is_active = TRUE 
              AND ug.expires_at > NOW()
            ORDER BY g.access_level DESC
            LIMIT 1
        """, user["uid"])

        if not active_sub:
             raise HTTPException(status_code=403, detail="Нет активной подписки. Купите доступ на сайте.")

        # 3. (Опционально) Обновляем HWID в таблице users (если добавлял колонку hwid)
        # Если колонки hwid в users нет, закомментируй строку ниже, чтобы не было ошибки 500
        try:
            await conn.execute("UPDATE users SET hwid=$1 WHERE uid=$2", data.hwid, user["uid"])
        except:
            pass # Игнорируем ошибку, если колонки hwid нет

        # 4. Генерируем токен
        token = make_jwt(user["id"], email)
        
        return {
            "status": "success",
            "username": user["username"],
            "token": token,
            "expires": str(active_sub["expires_at"].date()),
            "group": active_sub["group_name"]
        }
# --- API СПИСОК ПРОДУКТОВ ---
@app.get("/api/client/products")
async def get_client_products(request: Request, user_data=Depends(current_user)):
    uid = user_data["uid"]
    
    async with request.app.state.pool.acquire() as conn:
        # Получаем максимальный уровень доступа пользователя
        # 1 = Basic/Standard, 2 = Plus, 3 = Alpha/Admin
        access_level = await conn.fetchval("""
            SELECT COALESCE(MAX(g.access_level), 0)
            FROM user_groups ug
            JOIN groups g ON ug.group_id = g.id
            WHERE ug.user_uid = $1 
              AND ug.is_active = TRUE 
              AND ug.expires_at > NOW()
        """, uid)

    # Логика доступа
    has_standard = access_level >= 1
    has_plus = access_level >= 2
    has_alpha = access_level >= 3

    products = []

    # Standard (Доступен всем с подпиской)
    products.append({
        "id": "standard",
        "name": "FPBooster Standard",
        "description": "Стабильная версия 1.16.5",
        "image_url": "pack://application:,,,/Assets/FPBoosterDef.png",
        "is_available": has_standard,
        "download_url": "/api/client/get-core?ver=standard"
    })

    # Plus (Уровень 2+)
    products.append({
        "id": "plus",
        "name": "FPBooster Plus",
        "description": "Расширенная версия",
        "image_url": "pack://application:,,,/Assets/FPBooster+Def.png",
        "is_available": has_plus,
        "download_url": "/api/client/get-core?ver=plus"
    })

    # Alpha (Уровень 3+)
    products.append({
        "id": "alpha",
        "name": "FPBooster Alpha",
        "description": "Бета-версия (Early Access)",
        "image_url": "pack://application:,,,/Assets/FPBoosterAlpha.png",
        "is_available": has_alpha, 
        "download_url": "/api/client/get-core?ver=alpha"
    })

    return products

# Твой секретный ключ
# Твой секретный ключ (убедись, что он совпадает с тем, что в лаунчере)
SERVER_SIDE_AES_KEY = "15345172281214561882123456789999"

@app.get("/api/client/get-core")
async def get_client_core(request: Request, ver: str = "standard", user_data = Depends(current_user)):
    # 1. Проверяем подписку
    async with request.app.state.pool.acquire() as conn:
        active = await conn.fetchval("""
            SELECT COUNT(*) FROM user_groups 
            WHERE user_uid=$1 AND is_active=TRUE AND expires_at > NOW()
        """, user_data["uid"])
        
        if active == 0:
             raise HTTPException(403, "No active license")

    # 2. Выбираем файл
    filename = "FPBooster.dll.enc"
    if ver == "alpha": filename = "FPBooster_Alpha.dll.enc"
    elif ver == "plus": filename = "FPBooster_Plus.dll.enc"

    # Путь к папке с билдами
    protected_folder = os.path.join(BASE_DIR, "protected_builds") 
    file_path = os.path.join(protected_folder, filename)
    
    # Фолбэк (если запрошенного файла нет, отдаем обычный)
    if not os.path.exists(file_path):
        file_path = os.path.join(protected_folder, "FPBooster.dll.enc")

    if not os.path.exists(file_path):
        print(f"CRITICAL: File not found: {file_path}")
        raise HTTPException(500, "Build not found on server")

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    return Response(
        content=file_bytes, 
        media_type="application/octet-stream",
        headers={"X-Decryption-Key": SERVER_SIDE_AES_KEY} 
    )

# --- ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ---
class UserProfileData(BaseModel):
    uid: str 
    username: str
    email: str
    group: Optional[str] = "User"
    expires: Optional[str]
    avatar_url: Optional[str] = None

@app.get("/api/client/profile", response_model=UserProfileData)
async def get_client_profile(request: Request, user_data=Depends(current_user)):
    target_uid = user_data["uid"]
    
    async with request.app.state.pool.acquire() as conn:
        user_row = await conn.fetchrow("""
            SELECT uid, username, email, user_group 
            FROM users 
            WHERE uid = $1
        """, target_uid)
        
        if not user_row:
            raise HTTPException(404, "User not found")

        license_row = await conn.fetchrow("""
            SELECT expires FROM licenses 
            WHERE user_uid = $1 AND status = 'active'
            ORDER BY expires DESC 
            LIMIT 1
        """, target_uid)

        expires_str = "Нет активной подписки"
        if license_row and license_row['expires']:
            if license_row['expires'] >= date.today():
                expires_str = license_row['expires'].strftime("%d.%m.%Y")
            else:
                expires_str = "Истекла"

        uid_str = str(user_row["uid"])
        group_display = user_row["user_group"] if user_row["user_group"] else "Пользователь"

        return {
            "uid": uid_str,
            "username": user_row["username"],
            "email": user_row["email"],
            "group": group_display,
            "expires": expires_str,
            "avatar_url": None 
        }

# ==========================================================
#             АДМИНКА, КЛЮЧИ И СКАЧИВАНИЕ (НОВОЕ)
# ==========================================================

# 1. АКТИВАЦИЯ КЛЮЧА (Замена старой активации)
@app.post("/api/license/activate")
async def activate_license(request: Request, token: Optional[str] = Form(None), key: Optional[str] = Form(None), user=Depends(current_user)):
    """
    Активирует ключ группы (из таблицы group_keys).
    Работает и для веба, и для лаунчера.
    """
    key_value = (token or key or "").strip()
    if not key_value: 
        raise HTTPException(400, "Key is required")
    
    try:
        async with request.app.state.pool.acquire() as conn:
            async with conn.transaction():
                # 1. Ищем ключ
                key_data = await conn.fetchrow("""
                    SELECT id, group_id, duration_days 
                    FROM group_keys 
                    WHERE key_code = $1 AND is_used = FALSE
                """, key_value)

                if not key_data:
                    raise HTTPException(404, "Ключ не найден или уже использован")

                group_id = key_data['group_id']
                duration = key_data['duration_days']
                
                # 2. Проверяем текущую подписку на эту группу
                existing = await conn.fetchrow("""
                    SELECT id, expires_at FROM user_groups 
                    WHERE user_uid = $1 AND group_id = $2
                """, user['uid'], group_id)

                now = datetime.now()
                
                # 3. Выдаем или продлеваем
                if existing:
                    # Если подписка активна - продлеваем от даты окончания
                    # Если истекла - продлеваем от текущего момента
                    current_expires = existing['expires_at']
                    if current_expires > now:
                        new_expires = current_expires + timedelta(days=duration)
                    else:
                        new_expires = now + timedelta(days=duration)
                    
                    await conn.execute("""
                        UPDATE user_groups 
                        SET expires_at = $1, is_active = TRUE, granted_at = NOW() 
                        WHERE id = $2
                    """, new_expires, existing['id'])
                else:
                    # Создаем новую запись
                    new_expires = now + timedelta(days=duration)
                    await conn.execute("""
                        INSERT INTO user_groups (user_uid, group_id, expires_at, is_active, granted_at)
                        VALUES ($1, $2, $3, TRUE, NOW())
                    """, user['uid'], group_id, new_expires)

                # 4. Помечаем ключ как использованный
                await conn.execute(
                    """
                    UPDATE group_keys 
                    SET is_used = TRUE, activated_by = $1
                    WHERE id = $2
                    """, user_uid, key_id
                )

                # 5. Логируем покупку (для истории)
                await conn.execute("""
                    INSERT INTO purchases (user_uid, plan, amount, currency, source, token_code, created_at) 
                    VALUES ($1, $2, 0, 'KEY', 'key_activation', $3, NOW())
                """, user['uid'], f"activation_group_{group_id}_{duration}d", key_value)

    except HTTPException: 
        raise 
    except Exception as e:
        print(f"CRITICAL ERROR in activate_license: {e}")
        raise HTTPException(500, f"SQL Error: {str(e)}")
    
    return RedirectResponse(url="/cabinet", status_code=302)


# ==========================================
#       FPBOOSTER PROTECTED API (FINAL)
# ==========================================

# 2. СПИСОК ТОВАРОВ
@app.get("/api/products")
async def get_api_products(request: Request):
    try:
        async with request.app.state.pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, name, description, image_url, is_available, required_access_level FROM products ORDER BY id ASC")
            
            products = []
            for row in rows:
                secure_url = f"/api/download/{row['id']}"
                products.append({
                    "id": str(row['id']),
                    "name": row['name'],
                    "description": row['description'],
                    "image_url": row['image_url'], 
                    "is_available": row['is_available'],
                    "download_url": secure_url,
                    # Можно добавить поле required_level, если лаунчеру это нужно
                })
            return products
    except Exception as e:
        print(f"API Error: {e}")
        return JSONResponse({"error": "Internal Server Error"}, status_code=500)


# 3. ЗАЩИЩЕННОЕ СКАЧИВАНИЕ (С ПРОВЕРКОЙ ГРУПП)
@app.get("/api/download/{product_id}")
async def download_product(
    request: Request,
    product_id: int, 
    x_hwid: Optional[str] = Header(None, alias="X-HWID"), 
    user_row = Depends(get_current_user)
):
    try:
        user_uid = user_row['uid'] 
        
        async with request.app.state.pool.acquire() as conn:
            # 1. Получаем инфо о продукте и требуемом уровне доступа
            prod = await conn.fetchrow("""
                SELECT exe_name, secret_key, name, is_available, required_access_level 
                FROM products WHERE id = $1
            """, product_id)
            
            if not prod:
                return JSONResponse({"error": "Product not found"}, status_code=404)

            if not prod['is_available']:
                 return JSONResponse({"error": "Product is temporarily unavailable"}, status_code=403)

            required_level = prod['required_access_level'] if prod['required_access_level'] else 1

            # 2. === ПРОВЕРКА ДОСТУПА (ГРУППЫ) ===
            # ИСПРАВЛЕНО: Добавлена проверка на NULL (вечный доступ)
            has_access = await conn.fetchval("""
                SELECT COUNT(*) 
                FROM user_groups ug
                JOIN groups g ON ug.group_id = g.id
                WHERE ug.user_uid = $1 
                  AND ug.is_active = TRUE 
                  AND (ug.expires_at IS NULL OR ug.expires_at > NOW())
                  AND g.access_level >= $2
            """, user_uid, required_level)

            if has_access == 0:
                 return JSONResponse({"error": f"NO_ACCESS: Required Level {required_level}"}, status_code=403)

            # 3. === HWID (Логика привязки) ===
            # Если в заголовках пришел HWID, можно его залогировать или обновить у юзера
            if x_hwid:
                # В таблице 'users' у тебя есть поле 'uid', можно добавить колонку 'last_hwid'
                # await conn.execute("UPDATE users SET last_login_hwid=$1 WHERE uid=$2", x_hwid, user_uid)
                pass

            # 4. === ОТДАЧА ФАЙЛА ===
            filename = prod['exe_name'] 
            # Фолбэк имен файлов для старой базы
            if not filename:
                if product_id == 1: filename = "FPBoosterPlus.dll"
                else: filename = "FPBoosterDefault.dll"

            # ПУТЬ К ФАЙЛАМ
            # protected_folder = "protected_builds" # Для локального теста
            protected_folder = "/opt/fpbooster/protected_builds" # Для продакшена

            file_path = os.path.join(protected_folder, filename)
            
            if not os.path.exists(file_path):
                print(f"CRITICAL: File missing at {file_path}") 
                return JSONResponse({"error": f"File missing on server: {filename}"}, status_code=404)

            key_to_send = prod['secret_key'] if prod['secret_key'] else ""

            headers = {
                "X-Encryption-Key": key_to_send,
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Access-Control-Expose-Headers": "X-Encryption-Key" # Важно для лаунчера
            }
            return FileResponse(file_path, headers=headers, media_type='application/octet-stream')

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": f"Server Error: {str(e)}"}, status_code=500)


# ==========================================
#           ВЕБ-АДМИНКА (Обновленная)
# ==========================================

@app.get("/admin", response_class=HTMLResponse)
async def admin_root(request: Request, _=Depends(ui_guard)):
    # Вместо лицензий редиректим на пользователей или ключи
    return RedirectResponse(url="/admin/users", status_code=302)

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if not app.state.ADMIN_TOKEN:
        return templates.TemplateResponse("login.html", {"request": request, "error": "ADMIN_TOKEN не настроен"}, status_code=500)
    if password != app.state.ADMIN_TOKEN:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный токен"}, status_code=401)
    
    resp = RedirectResponse(url="/admin/users", status_code=302)
    resp.set_cookie("admin_auth", app.state.ADMIN_TOKEN, httponly=True, samesite="lax", secure=True, max_age=7*24*3600)
    return resp

@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=302)
    resp.delete_cookie("admin_auth")
    return resp

# --- УПРАВЛЕНИЕ КЛЮЧАМИ (Вместо лицензий) ---

@app.get("/admin/keys", response_class=HTMLResponse)
async def admin_keys_list(request: Request, _=Depends(ui_guard)):
    """Страница со списком всех ключей"""
    async with app.state.pool.acquire() as conn:
        # Получаем ключи и имена групп
        keys = await conn.fetch("""
            SELECT gk.id, gk.key_code, gk.duration_days, gk.is_used, gk.created_at, g.name as group_name
            FROM group_keys gk
            LEFT JOIN groups g ON gk.group_id = g.id
            ORDER BY gk.created_at DESC
            LIMIT 100
        """)
        # Получаем список групп для формы создания
        groups = await conn.fetch("SELECT id, name, slug FROM groups ORDER BY access_level ASC")
        
    # Вам понадобится шаблон keys.html (можно скопировать users.html и упростить)
    # Если шаблона нет, покажем простой текст или JSON для теста
    try:
        return templates.TemplateResponse("keys.html", {"request": request, "keys": keys, "groups": groups})
    except:
        return JSONResponse({"detail": "Template keys.html not found, but data works", "keys": [dict(k) for k in keys]})

@app.post("/admin/keys/create")
async def admin_create_keys(
    request: Request, 
    group_id: int = Form(...), 
    days: int = Form(...), 
    count: int = Form(1), 
    _=Depends(ui_guard)
):
    import secrets
    if count > 50: count = 50
    
    async with app.state.pool.acquire() as conn:
        for _ in range(count):
            # Генерируем ключ формата XXXX-YYYY-ZZZZ
            code = f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}".upper()
            await conn.execute("""
                INSERT INTO group_keys (key_code, group_id, duration_days) 
                VALUES ($1, $2, $3)
            """, code, group_id, days)
            
    return RedirectResponse(url="/admin/keys", status_code=302)

@app.get("/admin/keys/delete/{id}")
async def admin_delete_key(request: Request, id: int, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM group_keys WHERE id = $1", id)
    return RedirectResponse(url="/admin/keys", status_code=302)

# ==========================================================
#                УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ И ГРУППАМИ
# ==========================================================

# Настройка цветов для групп (slug -> css class suffix)
GROUP_COLORS = {
    # === ВЫСШАЯ АДМИНИСТРАЦИЯ ===
    "tech-admin": "purple",     
    "admin": "indigo",          
    
    # === ПЕРСОНАЛ ===
    "senior-staff": "pink",     
    "staff": "danger",          
    "moderator": "orange",      
    "media": "cyan",            

    # === ПРЕМИУМ ===
    "plus": "primary",          
    "alpha": "azure",           
    "premium": "primary",       

    # === БАЗОВЫЕ ===
    "basic": "success",         
    
    # === ОБЫЧНЫЕ ===
    "user": "secondary"         
}

@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, q: Optional[str] = None, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        # Получаем пользователей и их АКТИВНУЮ группу (если есть)
        query_base = """
             SELECT u.id, u.email, u.username, u.uid, u.created_at, u.last_login, u.email_confirmed,
                    COALESCE((SELECT SUM(amount) FROM purchases p WHERE p.user_uid = u.uid), 0) as total_spent,
                    g.name as group_name, g.slug as group_slug
             FROM users u
             LEFT JOIN user_groups ug ON u.uid = ug.user_uid AND ug.is_active = TRUE AND ug.expires_at > NOW()
             LEFT JOIN groups g ON ug.group_id = g.id
        """
        
        # Сортировка: сначала новые регистрации
        if q:
            rows = await conn.fetch(
                f"{query_base} WHERE u.email ILIKE $1 OR u.username ILIKE $1 OR CAST(u.uid AS TEXT) ILIKE $1 ORDER BY u.created_at DESC", 
                f"%{q}%"
            )
        else:
            rows = await conn.fetch(f"{query_base} ORDER BY u.created_at DESC")

    return templates.TemplateResponse("users.html", {
        "request": request, 
        "rows": rows, 
        "q": q or "",
        "group_colors": GROUP_COLORS
    })

@app.get("/admin/users/edit/{uid}", response_class=HTMLResponse)
async def edit_user_form(request: Request, uid: str, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE uid=$1", uid)
        if not row:
             return Response("User not found", status_code=404)
        
        purchases = await conn.fetch("SELECT * FROM purchases WHERE user_uid=$1 ORDER BY created_at DESC", uid)
        
        # 1. Получаем активную группу пользователя
        user_groups_list = await conn.fetch("""
            SELECT ug.id, ug.expires_at, ug.granted_at, g.name, g.slug, ug.is_active
            FROM user_groups ug
            JOIN groups g ON ug.group_id = g.id
            WHERE ug.user_uid = $1
            ORDER BY ug.is_active DESC, ug.expires_at DESC
        """, uid)

        # 2. Получаем список всех доступных групп для выпадающего списка
        all_groups = await conn.fetch("SELECT id, name, slug FROM groups ORDER BY access_level ASC")

    return templates.TemplateResponse("user_form.html", {
        "request": request, 
        "row": row, 
        "purchases": purchases, 
        "now": datetime.now(),
        "user_groups": user_groups_list, 
        "all_groups": all_groups,
        "group_colors": GROUP_COLORS,
        "error": None
    })

@app.post("/admin/users/edit/{uid}")
async def edit_user(
    uid: str, 
    new_password: str = Form(None),
    email_confirmed: bool = Form(False),
    _ = Depends(ui_guard)
):
    async with app.state.pool.acquire() as conn:
        await conn.execute("UPDATE users SET email_confirmed=$1 WHERE uid=$2", email_confirmed, uid)
        
        if new_password and len(new_password.strip()) >= 6:
            from auth.jwt_utils import hash_password
            new_hash = hash_password(new_password.strip())
            await conn.execute("UPDATE users SET password_hash=$1 WHERE uid=$2", new_hash, uid)

    return RedirectResponse(url=f"/admin/users/edit/{uid}", status_code=302)


# --- НОВЫЕ ОБРАБОТЧИКИ ДЛЯ ГРУПП ---

@app.post("/admin/users/assign_group")
async def admin_assign_group_post(
    request: Request,
    user_uid: str = Form(...),
    group_id: int = Form(...),
    duration_days: Optional[str] = Form(None),
    is_forever: bool = Form(False),
    _=Depends(ui_guard)
):
    from datetime import datetime, timedelta
    
    # Обработка пустой строки дней
    days_val = 30 # Дефолт
    if duration_days and duration_days.strip():
        try:
            days_val = int(duration_days)
        except ValueError:
            pass 

    async with app.state.pool.acquire() as conn:
        async with conn.transaction(): 
            # 1. СБРОС ВСЕХ ТЕКУЩИХ АКТИВНЫХ ГРУПП (Правило: 1 пользователь = 1 группа)
            await conn.execute("UPDATE user_groups SET is_active=FALSE WHERE user_uid=$1", user_uid)

            # 2. Считаем дату окончания
            if is_forever:
                # Ставим дату очень далеко (например, 100 лет)
                expires_at = datetime.now() + timedelta(days=36500)
            else:
                expires_at = datetime.now() + timedelta(days=days_val)

            # 3. Выдаем группу (Создаем новую запись или обновляем старую)
            # Мы не используем ON CONFLICT, так как история выдач может быть полезна (не удаляем старые записи)
            await conn.execute("""
                INSERT INTO user_groups (user_uid, group_id, expires_at, is_active)
                VALUES ($1, $2, $3, TRUE)
                ON CONFLICT (user_uid, group_id) 
                DO UPDATE SET 
                    expires_at = EXCLUDED.expires_at, 
                    is_active = TRUE,
                    granted_at = NOW()
            """, user_uid, group_id, expires_date)
            
            # ВАЖНО: Код синхронизации с licenses УДАЛЕН, так как таблицы licenses больше нет.

    return RedirectResponse(url=f"/admin/users/edit/{user_uid}", status_code=302)


@app.post("/admin/users/revoke_group")
async def admin_revoke_group_post(
    request: Request,
    user_uid: str = Form(...),
    group_id: int = Form(...),
    _=Depends(ui_guard)
):
    async with app.state.pool.acquire() as conn:
        # Просто деактивируем группу
        await conn.execute("""
            UPDATE user_groups SET is_active = FALSE 
            WHERE user_uid=$1 AND group_id=$2
        """, user_uid, group_id)
        
    return RedirectResponse(url=f"/admin/users/edit/{user_uid}", status_code=302)

# --- УПРАВЛЕНИЕ КЛЮЧАМИ (Вместо старых токенов) ---

@app.get("/admin/tokens", response_class=HTMLResponse)
async def admin_tokens_list(request: Request, _=Depends(ui_guard)):
    """Отображение ключей доступа"""
    async with app.state.pool.acquire() as conn:
        # Загружаем ключи и имена групп
        keys = await conn.fetch("""
            SELECT k.id, k.key_code, k.duration_days, k.is_used, k.created_at, g.name as group_name
            FROM group_keys k
            LEFT JOIN groups g ON k.group_id = g.id
            ORDER BY k.created_at DESC LIMIT 100
        """)
        groups = await conn.fetch("SELECT id, name FROM groups ORDER BY access_level")
        
    # Используем шаблон tokens.html (нужно будет его адаптировать под поля group_keys)
    return templates.TemplateResponse("tokens.html", {"request": request, "rows": keys, "groups": groups, "q": ""})

@app.post("/admin/tokens/create")
async def admin_create_keys(
    request: Request,
    group_id: int = Form(...),
    days: int = Form(...),
    count: int = Form(1),
    _=Depends(ui_guard)
):
    if count < 1 or count > 50:
        return Response("Количество от 1 до 50", status_code=400)
        
    import secrets
    async with app.state.pool.acquire() as conn:
        for _ in range(count):
            # Генерируем ключ формата XXXX-YYYY-ZZZZ
            code = f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}".upper()
            await conn.execute("""
                INSERT INTO group_keys (key_code, group_id, duration_days) 
                VALUES ($1, $2, $3)
            """, code, group_id, days)
            
    return RedirectResponse(url="/admin/tokens", status_code=302)

@app.get("/admin/tokens/delete/{id}")
async def admin_delete_key(request: Request, id: int, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM group_keys WHERE id=$1", id)
    return RedirectResponse(url="/admin/tokens", status_code=302)

@app.post("/admin/tokens/delete_used")
async def admin_delete_used_keys(request: Request, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM group_keys WHERE is_used=TRUE")
    return RedirectResponse(url="/admin/tokens", status_code=302)
























