# auth/users_router.py
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from .jwt_utils import hash_password, verify_password, make_jwt
from .guards import get_current_user
from .email_service import create_and_send_confirmation
import secrets, string
from fastapi import Form
from .jwt_utils import verify_password, hash_password

router = APIRouter()
templates = Jinja2Templates(directory="templates")

def generate_license_key():
    # 8-символьный, уникальный, верхний регистр + цифры
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))

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
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Необходимо принять соглашение"},
            status_code=400,
        )

    if not username.strip():
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Имя пользователя обязательно"},
            status_code=400,
        )

    if password != password2:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Пароли не совпадают"},
            status_code=400,
        )

    if len(password) < 6:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Пароль должен быть ≥ 6 символов"},
            status_code=400,
        )

    email = email.strip().lower()
    pw_hash = hash_password(password)

    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (email, password_hash, username)
            VALUES ($1, $2, $3)
            RETURNING id, email, uid, username
            """,
            email, pw_hash, username.strip()
        )

        # создаём лицензию сразу (истекшую по умолчанию)
        license_key = generate_license_key()
        await conn.execute(
            """
            INSERT INTO licenses (license_key, status, user_name, user_uid)
            VALUES ($1, 'expired', $2, $3)
            """,
            license_key,
            row["username"],
            row["uid"]
        )

    await create_and_send_confirmation(request.app, row["id"], row["email"])

    token = make_jwt(row["id"], row["email"])
    resp = RedirectResponse(url="/cabinet", status_code=302)
    resp.set_cookie("user_auth", token, httponly=True, samesite="lax", secure=True, max_age=7*24*3600)
    return resp

@router.get("/login", response_class=HTMLResponse)
async def user_login_page(request: Request):
    try:
        _ = await get_current_user(request.app, request)
        return RedirectResponse("/cabinet", status_code=302)
    except:
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
    resp.set_cookie("user_auth", token, httponly=True, samesite="lax", secure=True, max_age=7*24*3600)
    return resp

@router.get("/cabinet", response_class=HTMLResponse)
async def account_page(request: Request):
    try:
        user = await get_current_user(request.app, request)
    except:
        # если не авторизован → редирект на логин
        return RedirectResponse(url="/login", status_code=302)

    async with request.app.state.pool.acquire() as conn:
        licenses = await conn.fetch(
            """
            SELECT license_key, status, expires
            FROM licenses
            WHERE user_uid = $1
            ORDER BY created_at DESC
            """,
            user["uid"],
        )

    # передаём ссылку загрузки из переменных окружения (пробрасывается в app.state)
    download_url = getattr(request.app.state, "DOWNLOAD_URL", "")

    return templates.TemplateResponse(
        "account.html",
        {"request": request, "user": user, "licenses": licenses, "download_url": download_url}
    )

@router.get("/logout")
async def user_logout():
    resp = RedirectResponse(url="/")
    resp.delete_cookie("user_auth")
    return resp




@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request):
    try:
        user = await get_current_user(request.app, request)
    except:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("change_password.html", {"request": request, "user": user, "error": None})

@router.post("/change-password", response_class=HTMLResponse)
async def change_password_submit(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    new_password2: str = Form(...),
):
    try:
        user = await get_current_user(request.app, request)
    except:
        return RedirectResponse(url="/login", status_code=302)

    if new_password != new_password2:
        return templates.TemplateResponse("change_password.html", {"request": request, "user": user, "error": "Пароли не совпадают"})

    if len(new_password) < 6:
        return templates.TemplateResponse("change_password.html", {"request": request, "user": user, "error": "Пароль должен быть ≥ 6 символов"})

    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT password_hash FROM users WHERE id=$1", user["id"])
        if not row or not verify_password(old_password, row["password_hash"]):
            return templates.TemplateResponse("change_password.html", {"request": request, "user": user, "error": "Старый пароль неверен"})

        new_hash = hash_password(new_password)
        await conn.execute("UPDATE users SET password_hash=$1 WHERE id=$2", new_hash, user["id"])

    return RedirectResponse(url="/cabinet", status_code=302)

