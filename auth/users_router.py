from fastapi import APIRouter, Request, Form, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from .jwt_utils import hash_password, verify_password, make_jwt
from .guards import get_current_user
from .email_service import create_and_send_confirmation
import secrets, string
from datetime import date, datetime
from typing import Optional

router = APIRouter()
templates = Jinja2Templates(directory="templates")

def generate_license_key():
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))

# ==========================================
# 1. ЛОГИКА ДЛЯ САЙТА (HTML FORMS)
# ==========================================

@router.get("/login", response_class=HTMLResponse)
async def user_login_page(request: Request):
    try:
        # Если кука есть и валидна - сразу в кабинет
        _ = await get_current_user(request.app, request)
        return RedirectResponse("/cabinet", status_code=302)
    except:
        return templates.TemplateResponse("user_login.html", {"request": request, "error": None})

@router.post("/login")
async def user_login(request: Request, email: str = Form(...), password: str = Form(...)):
    # 1. Очистка данных
    email = email.strip().lower()
    
    async with request.app.state.pool.acquire() as conn:
        # 2. Поиск юзера
        user = await conn.fetchrow("SELECT id, email, password_hash FROM users WHERE email=$1", email)
        
        # 3. Проверка пароля
        if not user or not verify_password(password, user["password_hash"]):
            return templates.TemplateResponse("user_login.html", {
                "request": request, 
                "error": "Неверный email или пароль"
            }, status_code=401)
            
        # 4. Обновляем дату входа
        await conn.execute("UPDATE users SET last_login=NOW() WHERE id=$1", user["id"])

    # 5. Генерация токена
    token = make_jwt(user["id"], user["email"])
    
    # 6. РЕДИРЕКТ И УСТАНОВКА КУКИ (ИСПРАВЛЕНО)
    resp = RedirectResponse(url="/cabinet", status_code=302)
    
    # ВАЖНО: secure=False позволяет работать без HTTPS.
    # Если сайт будет на https://, браузер все равно примет куку.
    resp.set_cookie(
        "user_auth", 
        token, 
        httponly=True, 
        samesite="lax", 
        secure=False,  # <--- БЫЛО True, СТАЛО False. Это починит вход.
        max_age=7*24*3600
    )
    return resp

# ... (Остальные методы: register, cabinet, logout и т.д. оставляем как есть) ...

@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    try:
        _ = await get_current_user(request.app, request)
        return RedirectResponse("/cabinet", status_code=302)
    except:
        return templates.TemplateResponse("register.html", {"request": request, "error": None})

@router.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    email: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    accept_terms: str = Form(None),
):
    if not accept_terms:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Необходимо принять соглашение"}, status_code=400)
    if not username.strip():
        return templates.TemplateResponse("register.html", {"request": request, "error": "Имя пользователя обязательно"}, status_code=400)
    if password != password2:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Пароли не совпадают"}, status_code=400)
    if len(password) < 6:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Пароль должен быть ≥ 6 символов"}, status_code=400)

    email = email.strip().lower()
    pw_hash = hash_password(password)

    async with request.app.state.pool.acquire() as conn:
        exist = await conn.fetchval("SELECT 1 FROM users WHERE email=$1", email)
        if exist:
             return templates.TemplateResponse("register.html", {"request": request, "error": "Email уже зарегистрирован"}, status_code=400)

        row = await conn.fetchrow(
            """INSERT INTO users (email, password_hash, username) VALUES ($1, $2, $3) RETURNING id, email, uid, username""",
            email, pw_hash, username.strip()
        )
        license_key = generate_license_key()
        await conn.execute("""INSERT INTO licenses (license_key, status, user_name, user_uid) VALUES ($1, 'expired', $2, $3)""", license_key, row["username"], row["uid"])

    try:
        await create_and_send_confirmation(request.app, row["id"], row["email"])
    except: pass 

    token = make_jwt(row["id"], row["email"])
    resp = RedirectResponse(url="/cabinet", status_code=302)
    
    # Здесь тоже ставим secure=False для регистрации
    resp.set_cookie("user_auth", token, httponly=True, samesite="lax", secure=False, max_age=7*24*3600)
    return resp

@router.get("/cabinet", response_class=HTMLResponse)
async def account_page(request: Request):
    try:
        user = await get_current_user(request.app, request)
    except:
        return RedirectResponse(url="/login", status_code=302)

    async with request.app.state.pool.acquire() as conn:
        licenses = await conn.fetch("""SELECT license_key, status, expires, hwid FROM licenses WHERE user_uid = $1 ORDER BY created_at DESC""", user["uid"])
        total_spent = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM purchases WHERE user_uid=$1", user["uid"])

        found_active = None
        for lic in licenses:
            if lic['status'] == 'active':
                if lic['expires'] and lic['expires'] >= date.today():
                    found_active = lic
                    break
        
        is_license_active = (found_active is not None)
        display_license = found_active if found_active else (licenses[0] if licenses else None)

    download_url = getattr(request.app.state, "DOWNLOAD_URL", "")

    return templates.TemplateResponse("account.html", {
            "request": request, "user": user, "licenses": licenses, 
            "active_license": display_license, "is_license_active": is_license_active, 
            "download_url": download_url, "total_spent": total_spent
        })

@router.get("/logout")
async def user_logout():
    resp = RedirectResponse(url="/")
    resp.delete_cookie("user_auth")
    return resp

# ==========================================
# 2. ЛОГИКА ДЛЯ ЛАУНЧЕРА (API JSON)
# ==========================================

@router.post("/api/login_launcher")
async def api_login_launcher(request: Request, data: dict = Body(...)):
    # Принимаем JSON
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
        return {
            "uid": user["uid"],
            "username": user["username"],
            "email": user["email"],
            "group": user.get("group", "User"),
            "expires": str(user.get("expires", "Unlimited")),
            "avatar_url": user.get("avatar_url", "")
        }
    except:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
