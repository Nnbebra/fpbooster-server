import os
import pathlib
from typing import Optional, Literal
from datetime import date, datetime, timedelta

import asyncpg
from fastapi import FastAPI, HTTPException, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, validator

from guards import admin_guard_ui
from auth.guards import get_current_user as get_current_user_raw
# === Импорты для Лаунчера ===
from auth.jwt_utils import verify_password, make_jwt 

# Обёртка для Depends
async def current_user(request: Request):
    return await get_current_user_raw(request.app, request)

# ========= Создаём приложение =========
app = FastAPI(title="FPBooster License Server", version="1.4.0")
templates = Jinja2Templates(directory="templates")

# Заворачиваем UI-guard в Depends
def ui_guard(request: Request):
    return admin_guard_ui(request, app.state.ADMIN_TOKEN)

# Публичная главная страница
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = None
    try:
        user = await get_current_user_raw(request.app, request)
    except Exception:
        user = None
    return templates.TemplateResponse("index.html", {"request": request, "user": user})

# Подключение роутеров
from auth.users_router import router as users_router
from auth.email_confirm import router as email_confirm_router

app.include_router(users_router, tags=["auth"])
app.include_router(email_confirm_router, tags=["email"])

# ===== Ссылка на скачивание Лаунчера =====
# Берется из переменных окружения
DOWNLOAD_URL = os.getenv("DOWNLOAD_URL", "").strip()
app.state.DOWNLOAD_URL = DOWNLOAD_URL

# ===== Админ токен =====
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
if not ADMIN_TOKEN:
    raise RuntimeError("ADMIN_TOKEN is not set")
app.state.ADMIN_TOKEN = ADMIN_TOKEN

# ===== Подключение остальных роутеров =====
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

from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/templates_css", StaticFiles(directory="templates_css"), name="templates_css")
app.mount("/JavaScript", StaticFiles(directory="JavaScript"), name="javascript")

# ========= Конфигурация БД =========
DB_URL = os.getenv("DATABASE_URL", "").strip()
if not DB_URL:
    raise RuntimeError("DATABASE_URL is not set")

# ========= Модели =========
class LicenseIn(BaseModel):
    license: str

class LicenseAdmin(BaseModel):
    license_key: str
    status: Literal["active", "expired", "banned"]
    expires: Optional[date] = None
    user: Optional[str] = None

    @validator("expires", pre=True)
    def parse_expires(cls, v):
        if v in (None, "", "null"):
            return None
        if isinstance(v, date):
            return v
        try:
            return date.fromisoformat(str(v))
        except Exception as e:
            raise ValueError(f"expires must be ISO date (YYYY-MM-DD), got {v!r}: {e}")

# === Модель для входа через Лаунчер ===
class LauncherLogin(BaseModel):
    email: str
    password: str
    hwid: str

# ========= База данных =========
@app.on_event("startup")
async def startup():
    app.state.pool = await asyncpg.create_pool(
        dsn=DB_URL,
        min_size=1,
        max_size=5,
        command_timeout=10
    )

@app.on_event("shutdown")
async def shutdown():
    pool = app.state.pool
    if pool:
        await pool.close()

# ========= Защита =========
def admin_guard_api(request: Request):
    token = request.headers.get("x-admin-token")
    if not app.state.ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN is not configured")
    if token != app.state.ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden: invalid admin token")
    return True

# ========= Health =========
@app.get("/api/health")
async def health():
    try:
        async with app.state.pool.acquire() as conn:
            await conn.execute("SELECT 1;")
        return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

# ========= Публичный API (Legacy check) =========
@app.get("/api/license")
async def check_license(license: str):
    if not license or not license.strip():
        return {"status": "invalid"}
    key = license.strip()

    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT license_key, status, expires, user_name, created_at, last_check, user_uid FROM licenses WHERE license_key = $1",
            key,
        )
        if not row:
            return {"status": "invalid"}

        await conn.execute("UPDATE licenses SET last_check = NOW() WHERE license_key = $1", key)

        return {
            "status": row["status"],
            "expires": row["expires"].isoformat() if row["expires"] else None,
            "user": row["user_name"],
            "user_uid": str(row["user_uid"]) if row["user_uid"] else None,
            "created": row["created_at"].isoformat() if row["created_at"] else None,
            "last_check": row["last_check"].isoformat() if row["last_check"] else None,
        }

# ==========================================================
#             ЭНДПОИНТЫ ДЛЯ C# ЛАУНЧЕРА
# ==========================================================

