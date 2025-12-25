from fastapi import APIRouter, Request, Form, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from .jwt_utils import hash_password, verify_password, make_jwt
from .guards import get_current_user
from .email_service import create_and_send_confirmation
from datetime import date, timedelta

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
        # Создаем пользователя
        row = await conn.fetchrow(
            "INSERT INTO users (email, password_hash, username) VALUES ($1, $2, $3) RETURNING id, email, uid, username",
            email, pw_hash, (username or "").strip() or None
        )
        # Сразу создаем ему "пустую" запись лицензии (важно для дальнейшей активации)
        # Генерируем случайный license_key для внутренней идентификации
        import secrets, string
        lic_key = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16))
        await conn.execute(
            "INSERT INTO licenses (license_key, status, user_name, user_uid) VALUES ($1, 'expired', $2, $3)",
            lic_key, row['username'], row['uid']
        )

    try:
        await create_and_send_confirmation(request.app, row["id"], row["email"])
    except: pass

    token = make_jwt(row["id"], row["email"])
    resp = RedirectResponse(url="/cabinet", status_code=302)
    # path="/" нужен, чтобы кука была видна на всем сайте
    resp.set_cookie("user_auth", token, path="/", httponly=True, samesite="lax", secure=False, max_age=30*24*3600)
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
    # path="/" критически важен для работы на главной странице
    resp.set_cookie("user_auth", token, path="/", httponly=True, samesite="lax", secure=False, max_age=30*24*3600)
    return resp

# === ЛИЧНЫЙ КАБИНЕТ (ОТОБРАЖЕНИЕ) ===
@router.get("/cabinet", response_class=HTMLResponse)
async def account_page(request: Request):
    try:
        user = await get_current_user(request.app, request)
    except:
        return RedirectResponse("/login", status_code=302)

    async with request.app.state.pool.acquire() as conn:
        licenses = await conn.fetch(
            """SELECT license_key, status, expires, hwid 
               FROM licenses WHERE user_uid = $1 ORDER BY created_at DESC""",
            user["uid"],
        )
        total_spent = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM purchases WHERE user_uid=$1", user["uid"]
        )

    # Логика поиска активной лицензии
    found_active = None
    if licenses:
        for lic in licenses:
            if lic['status'] == 'active':
                if lic['expires'] and lic['expires'] >= date.today():
                    found_active = lic
                    break
    
    display_license = found_active if found_active else (licenses[0] if licenses else None)
    
    return templates.TemplateResponse("account.html", {
        "request": request, 
        "user": user, 
        "licenses": licenses,
        "active_license": display_license,
        "is_license_active": (found_active is not None),
        "download_url": getattr(request.app.state, "DOWNLOAD_URL", ""),
        "total_spent": total_spent
    })

# === ЛИЧНЫЙ КАБИНЕТ (АКТИВАЦИЯ КЛЮЧА ИЗ activation_tokens) ===
@router.post("/cabinet", response_class=HTMLResponse)
async def activate_license(request: Request, license_key: str = Form(...)):
    try:
        user = await get_current_user(request.app, request)
    except:
        return RedirectResponse("/login", status_code=302)

    input_token = license_key.strip()
    error_msg = None

    async with request.app.state.pool.acquire() as conn:
        # 1. Ищем ключ в таблице activation_tokens
        token_row = await conn.fetchrow(
            "SELECT * FROM activation_tokens WHERE token=$1 AND status='unused'", 
            input_token
        )
        
        if not token_row:
            error_msg = "Ключ не найден или уже использован"
        else:
            # 2. Нашли ключ. Теперь ищем лицензию пользователя, которую надо продлить.
            # Берем самую свежую лицензию пользователя
            user_lic = await conn.fetchrow(
                "SELECT * FROM licenses WHERE user_uid=$1 ORDER BY created_at DESC LIMIT 1",
                user['uid']
            )

            # Если вдруг лицензии нет (старая регистрация), создадим новую "на лету"
            lic_key_target = None
            current_expires = None
            is_active_now = False

            if user_lic:
                lic_key_target = user_lic['license_key']
                current_expires = user_lic['expires']
                is_active_now = (user_lic['status'] == 'active')
            else:
                # Генерируем новую лицензию если не нашли
                import secrets, string
                lic_key_target = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16))
                await conn.execute(
                    "INSERT INTO licenses (license_key, status, user_name, user_uid) VALUES ($1, 'expired', $2, $3)",
                    lic_key_target, user['username'], user['uid']
                )

            # 3. Считаем новую дату окончания
            days_to_add = token_row['duration_days']
            today = date.today()

            if is_active_now and current_expires and current_expires >= today:
                # Если уже активна - прибавляем к дате окончания
                new_expires = current_expires + timedelta(days=days_to_add)
            else:
                # Если истекла или не было - считаем от сегодня
                new_expires = today + timedelta(days=days_to_add)

            # 4. ТРАНЗАКЦИЯ: Гасим токен и обновляем лицензию
            async with conn.transaction():
                # Помечаем токен как использованный
                await conn.execute(
                    "UPDATE activation_tokens SET status='used', used_at=NOW(), used_by_uid=$1 WHERE id=$2",
                    user['uid'], token_row['id']
                )
                
                # Обновляем лицензию
                await conn.execute(
                    "UPDATE licenses SET status='active', expires=$1, activated_at=NOW() WHERE license_key=$2",
                    new_expires, lic_key_target
                )
            
            # Успех - перезагружаем страницу
            return RedirectResponse("/cabinet", status_code=302)

    # Если была ошибка - рендерим страницу с ошибкой
    async with request.app.state.pool.acquire() as conn:
        licenses = await conn.fetch("SELECT license_key, status, expires, hwid FROM licenses WHERE user_uid = $1 ORDER BY created_at DESC", user["uid"])
        total_spent = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM purchases WHERE user_uid=$1", user["uid"])

    found_active = None
    if licenses:
        for l in licenses:
            if l['status'] == 'active' and l['expires'] and l['expires'] >= date.today():
                found_active = l
                break
    
    return templates.TemplateResponse("account.html", {
        "request": request, 
        "user": user, 
        "licenses": licenses,
        "active_license": found_active if found_active else (licenses[0] if licenses else None),
        "is_license_active": (found_active is not None),
        "download_url": getattr(request.app.state, "DOWNLOAD_URL", ""),
        "total_spent": total_spent,
        "error": error_msg 
    })

@router.get("/logout")
async def user_logout():
    resp = RedirectResponse(url="/")
    resp.delete_cookie("user_auth", path="/") # Удаляем куку корректно
    return resp

# === API ДЛЯ ЛАУНЧЕРА ===
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
        return {"status": "success", "access_token": token, "username": user["username"]}

@router.get("/api/me_launcher")
async def get_api_profile_launcher(request: Request):
    try:
        user = await get_current_user(request.app, request)
        return {
            "uid": str(user["uid"]),
            "username": user["username"],
            "email": user["email"],
            "group": user["group"] or "User",
            "expires": "Unlimited", 
            "avatar_url": "" 
        }
    except:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
