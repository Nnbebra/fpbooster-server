from fastapi import Request, HTTPException
from .jwt_utils import decode_jwt

async def get_current_user(request: Request):
    token = request.cookies.get("user_auth")
    
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header:
            # Поддержка 'Bearer <token>'
            if auth_header.startswith("Bearer "):
                token = auth_header.split(" ")[1]
            # Поддержка просто '<token>' (для старого софта)
            else:
                token = auth_header

    if not token:
        raise HTTPException(status_code=401, detail="Missing Token")

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

