from fastapi import APIRouter, Request, Form, Body, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel # ВАЖНО: Добавили Pydantic

from .jwt_utils import hash_password, verify_password, make_jwt
from .guards import get_current_user
from .email_service import create_and_send_confirmation
from datetime import date, datetime, timedelta
import secrets
import string
import uuid

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# === МОДЕЛЬ ДЛЯ ВХОДА ЛАУНЧЕРА (Новое) ===
class LauncherLoginModel(BaseModel):
    username: str
    password: str

# ==========================================
#  РЕГИСТРАЦИЯ
# ==========================================
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
        
        row = await conn.fetchrow(
            "INSERT INTO users (email, password_hash, username) VALUES ($1, $2, $3) RETURNING id, email, uid, username",
            email, pw_hash, (username or "").strip() or None
        )

        lic_key_internal = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16))
        await conn.execute(
            "INSERT INTO licenses (license_key, status, user_name, user_uid) VALUES ($1, 'expired', $2, $3)",
            lic_key_internal, row['username'], row['uid']
        )

    try:
        await create_and_send_confirmation(request.app, row["id"], row["email"])
    except: pass

    token = make_jwt(row["id"], row["email"])
    resp = RedirectResponse(url="/cabinet", status_code=302)
    resp.set_cookie("user_auth", token, path="/", httponly=True, samesite="lax", secure=False, max_age=30*24*3600)
    return resp


# ==========================================
#  ВХОД (Web)
# ==========================================
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
    resp.set_cookie("user_auth", token, path="/", httponly=True, samesite="lax", secure=False, max_age=30*24*3600)
    return resp


# ==========================================
#  КАБИНЕТ
# ==========================================
@router.get("/cabinet", response_class=HTMLResponse)
async def account_page(request: Request):
    try:
        user = await get_current_user(request)
    except:
        return RedirectResponse("/login", status_code=302)

    async with request.app.state.pool.acquire() as conn:
        licenses = await conn.fetch("SELECT license_key, status, expires, hwid FROM licenses WHERE user_uid = $1 ORDER BY created_at DESC", user["uid"])
        total_spent = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM purchases WHERE user_uid=$1", user["uid"])

    found_active = None
    if licenses:
        for lic in licenses:
            if lic['status'] == 'active' and lic['expires'] and lic['expires'] >= date.today():
                found_active = lic
                break
    
    display_license = found_active if found_active else (licenses[0] if licenses else None)
    
    return templates.TemplateResponse("account.html", {
        "request": request, "user": user, "licenses": licenses,
        "active_license": display_license, "is_license_active": (found_active is not None),
        "download_url": getattr(request.app.state, "DOWNLOAD_URL", ""), "total_spent": total_spent
    })


# ==========================================
#  АКТИВАЦИЯ
# ==========================================
@router.post("/cabinet", response_class=HTMLResponse)
async def activate_license(request: Request, license_key: str = Form(...)):
    try:
        user = await get_current_user(request)
    except:
        return RedirectResponse("/login", status_code=302)

    input_token = license_key.strip()

    async with request.app.state.pool.acquire() as conn:
        token_row = await conn.fetchrow("SELECT * FROM activation_tokens WHERE token=$1 AND status='unused'", input_token)
        
        if not token_row:
            return templates.TemplateResponse("activation_result.html", {"request": request, "user": user, "success": False, "error": "Ключ не найден или уже использован"})

        try:
            user_lic = await conn.fetchrow("SELECT * FROM licenses WHERE user_uid=$1 ORDER BY created_at DESC LIMIT 1", user['uid'])
            days_to_add = token_row['duration_days']
            today = date.today()
            new_expires = None
            
            if user_lic:
                current_expires = user_lic['expires']
                is_active_now = (user_lic['status'] == 'active')
                if is_active_now and current_expires and current_expires >= today:
                    new_expires = current_expires + timedelta(days=days_to_add)
                else:
                    new_expires = today + timedelta(days=days_to_add)
                target_key = user_lic['license_key']
                async with conn.transaction():
                    await conn.execute("UPDATE activation_tokens SET status='used', used_at=NOW(), used_by_uid=$1 WHERE id=$2", user['uid'], token_row['id'])
                    await conn.execute("UPDATE licenses SET status='active', expires=$1, activated_at=NOW() WHERE license_key=$2", new_expires, target_key)
            else:
                new_expires = today + timedelta(days=days_to_add)
                new_lic_key = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16))
                async with conn.transaction():
                    await conn.execute("UPDATE activation_tokens SET status='used', used_at=NOW(), used_by_uid=$1 WHERE id=$2", user['uid'], token_row['id'])
                    await conn.execute("INSERT INTO licenses (license_key, status, user_name, user_uid, expires, activated_at) VALUES ($1, 'active', $2, $3, $4, NOW())", new_lic_key, user['username'], user['uid'], new_expires)

            return templates.TemplateResponse("activation_result.html", {"request": request, "user": user, "success": True, "days": days_to_add, "new_expires": new_expires})
        except Exception as e:
            return templates.TemplateResponse("activation_result.html", {"request": request, "user": user, "success": False, "error": "Ошибка БД"})


