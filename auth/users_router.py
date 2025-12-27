from fastapi import APIRouter, Request, Form, Body, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from typing import List, Optional
from pydantic import BaseModel
from datetime import date, datetime, timedelta
import secrets
import string

from .jwt_utils import hash_password, verify_password, make_jwt
from .guards import get_current_user
from .email_service import create_and_send_confirmation

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# === МОДЕЛИ ===
class LauncherLoginModel(BaseModel):
    username: str
    password: str

class ProductSchema(BaseModel):
    id: int
    name: str
    description: str
    image_url: str
    download_url: str
    version: str

class UserProfileSchema(BaseModel):
    uid: str
    username: str
    email: str
    group_name: str
    group_slug: str
    avatar_url: Optional[str] = None
    expires: Optional[str] = None
    available_products: List[ProductSchema]

# Цвета групп для шаблонов
GROUP_COLORS = {
    "tech-admin": "purple",     
    "admin": "indigo",          
    "senior-staff": "pink",     
    "staff": "danger",          
    "moderator": "orange",      
    "media": "cyan",            
    "plus": "primary",          
    "alpha": "azure",           
    "premium": "primary",       
    "basic": "success",         
    "user": "secondary"         
}

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
        
        # Создаем пользователя
        row = await conn.fetchrow(
            "INSERT INTO users (email, password_hash, username) VALUES ($1, $2, $3) RETURNING id, email, uid, username",
            email, pw_hash, (username or "").strip() or None
        )
        
        # ВНИМАНИЕ: Мы убрали INSERT INTO licenses, так как этой таблицы больше нет.
        # Группу по умолчанию (User) можно не выдавать явно, если логика подразумевает отсутствие группы = User.

    try:
        await create_and_send_confirmation(request.app, row["id"], row["email"])
    except: pass

    # Авторизуем сразу после регистрации
    token = make_jwt(row["id"], row["email"])
    resp = RedirectResponse(url="/cabinet", status_code=302)
    
    # КУКИ УСТАНАВЛИВАЮТСЯ ЗДЕСЬ
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
    
    # КУКИ УСТАНАВЛИВАЮТСЯ ЗДЕСЬ
    resp.set_cookie("user_auth", token, path="/", httponly=True, samesite="lax", secure=False, max_age=30*24*3600)
    return resp


# ==========================================
#   КАБИНЕТ
# ==========================================
@router.get("/cabinet", response_class=HTMLResponse)
async def account_page(request: Request):
    try:
        user = await get_current_user(request)
    except:
        return RedirectResponse("/login", status_code=302)

    async with request.app.state.pool.acquire() as conn:
        # 1. Получаем сумму покупок
        total_spent = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM purchases WHERE user_uid=$1", user["uid"])
        
        # 2. Получаем ВСЕ группы пользователя для отображения
        # Используем НОВУЮ таблицу user_groups
        groups = await conn.fetch("""
            SELECT g.name, g.slug, ug.expires_at, ug.is_active
            FROM user_groups ug
            JOIN groups g ON ug.group_id = g.id
            WHERE ug.user_uid = $1
            ORDER BY ug.is_active DESC, ug.expires_at DESC
        """, user["uid"])

        # 3. Определяем "Главную" активную группу
        active_group = None
        for g in groups:
            if g['is_active'] and (g['expires_at'] is None or g['expires_at'] > datetime.now()):
                active_group = g
                break
        
        # 4. Ссылка на скачивание
        # Берем любой доступный продукт
        download_url = "/api/client/products" # Заглушка, если конкретного URL нет

    # Подготовка данных для шаблона
    # Старый шаблон account.html ждет переменные licenses, active_license.
    # Мы их эмулируем, чтобы шаблон не ломался.
    
    display_license = None
    if active_group:
        display_license = {
            "license_key": active_group['name'], # Вместо ключа показываем имя группы
            "status": "active",
            "expires": active_group['expires_at'].date() if active_group['expires_at'] else date(2099, 1, 1),
            "hwid": "Привязан" # Заглушка
        }

    group_name = active_group['name'] if active_group else "User"
    group_slug = active_group['slug'] if active_group else "user"
    
    return templates.TemplateResponse("account.html", {
        "request": request, 
        "user": user, 
        "licenses": [], # Пустой список, чтобы шаблон не ругался
        "active_license": display_license, 
        "is_license_active": (active_group is not None),
        "download_url": download_url, 
        "total_spent": total_spent,
        "group_name": group_name,
        "group_slug": group_slug,
        "group_colors": GROUP_COLORS
    })


