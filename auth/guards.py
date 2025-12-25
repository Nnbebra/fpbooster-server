from fastapi import Request, HTTPException
from .jwt_utils import decode_jwt

async def get_current_user(request: Request, **kwargs):
    """
    Универсальная проверка авторизации:
    1. Ищет токен в куках (сайт)
    2. Ищет токен в заголовке Authorization (лаунчер и софт)
    
    **kwargs добавлена для предотвращения ошибки "takes 1 positional argument but 2 were given"
    при вложенных вызовах Depends.
    """
    token = None

    # 1. Пробуем взять из Кук (приоритет для сайта)
    token = request.cookies.get("user_auth")

    # 2. Если в куках пусто, проверяем заголовок Authorization
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header:
            if auth_header.startswith("Bearer "):
                parts = auth_header.split(" ")
                if len(parts) > 1:
                    token = parts[1]
            else:
                # Поддержка старого софта или прямого токена
                # Проверяем, что это похоже на JWT (состоит из 3 частей через точку)
                if auth_header.count(".") == 2:
                    token = auth_header

    # Очистка токена от мусорных строк из JS/Лаунчера
    if not token or str(token).lower() in ["null", "undefined", "none", ""]:
        raise HTTPException(status_code=401, detail="Missing Token")

    try:
        # Декодируем JWT
        data = decode_jwt(token)
        # Убеждаемся, что sub существует
        sub = data.get("sub")
        if not sub:
            raise ValueError("No sub in token")
        
        # Преобразуем sub в ID (целое число)
        user_id = int(sub)
    except Exception as e:
        # Если токен невалидный или sub не число
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

    # Получаем доступ к БД через state приложения
    pool = request.app.state.pool
    
    async with pool.acquire() as conn:
        # ВАЖНО: оставляем алиас user_group as "group", так как плагины ищут u['group']
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
