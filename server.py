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
from auth.guards import get_current_user    # Наша новая универсальная защита

# --- ИМПОРТ ПЛАГИНОВ ---
from Plugins import AutoBump, AutoRestock


async def current_user(request: Request):
    return await get_current_user_raw(request.app, request)

app = FastAPI(title="FPBooster License Server", version="1.6.0")
templates = Jinja2Templates(directory="templates")

def ui_guard(request: Request):
    return admin_guard_ui(request, app.state.ADMIN_TOKEN)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = None
    try:
        user = await get_current_user_raw(request.app, request)
    except Exception:
        user = None
    return templates.TemplateResponse("index.html", {"request": request, "user": user})

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
class LicenseIn(BaseModel):
    license: str

class LicenseAdmin(BaseModel):
    license_key: str
    status: Literal["active", "expired", "banned"]
    expires: Optional[date] = None
    user: Optional[str] = None

    @validator("expires", pre=True)
    def parse_expires(cls, v):
        if v in (None, "", "null"): return None
        if isinstance(v, date): return v
        try: return date.fromisoformat(str(v))
        except Exception as e: raise ValueError(f"expires error: {e}")

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
        user = await conn.fetchrow("SELECT id, uid, password_hash, username FROM users WHERE email=$1", email)
        
        if not user or not verify_password(data.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Неверный логин или пароль")

        license_row = await conn.fetchrow("SELECT license_key, status, expires, hwid FROM licenses WHERE user_uid = $1", user["uid"])

        if not license_row:
             raise HTTPException(status_code=403, detail="Лицензия не найдена. Купите подписку на сайте.")

        # Базовая проверка активности аккаунта
        if license_row['status'] != 'active':
             raise HTTPException(status_code=402, detail="Подписка не активна")
             
        # Проверяем срок действия (если истек - 402)
        if license_row['expires'] and license_row['expires'] < date.today():
             raise HTTPException(status_code=402, detail="Срок подписки истек")

        # HWID
        db_hwid = license_row['hwid']
        if db_hwid is None:
            await conn.execute("UPDATE licenses SET hwid=$1 WHERE license_key=$2", data.hwid, license_row['license_key'])
        elif db_hwid != data.hwid:
            raise HTTPException(status_code=403, detail="Ошибка HWID: Заход с другого ПК запрещен.")

        token = make_jwt(user["id"], email)
        
        return {
            "status": "success",
            "username": user["username"],
            "token": token,
            "expires": str(license_row["expires"])
        }

# --- API СПИСОК ПРОДУКТОВ ---
@app.get("/api/client/products")
async def get_client_products(request: Request, user_data=Depends(current_user)):
    uid = user_data["uid"]
    
    # Флаги доступа
    has_standard = False
    has_alpha = False
    has_plus = False
    
    async with request.app.state.pool.acquire() as conn:
        # 1. Проверяем текущую активную лицензию (это дает доступ к Standard)
        license_row = await conn.fetchrow("SELECT status, expires FROM licenses WHERE user_uid=$1", uid)
        
        if license_row and license_row['status'] == 'active':
             if not license_row['expires'] or license_row['expires'] >= date.today():
                 has_standard = True

        # 2. Проверяем историю покупок на наличие Alpha или Plus
        rows = await conn.fetch("SELECT plan FROM purchases WHERE user_uid=$1", uid)
        for r in rows:
            plan_name = (r['plan'] or "").lower()
            if "alpha" in plan_name:
                has_alpha = True
            if "plus" in plan_name:
                has_plus = True

    products = []

    # Standard
    products.append({
        "id": "standard",
        "name": "FPBooster Standard",
        "description": "Стабильная версия 1.16.5",
        "image_url": "pack://application:,,,/Assets/FPBoosterDef.png",
        "is_available": has_standard,
        "download_url": "/api/client/get-core?ver=standard"
    })

    # Alpha
    products.append({
        "id": "alpha",
        "name": "FPBooster Alpha",
        "description": "Бета-версия (Early Access)",
        "image_url": "pack://application:,,,/Assets/FPBoosterAlpha.png",
        "is_available": has_alpha and has_standard, 
        "download_url": "/api/client/get-core?ver=alpha"
    })

    # Plus
    products.append({
        "id": "plus",
        "name": "FPBooster Plus",
        "description": "Расширенная версия",
        "image_url": "pack://application:,,,/Assets/FPBooster+Def.png",
        "is_available": has_plus and has_standard,
        "download_url": "/api/client/get-core?ver=plus"
    })

    return products

# Твой секретный ключ
SERVER_SIDE_AES_KEY = "15345172281214561882123456789999"

@app.get("/api/client/get-core")
async def get_client_core(request: Request, ver: str = "standard", user_data = Depends(current_user)):
    # 1. Проверяем лицензию
    async with request.app.state.pool.acquire() as conn:
        license_row = await conn.fetchrow("SELECT status, expires FROM licenses WHERE user_uid=$1", user_data["uid"])
        if not license_row or license_row['status'] != 'active':
             raise HTTPException(403, "No active license")
        if license_row['expires'] and license_row['expires'] < date.today():
            raise HTTPException(403, "License expired")

    # 2. Выбираем ЗАШИФРОВАННЫЙ файл
    filename = "FPBooster.dll.enc"
    if ver == "alpha": filename = "FPBooster_Alpha.dll.enc"
    elif ver == "plus": filename = "FPBooster_Plus.dll.enc"

    file_path = f"protected_builds/{filename}"
    
    if not os.path.exists(file_path):
        file_path = "protected_builds/FPBooster.dll.enc"

    if not os.path.exists(file_path):
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
#             АДМИНКА И ЛИЦЕНЗИИ
# ==========================================================

@app.post("/api/license/activate")
async def activate_license(request: Request, token: Optional[str] = Form(None), key: Optional[str] = Form(None), user=Depends(current_user)):
    token_value = (token or key or "").strip()
    if not token_value: raise HTTPException(400, "Token is required")
    try:
        async with request.app.state.pool.acquire() as conn:
            async with conn.transaction():
                activation = await conn.fetchrow("SELECT * FROM activation_tokens WHERE token=$1", token_value)
                if not activation: raise HTTPException(404, "Токен не найден")
                if activation["status"] != "unused": raise HTTPException(400, "Токен уже использован")
                
                license_row = await conn.fetchrow("SELECT * FROM licenses WHERE user_uid=$1", user["uid"])
                if not license_row:
                     import secrets
                     new_key = "key_" + secrets.token_hex(8)
                     await conn.execute("INSERT INTO licenses (license_key, status, user_uid, created_at) VALUES ($1, 'expired', $2, NOW())", new_key, user["uid"])
                     license_row = await conn.fetchrow("SELECT * FROM licenses WHERE user_uid=$1", user["uid"])
                
                today = datetime.utcnow().date()
                current_expires = license_row["expires"]
                if current_expires and current_expires.year > 2090: base_date = today 
                else: base_date = (current_expires if current_expires and current_expires >= today else today)
                
                days_to_add = activation["duration_days"]
                try:
                    new_expires = base_date + timedelta(days=days_to_add)
                    if new_expires.year > 2100: new_expires = date(2100, 1, 1)
                except OverflowError: new_expires = date(2100, 1, 1)
                
                await conn.execute("UPDATE licenses SET status='active', expires=$1, activated_at=NOW() WHERE user_uid=$2", new_expires, user["uid"])
                await conn.execute("UPDATE activation_tokens SET status='used', used_at=NOW(), used_by_uid=$1 WHERE id=$2", user["uid"], activation["id"])
                await conn.execute("INSERT INTO purchases (user_uid, plan, amount, currency, source, token_code, created_at) VALUES ($1, $2, 0, 'TOKEN', 'token', $3, NOW())", user["uid"], f"activation_{days_to_add}_days", token_value)
    except HTTPException: raise 
    except Exception as e:
        print(f"CRITICAL ERROR in activate_license: {e}")
        raise HTTPException(500, f"SQL Error: {str(e)}")
    return RedirectResponse(url="/cabinet", status_code=302)


# ==========================================
#      FPBOOSTER PROTECTED API (FINAL)
# ==========================================

# 1. СПИСОК ТОВАРОВ (Лаунчер запрашивает это)
@app.get("/api/products")
async def get_api_products():
    pool = app.state.pool
    try:
        async with pool.acquire() as conn:
            # Берем реальные пути и данные из базы
            rows = await conn.fetch("SELECT id, name, description, image_url, is_available FROM products")
            
            products = []
            for row in rows:
                # ПОДМЕНА ССЫЛКИ: Лаунчер получает ссылку на API
                secure_url = f"/api/download/{row['id']}"
                
                products.append({
                    "id": str(row['id']),
                    "name": row['name'],
                    "description": row['description'],
                    "image_url": row['image_url'],
                    "is_available": row['is_available'],
                    "download_url": secure_url 
                })
            return products
    except Exception as e:
        print(f"API Error: {e}")
        return JSONResponse({"error": "Internal Server Error"}, status_code=500)


# 2. ЗАЩИЩЕННОЕ СКАЧИВАНИЕ (Только для Лаунчера)
@app.get("/api/download/{product_id}")
async def download_product(
    product_id: int, 
    x_hwid: Optional[str] = Header(None, alias="X-HWID"), # Читаем заголовок X-HWID
    user_row = Depends(get_current_user) # <-- ТУТ РАБОТАЕТ НАШ НОВЫЙ GUARDS.PY
):
    pool = app.state.pool
    
    try:
        user_uid = user_row['uid'] # Достаем UID из пользователя (auth.guards уже нашел его в базе)
        user_id = user_row['id']

        async with pool.acquire() as conn:
            # 1. Ищем информацию о файле
            prod_row = await conn.fetchrow(
                "SELECT exe_name, download_url, secret_key, name FROM products WHERE id = $1", 
                product_id
            )
            
            if not prod_row:
                return JSONResponse({"error": "Product not found"}, status_code=404)

            # === ANTI-CRACK & LICENSE CHECK ===
            # Проверяем лицензию, только если это платная версия (ID=1 или "Plus" в названии)
            if product_id == 1 or "Plus" in prod_row['name']:
                
                # Ищем АКТИВНУЮ лицензию пользователя
                license_row = await conn.fetchrow("""
                    SELECT * FROM licenses 
                    WHERE user_uid = $1 AND status = 'active' AND expires >= CURRENT_DATE
                """, user_uid)

                if not license_row:
                     return JSONResponse({"error": "NO_LICENSE: Active license required."}, status_code=403)

                # Проверка HWID
                db_hwid = license_row['hwid']
                
                if not db_hwid:
                    # Если HWID пустой (первый запуск) -> Привязываем текущий
                    if x_hwid:
                        await conn.execute(
                            "UPDATE licenses SET hwid = $1 WHERE license_key = $2", 
                            x_hwid, license_row['license_key']
                        )
                        print(f"HWID Linked for User {user_id}: {x_hwid}")
                
                elif x_hwid and db_hwid != x_hwid:
                    # HWID не совпал -> БАН
                    print(f"HWID Mismatch! User: {user_id}, Stored: {db_hwid}, Got: {x_hwid}")
                    return JSONResponse({"error": "HWID_MISMATCH: License bound to another PC."}, status_code=403)
            # ==================================

            # 2. Формируем путь к файлу
            db_path = prod_row['download_url'] # Например: /static/FPBoosterPlus.dll
            filename = prod_row['exe_name'] or "Program.exe"
            secret_key = prod_row['secret_key']

            # Очищаем путь от /static/ и лишних слешей
            real_filename = db_path.replace("/static/", "").replace("/", "")
            
            # Полный путь на диске сервера
            file_disk_path = f"/opt/fpbooster/static/{real_filename}"

            if not os.path.exists(file_disk_path):
                 return JSONResponse({"error": f"File missing on server: {real_filename}"}, status_code=404)

            # 3. Отдаем файл + Ключ
            headers = {
                "X-Encryption-Key": secret_key if secret_key else "",
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
            
            return FileResponse(path=file_disk_path, headers=headers, media_type='application/octet-stream')

    except Exception as e:
        print(f"Download Error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
# ========= Админ API =========
@app.post("/api/admin/license/create")
async def create_or_update_license(data: LicenseAdmin, _guard: bool = Depends(admin_guard_api)):
    async with app.state.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO licenses (license_key, status, expires, user_name) VALUES ($1, $2, $3, $4)
            ON CONFLICT (license_key) DO UPDATE SET status=EXCLUDED.status, expires=EXCLUDED.expires, user_name=EXCLUDED.user_name
            """,
            data.license_key.strip(), data.status, data.expires, data.user.strip() if data.user else None
        )
        return {"ok": True}

@app.post("/api/admin/license/delete")
async def delete_license(data: LicenseIn, _guard: bool = Depends(admin_guard_api)):
    async with app.state.pool.acquire() as conn:
        result = await conn.execute("DELETE FROM licenses WHERE license_key = $1", data.license.strip())
        return {"ok": True, "result": result}

@app.get("/api/admin/license/get")
async def get_license_api(license: str, _guard: bool = Depends(admin_guard_api)):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT license_key, status, expires, user_name, created_at, last_check FROM licenses WHERE license_key = $1", license.strip())
        if not row:
            raise HTTPException(status_code=404, detail="License not found")
        return dict(row)

# ========= Веб-админка =========
@app.get("/admin", response_class=HTMLResponse)
async def admin_root(request: Request, _=Depends(ui_guard)):
    return RedirectResponse(url="/admin/licenses", status_code=302)

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if not app.state.ADMIN_TOKEN:
        return templates.TemplateResponse("login.html", {"request": request, "error": "ADMIN_TOKEN не настроен"}, status_code=500)
    if password != app.state.ADMIN_TOKEN:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный токен"}, status_code=401)
    resp = RedirectResponse(url="/admin/licenses", status_code=302)
    resp.set_cookie("admin_auth", app.state.ADMIN_TOKEN, httponly=True, samesite="lax", secure=True, max_age=7*24*3600)
    return resp

@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=302)
    resp.delete_cookie("admin_auth")
    return resp

# --- ЛИЦЕНЗИИ ---
@app.get("/admin/licenses", response_class=HTMLResponse)
async def admin_list(request: Request, q: Optional[str] = None, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        query_base = """
            SELECT l.license_key, l.status, l.expires, l.user_name, l.user_uid, l.hwid, 
                   l.created_at, l.last_check, l.promocode_used, u.email
            FROM licenses l
            LEFT JOIN users u ON l.user_uid = u.uid
        """
        if q:
            rows = await conn.fetch(
                f"{query_base} WHERE l.license_key ILIKE $1 OR COALESCE(l.user_name,'') ILIKE $1 OR u.email ILIKE $1 ORDER BY l.created_at DESC", 
                f"%{q}%"
            )
        else:
            rows = await conn.fetch(f"{query_base} ORDER BY l.created_at DESC")
            
    return templates.TemplateResponse("licenses.html", {"request": request, "rows": rows, "q": q or ""})

@app.get("/admin/licenses/new", response_class=HTMLResponse)
async def admin_new_form(request: Request, _=Depends(ui_guard)):
    return templates.TemplateResponse("license_form.html", {"request": request, "row": None, "error": None})

@app.post("/admin/licenses/new")
async def admin_create_form(request: Request, license_key: str = Form(...), status: str = Form(...), expires: str = Form(None), user: str = Form(None), _=Depends(ui_guard)):
    try:
        exp = date.fromisoformat(expires) if expires else None
    except Exception:
        return templates.TemplateResponse("license_form.html", {"request": request, "row": None, "error": "Неверный формат даты"}, status_code=400)
    async with app.state.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO licenses (license_key, status, expires, user_name) VALUES ($1, $2, $3, $4) ON CONFLICT (license_key) DO UPDATE SET status=EXCLUDED.status, expires=EXCLUDED.expires, user_name=EXCLUDED.user_name",
            license_key.strip(), status, exp, (user or "").strip() or None
        )
    return RedirectResponse(url="/admin/licenses", status_code=302)

@app.get("/admin/licenses/edit/{license_key}", response_class=HTMLResponse)
async def edit_license_form(request: Request, license_key: str, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM licenses WHERE license_key=$1", license_key)
    if not row:
        return Response("License not found", status_code=404)
    return templates.TemplateResponse("license_form.html", {"request": request, "row": row, "error": None})

@app.post("/admin/licenses/edit/{license_key}")
async def edit_license(request: Request, license_key: str, status: str = Form(...), expires: str = Form(None), user: str = Form(None), _=Depends(ui_guard)):
    try:
        exp = date.fromisoformat(expires) if expires else None
    except Exception:
        return RedirectResponse(url=f"/admin/licenses/edit/{license_key}", status_code=303)
    async with app.state.pool.acquire() as conn:
        await conn.execute("UPDATE licenses SET status=$1, expires=$2, user_name=$3 WHERE license_key=$4", status, exp, (user or "").strip() or None, license_key)
    return RedirectResponse(url="/admin/licenses", status_code=302)

@app.get("/admin/licenses/reset_hwid/{license_key}")
async def reset_hwid(request: Request, license_key: str, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        await conn.execute("UPDATE licenses SET hwid = NULL WHERE license_key=$1", license_key)
    return RedirectResponse(url="/admin/licenses", status_code=302)

@app.get("/admin/licenses/delete/{license_key}")
async def delete_license_get(request: Request, license_key: str, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM licenses WHERE license_key=$1", license_key)
    return RedirectResponse(url="/admin/licenses", status_code=302)

# --- ПОЛЬЗОВАТЕЛИ ---
@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, q: Optional[str] = None, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        query = """
             SELECT u.id, u.email, u.username, u.uid, u.user_group, u.created_at, u.last_login, u.email_confirmed,
                   COALESCE((SELECT SUM(amount) FROM purchases p WHERE p.user_uid = u.uid), 0) as total_spent
            FROM users u
        """
        if q:
            rows = await conn.fetch(f"{query} WHERE u.email ILIKE $1 OR u.username ILIKE $1 OR CAST(u.uid AS TEXT) ILIKE $1 ORDER BY u.created_at DESC", f"%{q}%")
        else:
            rows = await conn.fetch(f"{query} ORDER BY u.created_at DESC")
    return templates.TemplateResponse("users.html", {"request": request, "rows": rows, "q": q or ""})

@app.get("/admin/users/edit/{uid}", response_class=HTMLResponse)
async def edit_user_form(request: Request, uid: str, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE uid=$1", uid)
        if not row:
             return Response("User not found", status_code=404)
        purchases = await conn.fetch("SELECT * FROM purchases WHERE user_uid=$1 ORDER BY created_at DESC", uid)
        license_info = await conn.fetchrow("SELECT license_key, status, expires, hwid FROM licenses WHERE user_uid=$1", uid)

    return templates.TemplateResponse("user_form.html", {
        "request": request, 
        "row": row, 
        "purchases": purchases, 
        "lic": license_info,
        "error": None
    })

@app.post("/admin/users/edit/{uid}")
async def edit_user(
    uid: str, 
    user_group: str = Form(...), 
    new_password: str = Form(None),
    email_confirmed: bool = Form(False),
    _ = Depends(ui_guard)
):
    async with app.state.pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET user_group=$1, email_confirmed=$2 WHERE uid=$3", 
            user_group, email_confirmed, uid
        )
        if new_password and len(new_password.strip()) >= 6:
            from auth.jwt_utils import hash_password
            new_hash = hash_password(new_password.strip())
            await conn.execute("UPDATE users SET password_hash=$1 WHERE uid=$2", new_hash, uid)

    return RedirectResponse(url="/admin/users", status_code=302)

@app.get("/admin/users/delete/{uid}")
async def delete_user_get(request: Request, uid: str, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM licenses WHERE user_uid=$1", uid)
        await conn.execute("DELETE FROM activation_tokens WHERE used_by_uid=$1", uid)
        await conn.execute("DELETE FROM purchases WHERE user_uid=$1", uid)
        await conn.execute("DELETE FROM users WHERE uid=$1", uid)
    return RedirectResponse(url="/admin/users", status_code=302)

# --- УПРАВЛЕНИЕ ТОКЕНАМИ АКТИВАЦИИ ---
@app.get("/admin/tokens", response_class=HTMLResponse)
async def admin_tokens_list(request: Request, q: Optional[str] = None, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        query = """
            SELECT t.id, t.token, t.duration_days, t.status, t.used_at, u.username as used_by
            FROM activation_tokens t
            LEFT JOIN users u ON t.used_by_uid = u.uid
        """
        if q:
            rows = await conn.fetch(
                f"{query} WHERE t.token ILIKE $1 OR u.username ILIKE $1 ORDER BY t.id DESC", 
                f"%{q}%"
            )
        else:
            rows = await conn.fetch(f"{query} ORDER BY t.id DESC")
            
    return templates.TemplateResponse("tokens.html", {"request": request, "rows": rows, "q": q or ""})

@app.post("/admin/tokens/create")
async def admin_create_tokens(
    request: Request,
    days: int = Form(...),
    count: int = Form(1),
    prefix: str = Form(""),
    _=Depends(ui_guard)
):
    if count < 1 or count > 100:
        return Response("Количество должно быть от 1 до 100", status_code=400)
    import secrets
    async with app.state.pool.acquire() as conn:
        async with conn.transaction():
            for _ in range(count):
                random_part = secrets.token_hex(8).upper()
                token = f"{prefix}{random_part}" if prefix else random_part
                await conn.execute(
                    "INSERT INTO activation_tokens (token, duration_days, status) VALUES ($1, $2, 'unused')",
                    token, days
                )
    return RedirectResponse(url="/admin/tokens", status_code=302)

@app.get("/admin/tokens/delete/{id}")
async def admin_delete_token(request: Request, id: int, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM activation_tokens WHERE id=$1", id)
    return RedirectResponse(url="/admin/tokens", status_code=302)

@app.post("/admin/tokens/delete_used")
async def admin_delete_used_tokens(request: Request, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM activation_tokens WHERE status='used'")
    return RedirectResponse(url="/admin/tokens", status_code=302)






