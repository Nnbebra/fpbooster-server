from fastapi import APIRouter, Request, Form, Body, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from .jwt_utils import hash_password, verify_password, make_jwt
from .guards import get_current_user
from .email_service import create_and_send_confirmation
from datetime import date

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# === РЕГИСТРАЦИЯ ===
@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})

@router.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    username: str = Form(None),
    accept_terms: str = Form(None),
):
    if not accept_terms:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Необходимо принять соглашение"}, status_code=400)

    email = email.strip().lower()
    if len(password) < 6:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Пароль должен быть ≥ 6 символов"}, status_code=400)

    async with request.app.state.pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM users WHERE email=$1", email)
        if exists:
            return templates.TemplateResponse("register.html", {"request": request, "error": "Такой email уже зарегистрирован"}, status_code=400)
        
        pw_hash = hash_password(password)
        # ВАЖНО: Возвращаем uid при создании
        row = await conn.fetchrow(
            "INSERT INTO users (email, password_hash, username) VALUES ($1, $2, $3) RETURNING id, email, uid",
            email, pw_hash, (username or "").strip() or None
        )

    try:
        await create_and_send_confirmation(request.app, row["id"], row["email"])
    except: pass

    token = make_jwt(row["id"], row["email"])
    resp = RedirectResponse(url="/cabinet", status_code=302)
    # ФИКС: secure=False (чтобы работало без HTTPS)
    resp.set_cookie("user_auth", token, httponly=True, samesite="lax", secure=False, max_age=7*24*3600)
    return resp

# === ВХОД ===
@router.get("/login", response_class=HTMLResponse)
async def user_login_page(request: Request):
    return templates.TemplateResponse("user_login.html", {"request": request, "error": None})

@router.post("/login")
async def user_login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    async with request.app.state.pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id, email, password_hash FROM users WHERE email=$1", email)
        if not user or not verify_password(password, user["password_hash"]):
            return templates.TemplateResponse("user_login.html", {"request": request, "error": "Неверный email или пароль"}, status_code=401)
        await conn.execute("UPDATE users SET last_login=NOW() WHERE id=$1", user["id"])

    token = make_jwt(user["id"], user["email"])
    resp = RedirectResponse(url="/cabinet", status_code=302)
    # ФИКС: secure=False (решает проблему "застревания" на входе)
    resp.set_cookie("user_auth", token, httponly=True, samesite="lax", secure=False, max_age=7*24*3600)
    return resp

# === ЛИЧНЫЙ КАБИНЕТ ===
@router.get("/cabinet", response_class=HTMLResponse)
async def account_page(request: Request):
    try:
        user = await get_current_user(request.app, request)
    except:
        return RedirectResponse("/login", status_code=302)

    async with request.app.state.pool.acquire() as conn:
        # ФИКС SQL: Запрос под твою структуру БД (таблица licenses, колонка user_uid)
        licenses = await conn.fetch(
            """
            SELECT license_key, status, expires, hwid 
            FROM licenses 
            WHERE user_uid = $1 
            ORDER BY created_at DESC
            """,
            user["uid"],
        )
        
        # Получаем общую сумму трат (если таблица purchases есть)
        total_spent = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM purchases WHERE user_uid=$1",
            user["uid"]
        )

    # Ищем активную лицензию для отображения
    found_active = None
    if licenses:
        for lic in licenses:
            if lic['status'] == 'active':
                if lic['expires'] and lic['expires'] >= date.today():
                    found_active = lic
                    break
    
    display_license = found_active if found_active else (licenses[0] if licenses else None)
    is_license_active = (found_active is not None)
    download_url = getattr(request.app.state, "DOWNLOAD_URL", "")

    return templates.TemplateResponse("account.html", {
        "request": request, 
        "user": user, 
        "licenses": licenses,
        "active_license": display_license,
        "is_license_active": is_license_active,
        "download_url": download_url,
        "total_spent": total_spent
    })

@router.get("/logout")
async def user_logout():
    resp = RedirectResponse(url="/")
    resp.delete_cookie("user_auth")
    return resp

# === API ДЛЯ ЛАУНЧЕРА (Новые методы) ===

@router.post("/api/login_launcher")
async def api_login_launcher(request: Request, data: dict = Body(...)):
    email = data.get("username", "").strip().lower()
    password = data.get("password", "")

    async with request.app.state.pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id, email, password_hash, username FROM users WHERE email=$1", email)
        
        if not user or not verify_password(password, user["password_hash"]):
            return JSONResponse({"status": "error", "message": "Invalid credentials"}, status_code=401)
        
        await conn.execute("UPDATE users SET last_login=NOW() WHERE id=$1", user["id"])
        
        token = make_jwt(user["id"], user["email"])
        
        return {
            "status": "success",
            "access_token": token,
            "username": user["username"]
        }

@router.get("/api/me_launcher")
async def get_api_profile_launcher(request: Request):
    try:
        user = await get_current_user(request.app, request)
        
        # Данные берутся из guards.py, где мы добавили uid и group
        return {
            "uid": str(user["uid"]),
            "username": user["username"],
            "email": user["email"],
            "group": user["group"] or "User",
            "expires": "Unlimited", 
            "avatar_url": "" 
        }
    except Exception as e:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
