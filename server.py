import os
from typing import Optional, Literal
from datetime import date, datetime

import asyncpg
from fastapi import FastAPI, HTTPException, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, validator

# ========= Конфигурация =========
DB_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

# Данные автообновлений (заполняются через Environment на Render)
UPDATE_VERSION = os.getenv("LATEST_VERSION", "1.8.1").strip()
UPDATE_URL = os.getenv("DOWNLOAD_URL", "").strip()  # сюда поставь ссылку Google Drive
UPDATE_SHA256 = os.getenv("UPDATE_SHA256", "").strip()
UPDATE_CHANGELOG = os.getenv("UPDATE_CHANGELOG", "Автообновления и улучшения стабильности").strip()

if not DB_URL:
    raise RuntimeError("DATABASE_URL is not set")

app = FastAPI(title="FPBooster License Server", version="1.1.0")
templates = Jinja2Templates(directory="templates")


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


# ========= БД =========
@app.on_event("startup")
async def startup():
    app.state.pool = await asyncpg.create_pool(
        dsn=DB_URL, min_size=1, max_size=5, command_timeout=10
    )

@app.on_event("shutdown")
async def shutdown():
    pool = app.state.pool
    if pool:
        await pool.close()


# ========= Защита =========
def admin_guard_api(request: Request):
    token = request.headers.get("x-admin-token")
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN is not configured")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden: invalid admin token")
    return True

def admin_guard_ui(request: Request):
    if not ADMIN_TOKEN:
        return False
    cookie = request.cookies.get("admin_auth")
    return cookie == ADMIN_TOKEN


# ========= Health =========
@app.get("/api/health")
async def health():
    try:
        async with app.state.pool.acquire() as conn:
            await conn.execute("SELECT 1;")
        return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ========= API для клиента =========
@app.get("/api/license")
async def check_license(license: str):
    if not license or not license.strip():
        return {"status": "invalid"}
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT license_key, status, expires, user_name, created_at, last_check
            FROM licenses
            WHERE license_key = $1
            """,
            license.strip(),
        )
        if not row:
            return {"status": "invalid"}

        await conn.execute(
            "UPDATE licenses SET last_check = NOW() WHERE license_key = $1",
            license.strip(),
        )

        return {
            "status": row["status"],
            "expires": row["expires"].isoformat() if row["expires"] else None,
            "user": row["user_name"],
            "created": row["created_at"].isoformat() if row["created_at"] else None,
            "last_check": row["last_check"].isoformat() if row["last_check"] else None,
        }


# ========= API для админки =========
@app.post("/api/admin/license/create")
async def create_or_update_license(data: LicenseAdmin, _guard: bool = Depends(admin_guard_api)):
    async with app.state.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO licenses (license_key, status, expires, user_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (license_key)
            DO UPDATE
            SET status=EXCLUDED.status,
                expires=EXCLUDED.expires,
                user_name=EXCLUDED.user_name
            """,
            data.license_key.strip(),
            data.status,
            data.expires,
            data.user.strip() if data.user else None,
        )
        return {"ok": True}

@app.post("/api/admin/license/delete")
async def delete_license(data: LicenseIn, _guard: bool = Depends(admin_guard_api)):
    async with app.state.pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM licenses WHERE license_key = $1",
            data.license.strip(),
        )
        return {"ok": True, "result": result}