@app.post("/api/launcher/login")
async def launcher_login(data: LauncherLogin, request: Request):
    # 1. Ищем пользователя
    email = data.email.strip().lower()
    async with request.app.state.pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id, uid, password_hash, username FROM users WHERE email=$1", email)
        
        # 2. Проверяем пароль
        if not user or not verify_password(data.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Неверный логин или пароль")

        # 3. Ищем лицензию
        license_row = await conn.fetchrow("SELECT license_key, status, expires, hwid FROM licenses WHERE user_uid = $1", user["uid"])

        if not license_row:
             raise HTTPException(status_code=403, detail="Лицензия не найдена")

        # 4. Статус подписки
        if license_row['status'] != 'active':
             raise HTTPException(status_code=402, detail="Подписка не активна")
             
        if license_row['expires'] and license_row['expires'] < date.today():
             raise HTTPException(status_code=402, detail="Срок подписки истек")

        # 5. HWID
        db_hwid = license_row['hwid']
        if db_hwid is None:
            await conn.execute("UPDATE licenses SET hwid=$1 WHERE license_key=$2", data.hwid, license_row['license_key'])
        elif db_hwid != data.hwid:
            raise HTTPException(status_code=403, detail="Ошибка HWID: Заход с другого ПК запрещен")

        # 6. Токен
        token = make_jwt(user["id"], email)
        
        return {
            "status": "success",
            "username": user["username"],
            "token": token,
            "expires": str(license_row["expires"])
        }

@app.get("/api/client/get-core")
async def get_client_core(request: Request, user_data = Depends(current_user)):
    # 1. Проверка лицензии
    async with request.app.state.pool.acquire() as conn:
        license_row = await conn.fetchrow("SELECT status, expires FROM licenses WHERE user_uid=$1", user_data["uid"])
        
        if not license_row or license_row['status'] != 'active':
            raise HTTPException(status_code=403, detail="No active license")
            
        if license_row['expires'] and license_row['expires'] < date.today():
            raise HTTPException(status_code=403, detail="License expired")

    # 2. Отдача файла (Protected Build)
    file_path = "protected_builds/FPBooster.dll" 
    if not os.path.exists(file_path):
        raise HTTPException(status_code=500, detail="Server Error: Build file not found")

    with open(file_path, "rb") as f:
        file_bytes = f.read()
    return Response(content=file_bytes, media_type="application/octet-stream")

# ==========================================================

# ========= Активация лицензии =========
@app.post("/api/license/activate")
async def activate_license(request: Request, token: Optional[str] = Form(None), key: Optional[str] = Form(None), user=Depends(current_user)):
    token_value = (token or key or "").strip()
    if not token_value:
        raise HTTPException(status_code=400, detail="Token is required")

    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            activation = await conn.fetchrow("SELECT * FROM activation_tokens WHERE token=$1", token_value)
            if not activation:
                raise HTTPException(404, "Токен не найден")
            if activation["status"] != "unused":
                raise HTTPException(400, "Токен уже использован")

            license_row = await conn.fetchrow("SELECT * FROM licenses WHERE user_uid=$1", user["uid"])
            if not license_row:
                raise HTTPException(404, "Лицензия пользователя не найдена")

            today = datetime.utcnow().date()
            base_date = (license_row["expires"] if license_row["expires"] and license_row["expires"] >= today else today)
            new_expires = base_date + timedelta(days=activation["duration_days"])

            await conn.execute("UPDATE licenses SET status='active', expires=$1, activated_at=NOW() WHERE user_uid=$2", new_expires, user["uid"])
            await conn.execute("UPDATE activation_tokens SET status='used', used_at=NOW(), used_by_uid=$1 WHERE id=$2", user["uid"], activation["id"])
            await conn.execute("INSERT INTO purchases (user_uid, plan, amount, currency, source, token_code) VALUES ($1, $2, $3, $4, 'token', $5)",
                user["uid"], str(activation["duration_days"]), 0, 'TOKEN', token_value)

    return RedirectResponse(url="/cabinet", status_code=302)

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

@app.get("/admin/licenses", response_class=HTMLResponse)
async def admin_list(request: Request, q: Optional[str] = None, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        # JOIN с users чтобы видеть email, и выборка hwid
        query_base = """
            SELECT l.license_key, l.status, l.expires, l.user_name, l.user_uid, l.hwid, l.created_at, l.last_check, u.email
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

# === НОВЫЙ МЕТОД: СБРОС HWID ===
@app.get("/admin/licenses/reset_hwid/{license_key}")
async def reset_hwid(request: Request, license_key: str, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        await conn.execute("UPDATE licenses SET hwid = NULL WHERE license_key=$1", license_key)
    # Возвращаемся обратно на список
    return RedirectResponse(url="/admin/licenses", status_code=302)

@app.post("/admin/licenses/delete")
async def admin_delete_post(request: Request, license_key: str = Form(...), _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM licenses WHERE license_key=$1", license_key)
    return RedirectResponse(url="/admin/licenses", status_code=302)

@app.get("/admin/licenses/delete/{license_key}")
async def delete_license_get(request: Request, license_key: str, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM licenses WHERE license_key=$1", license_key)
    return RedirectResponse(url="/admin/licenses", status_code=302)

@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, q: Optional[str] = None, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        if q:
            rows = await conn.fetch("SELECT id, email, username, uid, user_group, created_at, last_login FROM users WHERE email ILIKE $1 OR username ILIKE $1 OR CAST(uid AS TEXT) ILIKE $1 ORDER BY created_at DESC", f"%{q}%")
        else:
            rows = await conn.fetch("SELECT id, email, username, uid, user_group, created_at, last_login FROM users ORDER BY created_at DESC")
    return templates.TemplateResponse("users.html", {"request": request, "rows": rows, "q": q or ""})

@app.get("/admin/users/edit/{uid}", response_class=HTMLResponse)
async def edit_user_form(request: Request, uid: str, _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, email, username, uid, user_group FROM users WHERE uid=$1", uid)
    if not row:
        return Response("User not found", status_code=404)
    return templates.TemplateResponse("user_form.html", {"request": request, "row": row, "error": None})

@app.post("/admin/users/edit/{uid}")
async def edit_user(uid: str, user_group: str = Form(...), _=Depends(ui_guard)):
    async with app.state.pool.acquire() as conn:
        await conn.execute("UPDATE users SET user_group=$1 WHERE uid=$2", user_group, uid)
    return RedirectResponse(url="/admin/users", status_code=302)
