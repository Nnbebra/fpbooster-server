from fastapi import Request, HTTPException
from .jwt_utils import decode_jwt

async def get_current_user(request: Request):
    """
    Безопасная проверка авторизации с защитой от падения сервера.
    """
    token = None

    # 1. Пробуем взять из Кук (сайт)
    token = request.cookies.get("user_auth")

    # 2. Если нет, берем из заголовка (софт)
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header:
            if auth_header.startswith("Bearer "):
                parts = auth_header.split(" ")
                if len(parts) > 1:
                    token = parts[1]
            elif auth_header.count(".") == 2:
                token = auth_header

    # Проверка на пустой токен
    if not token or str(token).lower() in ["null", "undefined", "none", ""]:
        raise HTTPException(status_code=401, detail="Missing Token")

    # Валидация токена
    try:
        data = decode_jwt(token)
        sub = data.get("sub")
        if not sub:
            raise ValueError("No sub in token")
        user_id = int(sub)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

    # --- ЗАЩИТА ОТ ОШИБКИ 500 ---
    if not hasattr(request.app.state, "pool") or request.app.state.pool is None:
        # Если это произойдет, в логе софта будет написано "Database error", а не просто 500
        print("CRITICAL ERROR: Database pool is not initialized!")
        raise HTTPException(status_code=500, detail="Database connection not initialized")

    pool = request.app.state.pool
    
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id, email, username, email_confirmed, created_at, last_login, 
                   uid, user_group as "group"
            FROM users 
            WHERE id=$1
            """,
            user_id
        )

    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    
    return user
