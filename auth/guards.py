from fastapi import Request, HTTPException
from .jwt_utils import decode_jwt

async def get_current_user(app, request: Request):
    # 1. Проверяем Куки (Сайт)
    token = request.cookies.get("user_auth")
    
    # 2. Проверяем Заголовок (Лаунчер)
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        data = decode_jwt(token)
        user_id = int(data.get("sub"))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    async with app.state.pool.acquire() as conn:
        # ВАЖНО: Тут выбираем uid и user_group, которые есть в твоей БД
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