# ==========================================
#  АКТИВАЦИЯ (Через group_keys)
# ==========================================
@router.post("/cabinet", response_class=HTMLResponse)
async def activate_license(request: Request, license_key: str = Form(...)):
    try:
        user = await get_current_user(request)
    except:
        return RedirectResponse("/login", status_code=302)

    key_value = license_key.strip()
    if not key_value:
         return templates.TemplateResponse("activation_result.html", {"request": request, "user": user, "success": False, "error": "Введите ключ"})

    try:
        async with request.app.state.pool.acquire() as conn:
            async with conn.transaction():
                # 1. Ищем ключ в НОВОЙ таблице group_keys
                key_data = await conn.fetchrow("""
                    SELECT id, group_id, duration_days 
                    FROM group_keys 
                    WHERE key_code = $1 AND is_used = FALSE
                """, key_value)

                if not key_data:
                    return templates.TemplateResponse("activation_result.html", {"request": request, "user": user, "success": False, "error": "Ключ не найден или уже использован"})

                group_id = key_data['group_id']
                duration = key_data['duration_days']
                
                # 2. Проверяем текущую подписку на эту группу
                existing = await conn.fetchrow("""
                    SELECT id, expires_at FROM user_groups 
                    WHERE user_uid = $1 AND group_id = $2
                """, user['uid'], group_id)

                now = datetime.now()
                new_expires = None
                
                if existing:
                    # Продлеваем
                    current_expires = existing['expires_at']
                    if current_expires > now:
                        new_expires = current_expires + timedelta(days=duration)
                    else:
                        new_expires = now + timedelta(days=duration)
                    
                    await conn.execute("""
                        UPDATE user_groups 
                        SET expires_at = $1, is_active = TRUE, granted_at = NOW()
                        WHERE id = $2
                    """, new_expires, existing['id'])
                else:
                    # Выдаем новую
                    new_expires = now + timedelta(days=duration)
                    await conn.execute("""
                        INSERT INTO user_groups (user_uid, group_id, expires_at, is_active, granted_at)
                        VALUES ($1, $2, $3, TRUE, NOW())
                    """, user['uid'], group_id, new_expires)

                # 3. Гасим ключ
                await conn.execute("""
                    UPDATE group_keys 
                    SET is_used = TRUE, activated_by = $1, used_at = NOW()
                    WHERE id = $2
                """, user['uid'], key_data['id'])

                # 4. Лог
                await conn.execute("""
                    INSERT INTO purchases (user_uid, plan, amount, currency, source, token_code, created_at) 
                    VALUES ($1, $2, 0, 'KEY', 'key_activation', $3, NOW())
                """, user['uid'], f"activation_group_{group_id}_{duration}d", key_value)

                return templates.TemplateResponse("activation_result.html", {"request": request, "user": user, "success": True, "days": duration, "new_expires": new_expires})

    except Exception as e:
        print(f"Activation Error: {e}")
        return templates.TemplateResponse("activation_result.html", {"request": request, "user": user, "success": False, "error": "Ошибка при активации"})


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
#  API ДЛЯ ЛАУНЧЕРА
# ==========================================

@router.post("/api/login_launcher")
async def api_login_launcher(request: Request, login_data: LauncherLoginModel):
    email = login_data.username.strip().lower()
    password = login_data.password

    async with request.app.state.pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id, email, password_hash, username, uid FROM users WHERE email=$1", email)
        
        if not user or not verify_password(password, user["password_hash"]):
            return JSONResponse({"status": "error", "message": "Invalid credentials"}, status_code=401)
        
        await conn.execute("UPDATE users SET last_login=NOW() WHERE id=$1", user["id"])
        token = make_jwt(user["id"], user["email"])
        
        return {
            "status": "success", 
            "access_token": token, 
            "username": user["username"],
            "uid": str(user["uid"])
        }

@router.get("/api/me", response_model=UserProfileSchema)
async def get_my_profile(request: Request, user=Depends(get_current_user)):
    """
    Профиль для лаунчера с расчетом доступных продуктов на основе ГРУППЫ.
    """
    async with request.app.state.pool.acquire() as conn:
        # 1. Получаем активную группу
        group_row = await conn.fetchrow("""
            SELECT g.name, g.slug, g.access_level, ug.expires_at
            FROM user_groups ug
            JOIN groups g ON ug.group_id = g.id
            WHERE ug.user_uid = $1 AND ug.is_active = TRUE AND ug.expires_at > NOW()
            ORDER BY g.access_level DESC
            LIMIT 1
        """, user['uid'])

        group_name = group_row['name'] if group_row else "User"
        slug = group_row['slug'] if group_row else "user"
        access_level = group_row['access_level'] if group_row else 0
        
        expires_str = "Нет лицензии"
        if group_row:
             if group_row['expires_at'].year > 3000:
                 expires_str = "Навсегда"
             else:
                 expires_str = group_row['expires_at'].strftime("%d.%m.%Y")

        # 2. Получаем доступные продукты
        all_products = await conn.fetch("SELECT * FROM products WHERE is_available = TRUE ORDER BY id ASC")
        
        allowed_products = []
        for p in all_products:
            # Сравниваем уровень доступа группы с требуемым уровнем продукта
            required_level = p.get('required_access_level', 1) 
            # Если колонки required_access_level нет в БД, считаем её равной 1 (Basic)
            
            if access_level >= required_level:
                allowed_products.append({
                    "id": p['id'],
                    "name": p['name'],
                    "description": p['description'],
                    "image_url": p['image_url'],
                    "download_url": f"/api/download/{p['id']}", # Генерируем ссылку на скачивание
                    "version": p['version']
                })

    return {
        "uid": str(user['uid']),
        "username": user['username'],
        "email": user['email'],
        "group_name": group_name,
        "group_slug": slug,
        "expires": expires_str,
        "available_products": allowed_products
    }