# ==========================================
#  СМЕНА ПАРОЛЯ
# ==========================================
@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request):
    try:
        user = await get_current_user(request)
    except:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("change_password.html", {"request": request, "user": user})

@router.post("/change-password", response_class=HTMLResponse)
async def change_password_submit(request: Request, current_password: str = Form(...), new_password: str = Form(...), confirm_password: str = Form(...)):
    try:
        user = await get_current_user(request)
    except:
        return RedirectResponse("/login", status_code=302)
    error, success = None, None
    if new_password != confirm_password: error = "Пароли не совпадают"
    elif len(new_password) < 6: error = "Пароль слишком короткий"
    else:
        async with request.app.state.pool.acquire() as conn:
            db_user = await conn.fetchrow("SELECT password_hash FROM users WHERE id=$1", user['id'])
            if not verify_password(current_password, db_user['password_hash']): error = "Неверный текущий пароль"
            else:
                await conn.execute("UPDATE users SET password_hash=$1 WHERE id=$2", hash_password(new_password), user['id'])
                success = "Пароль изменен!"
    return templates.TemplateResponse("change_password.html", {"request": request, "user": user, "error": error, "success": success})

@router.get("/logout")
async def user_logout():
    resp = RedirectResponse(url="/")
    resp.delete_cookie("user_auth", path="/")
    return resp


# ==========================================
#  API ДЛЯ ЛАУНЧЕРА (ИСПРАВЛЕНО)
# ==========================================

@router.post("/api/login_launcher")
async def api_login_launcher(request: Request, login_data: LauncherLoginModel):
    """
    Теперь принимаем данные через Pydantic модель LauncherLoginModel.
    FastAPI сам проверит, что это JSON и что там есть username/password.
    """
    email = login_data.username.strip().lower()
    password = login_data.password

    print(f"[LAUNCHER] Login attempt: {email}") # Лог в консоль

    async with request.app.state.pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id, email, password_hash, username, uid FROM users WHERE email=$1", email)
        
        if not user:
            print("[LAUNCHER] User not found")
            return JSONResponse({"status": "error", "message": "Invalid credentials"}, status_code=401)
        
        if not verify_password(password, user["password_hash"]):
            print("[LAUNCHER] Wrong password")
            return JSONResponse({"status": "error", "message": "Invalid credentials"}, status_code=401)
        
        # Обновляем last_login
        await conn.execute("UPDATE users SET last_login=NOW() WHERE id=$1", user["id"])
        
        token = make_jwt(user["id"], user["email"])
        print(f"[LAUNCHER] Success for {user['username']}")
        
        return {
            "status": "success", 
            "access_token": token, 
            "username": user["username"],
            "uid": str(user["uid"])
        }

@router.get("/api/me_launcher")
async def get_api_profile_launcher(request: Request):
    # Теперь эта функция корректно возьмет юзера через guards.py
    try:
        user = await get_current_user(request)
        
        # Получаем статус лицензии
        async with request.app.state.pool.acquire() as conn:
             licenses = await conn.fetch("SELECT status, expires FROM licenses WHERE user_uid=$1", user["uid"])
        
        status_str = "No License"
        expires_str = "-"
        
        if licenses:
            for lic in licenses:
                if lic['status'] == 'active' and lic['expires'] and lic['expires'] >= date.today():
                    status_str = "Active"
                    expires_str = str(lic['expires'])
                    break
        
        return {
            "uid": str(user["uid"]),
            "username": user["username"],
            "email": user["email"],
            "group": user["group"] or "User",
            "expires": expires_str,
            "status": status_str,
            "avatar_url": "" 
        }
    except Exception as e:
        print(f"[API ME] Error: {e}")
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