@app.get("/api/admin/license/get")
async def get_license_api(license: str, _guard: bool = Depends(admin_guard_api)):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT license_key, status, expires, user_name, created_at, last_check
            FROM licenses
            WHERE license_key = $1
            """,
            license.strip(),
        )
        if not row:
            raise HTTPException(status_code=404, detail="License not found")
        return {
            "license_key": row["license_key"],
            "status": row["status"],
            "expires": row["expires"].isoformat() if row["expires"] else None,
            "user": row["user_name"],
            "created": row["created_at"].isoformat() if row["created_at"] else None,
            "last_check": row["last_check"].isoformat() if row["last_check"] else None,
        }


# ========= Автообновления =========
@app.get("/api/update")
async def update_meta():
    # Проверяем, что URL и SHA заданы — чтобы не отдавать пустые значения клиенту
    return {
        "version": UPDATE_VERSION,
        "url": UPDATE_URL,
        "sha256": UPDATE_SHA256,
        "changelog": UPDATE_CHANGELOG,
    }


# ========= Веб-админка =========
@app.get("/admin", response_class=HTMLResponse)
async def admin_root(request: Request):
    if not admin_guard_ui(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    return RedirectResponse(url="/admin/licenses", status_code=302)

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if not ADMIN_TOKEN:
        return templates.TemplateResponse("login.html", {"request": request, "error": "ADMIN_TOKEN не настроен"}, status_code=500)
    if password != ADMIN_TOKEN:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный пароль"}, status_code=401)
    resp = RedirectResponse(url="/admin/licenses", status_code=302)
    resp.set_cookie("admin_auth", ADMIN_TOKEN, max_age=7*24*3600, httponly=True, samesite="lax")
    return resp

@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=302)
    resp.delete_cookie("admin_auth")
    return resp

@app.get("/admin/licenses", response_class=HTMLResponse)
async def admin_list(request: Request, q: Optional[str] = None):
    if not admin_guard_ui(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    async with app.state.pool.acquire() as conn:
        if q:
            rows = await conn.fetch(
                """
                SELECT license_key, status, expires, user_name, created_at, last_check
                FROM licenses
                WHERE license_key ILIKE $1 OR COALESCE(user_name,'') ILIKE $1
                ORDER BY created_at DESC
                """,
                f"%{q}%",
            )
        else:
            rows = await conn.fetch(
                """
                SELECT license_key, status, expires, user_name, created_at, last_check
                FROM licenses
                ORDER BY created_at DESC
                """
            )
    return templates.TemplateResponse("licenses.html", {"request": request, "rows": rows, "q": q or ""})

@app.get("/admin/licenses/new", response_class=HTMLResponse)
async def admin_new_form(request: Request):
    if not admin_guard_ui(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse("form.html", {"request": request, "mode": "create", "item": None, "error": None})

@app.post("/admin/licenses/new")
async def admin_create(
    request: Request,
    license_key: str = Form(...),
    status: str = Form(...),
    expires: str = Form(None),
    user: str = Form(None),
):
    if not admin_guard_ui(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    try:
        exp = date.fromisoformat(expires) if expires else None
    except Exception:
        return templates.TemplateResponse(
            "form.html",
            {"request": request, "mode": "create", "item": None, "error": "Неверный формат даты (YYYY-MM-DD)"},
            status_code=400,
        )

    async with app.state.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO licenses (license_key, status, expires, user_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (license_key)
            DO UPDATE SET status=EXCLUDED.status, expires=EXCLUDED.expires, user_name=EXCLUDED.user_name
            """,
            license_key.strip(),
            status,
            exp,
            (user or "").strip() or None,
        )
    return RedirectResponse(url="/admin/licenses", status_code=302)

@app.get("/admin/licenses/edit", response_class=HTMLResponse)
async def admin_edit_form(request: Request, license_key: str):
    if not admin_guard_ui(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT license_key, status, expires, user_name, created_at, last_check
            FROM licenses WHERE license_key=$1
            """,
            license_key,
        )
    if not row:
        return Response("License not found", status_code=404)
    return templates.TemplateResponse("form.html", {"request": request, "mode": "edit", "item": row, "error": None})

@app.post("/admin/licenses/edit")
async def admin_update(
    request: Request,
    original_key: str = Form(...),
    license_key: str = Form(...),
    status: str = Form(...),
    expires: str = Form(None),
    user: str = Form(None),
):
    if not admin_guard_ui(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    try:
        exp = date.fromisoformat(expires) if expires else None
    except Exception:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT license_key, status, expires, user_name, created_at, last_check FROM licenses WHERE license_key=$1",
                original_key,
            )
        return templates.TemplateResponse(
            "form.html",
            {"request": request, "mode": "edit", "item": row, "error": "Неверный формат даты (YYYY-MM-DD)"},
            status_code=400,
        )

    async with app.state.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE licenses SET status=$1, expires=$2, user_name=$3 WHERE license_key=$4",
                status,
                exp,
                (user or "").strip() or None,
                original_key,
            )
            if license_key.strip() != original_key:
                await conn.execute(
                    "UPDATE licenses SET license_key=$1 WHERE license_key=$2",
                    license_key.strip(),
                    original_key,
                )
    return RedirectResponse(url="/admin/licenses", status_code=302)

@app.post("/admin/licenses/delete")
async def admin_delete(request: Request, license_key: str = Form(...)):
    if not admin_guard_ui(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    async with app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM licenses WHERE license_key=$1", license_key)
    return RedirectResponse(url="/admin/licenses", status_code=302)
