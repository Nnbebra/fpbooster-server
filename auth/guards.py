# auth/guards.py
from fastapi import Request, HTTPException, Depends
from .jwt_utils import decode_jwt

async def get_current_user(request: Request):
    """
    Универсальная защита:
    1. Сначала ищет токен в заголовке Authorization (для Лаунчера).
    2. Если нет — ищет в Cookie 'user_auth' (для Сайта).
    """
    token = None
    
    # 1. Проверка заголовка (Лаунчер)
    auth_header = request.headers.get("Authorization")
    if auth_header:
        parts = auth_header.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]

    # 2. Проверка куки (Сайт), если токен еще не найден
    if not token:
        token = request.cookies.get("user_auth")

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized: No token provided")

    # 3. Расшифровка
    try:
        data = decode_jwt(token)
        if not data:
            raise HTTPException(status_code=401, detail="Invalid token structure")
        user_id = int(data.get("sub"))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    # 4. Поиск в базе
    # Важно: request.app.state.pool доступен везде в FastAPI
    try:
        pool = getattr(request.app.state, 'pool', None) or getattr(request.app.state, 'db_pool', None)
        if not pool:
            # Если пул не готов (редкий случай), кидаем 500
            raise HTTPException(status_code=500, detail="Database pool not ready")

        async with pool.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT id, uid, email, username, user_group, email_confirmed, created_at, last_login FROM users WHERE id=$1",
                user_id
            )
            
        if not user:
            raise HTTPException(status_code=401, detail="User not found in DB")
            
        return user # Возвращаем объект Record (ведет себя как словарь)

    except Exception as e:
        print(f"Auth DB Error: {e}")
        raise HTTPException(status_code=401, detail="Authentication failed during DB check")
