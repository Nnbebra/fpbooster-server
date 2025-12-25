from fastapi import Request, HTTPException
from .jwt_utils import decode_jwt

async def get_current_user(request: Request):
    """
    Универсальная проверка авторизации:
    1. Ищет токен в куках (для браузера/сайта)
    2. Ищет токен в заголовке Authorization (для лаунчера)
    """
    # 1. Проверяем Куки (Сайт)
    token = request.cookies.get("user_auth")
    
    # 2. Проверяем Заголовок (Лаунчер), если в куках пусто
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

    # Если токена нет совсем
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        # Декодируем JWT
        data = decode_jwt(token)
        user_id = int(data.get("sub"))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Достаем пул соединений напрямую из state приложения
    pool = request.app.state.pool
    
    async with pool.acquire() as conn:
        # Выбираем все нужные поля, включая uid и группу
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
