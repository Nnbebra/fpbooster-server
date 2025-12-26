from fastapi import Request, HTTPException
from .jwt_utils import decode_jwt

async def get_current_user(request: Request):
    """
    Универсальная проверка авторизации (Сайт + Лаунчер + Софт).
    """
    token = None

    # 1. Сначала проверяем Куки (для сайта)
    token = request.cookies.get("user_auth")

    # 2. Если в куках пусто, проверяем заголовок Authorization (для софта/лаунчера)
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header:
            if auth_header.startswith("Bearer "):
                parts = auth_header.split(" ")
                if len(parts) > 1:
                    token = parts[1]
            else:
                # Поддержка прямого токена (если софт шлет без Bearer)
                if auth_header.count(".") == 2:
                    token = auth_header

    # Если токен — это строка "null"/"undefined" или он отсутствует
    if not token or str(token).lower() in ["null", "undefined", "none", ""]:
        raise HTTPException(status_code=401, detail="Missing Token")

    try:
        # Декодируем JWT
        data = decode_jwt(token)
        sub = data.get("sub")
        if not sub:
            raise ValueError("No sub in token")
        
        user_id = int(sub)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

    # Получаем пул соединений из состояния приложения
    pool = request.app.state.pool
    
    async with pool.acquire() as conn:
        # Алиас user_group as "group" критически важен для плагинов
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
