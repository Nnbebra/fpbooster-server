from fastapi import Request, HTTPException, Depends
from fastapi.responses import RedirectResponse
from auth.jwt_utils import decode_jwt
from auth.guards import get_current_user # Импортируем получение юзера по токену

# ==========================================================
# 1. ЗАЩИТА UI (АДМИНКА В БРАУЗЕРЕ) - Работает через Куки
# ==========================================================
async def admin_guard_ui(request: Request):
    """
    Проверяет наличие куки 'admin_auth'.
    Используется для HTML-страниц админки.
    """
    token = request.cookies.get("admin_auth")
    
    # Если токена нет - редирект на логин
    if not token:
        raise HTTPException(status_code=307, headers={"Location": "/admin/login"})
    
    # Проверяем совпадение с токеном в конфиге сервера
    if not hasattr(request.app.state, "ADMIN_TOKEN"):
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN not configured")
        
    if token != request.app.state.ADMIN_TOKEN:
         raise HTTPException(status_code=307, headers={"Location": "/admin/login"})
         
    return True

# Алиас для удобства (чтобы старый код не ломался)
ui_guard = admin_guard_ui


# ==========================================================
# 2. ЗАЩИТА API (ЗАПРОСЫ ОТ КОДА) - Работает через Bearer Token
# ==========================================================
async def admin_guard_api(request: Request, user = Depends(get_current_user)):
    """
    Проверяет, есть ли у пользователя из токена права админа.
    Используется в groups_router.py и других API эндпоинтах.
    """
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        # Ищем, есть ли у пользователя АКТИВНАЯ группа с флагом is_admin_group=TRUE
        is_admin = await conn.fetchval("""
            SELECT COUNT(*) 
            FROM user_groups ug
            JOIN groups g ON ug.group_id = g.id
            WHERE ug.user_uid = $1 
              AND ug.is_active = TRUE 
              AND g.is_admin_group = TRUE
        """, user['uid'])
        
        if is_admin > 0:
            return True
            
    # Если прав нет
    raise HTTPException(status_code=403, detail="Admin Access Required")
