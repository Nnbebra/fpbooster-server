# auth/users_router.py
from fastapi import APIRouter, Request, Form, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from .jwt_utils import hash_password, verify_password, make_jwt
from .guards import get_current_user
from .email_service import create_and_send_confirmation
from fastapi.responses import RedirectResponse

router = APIRouter()
templates = Jinja2Templates(directory="templates")

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
    password: str = Form(...),
    username: str = Form(None),
    accept_terms: str = Form(None),   # добавили чекбокс
):
    if not accept_terms:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Необходимо принять соглашение"},
            status_code=400,
        )

    email = email.strip().lower()
    if len(password) < 6:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Пароль должен быть ≥ 6 символов"},
            status_code=400,
        )

    async with request.app.state.pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM users WHERE email=$1", email)
        if exists:
            return templates.TemplateResponse(
                "register.html",
                {"request": request, "error": "Такой email уже зарегистрирован"},
                status_code=400,
            )
        pw_hash = hash_password(password)
        row = await conn.fetchrow(
            "INSERT INTO users (email, password_hash, username) VALUES ($1, $2, $3) RETURNING id, email",
            email, pw_hash, (username or "").strip() or None
        )

    # отправка письма подтверждения (пока заглушка)
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
            """SELECT l.license_key, l.status, l.expires
               FROM licenses l
               LEFT JOIN user_licenses ul ON ul.license_key = l.license_key
               WHERE ul.user_id = $1
               ORDER BY l.created_at DESC""",
            user["id"],
        )

    return templates.TemplateResponse(
        "account.html",
        {"request": request, "user": user, "licenses": licenses}
    )

@router.get("/logout")
async def user_logout():
    resp = RedirectResponse(url="/")
    resp.delete_cookie("user_auth")
    return resp


