from fastapi import APIRouter, Request, Form, Body, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from .jwt_utils import hash_password, verify_password, make_jwt
from .guards import get_current_user
from .email_service import create_and_send_confirmation

router = APIRouter()
templates = Jinja2Templates(directory="templates")

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
        # ВАЖНО: Убедись, что в твоей БД есть колонка uid. Если нет - убери uid из RETURNING
        row = await conn.fetchrow(
            "INSERT INTO users (email, password_hash, username) VALUES ($1, $2, $3) RETURNING id, email",
            email, pw_hash, (username or "").strip() or None
        )

    try:
        await create_and_send_confirmation(request.app, row["id"], row["email"])
    except: pass

    token = make_jwt(row["id"], row["email"])
    resp = RedirectResponse(url="/cabinet", status_code=302)
    # ИСПРАВЛЕНИЕ: secure=False, чтобы работало без HTTPS сертификата
    resp.set_cookie("user_auth", token, httponly=True, samesite="lax", secure=False, max_age=7*24*3600)
    return resp


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
    # ИСПРАВЛЕНИЕ: secure=False
    resp.set_cookie("user_auth", token, httponly=True, samesite="lax", secure=False, max_age=7*24*3600)
    return resp

@router.get("/cabinet", response_class=HTMLResponse)
async def account_page(request: Request):
    try:
        # Используем твою сигнатуру (app, request)
        user = await get_current_user(request.app, request)
    except:
        return RedirectResponse("/login", status_code=302)

    async with request.app.state.pool.acquire() as conn:
        # Убедись, что таблица licenses существует
        licenses = await conn.fetch(
            """SELECT l.license_key, l.status, l.expires
               FROM licenses l
               LEFT JOIN user_licenses ul ON ul.license_key = l.license_key
               WHERE ul.user_id = $1
               ORDER BY l.created_at DESC""",
            user["id"],
        )
    return templates.TemplateResponse("account.html", {"request": request, "user": user, "licenses": licenses})

@router.get("/logout")
async def user_logout():
    resp = RedirectResponse(url="/")
    resp.delete_cookie("user_auth")
    return resp


# ==========================================
# ДОБАВЛЕНО ДЛЯ ЛАУНЧЕРА (Не ломает сайт)
# ==========================================

@router.post("/api/login_launcher")
async def api_login_launcher(request: Request, data: dict = Body(...)):
    # Принимаем JSON от C#
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
        # Используем твою функцию guards
        user = await get_current_user(request.app, request)
        
        # Безопасно получаем поля, даже если их нет в БД
        uid = user["uid"] if "uid" in user else "NoUID"
        grp = user["group"] if "group" in user else "User"
        
        return {
            "uid": str(uid),
            "username": user["username"],
            "email": user["email"],
            "group": str(grp),
            "expires": "Unlimited", 
            "avatar_url": "" 
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=401)
