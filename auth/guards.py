from fastapi import Request, HTTPException, status
from .jwt_utils import decode_jwt

async def get_current_user(request: Request):
    # 1. Сначала ищем токен в КУКАХ (для Сайта)
    token = request.cookies.get("user_auth")
    
    # 2. Если нет, ищем в ЗАГОЛОВКЕ Authorization (для Лаунчера/API)
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

    if not token:
        # Вызываем ошибку, которую поймает роутер и сделает редирект
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = decode_jwt(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        user_id = int(payload["sub"])
    except:
        raise HTTPException(status_code=401, detail="Invalid token data")
    
    # Достаем пул соединений из app.state
    # request.app доступен внутри request, поэтому передавать app отдельно не нужно
    async with request.app.state.pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE id=$1", user_id)
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
            
    return user
