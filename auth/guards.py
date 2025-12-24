from fastapi import Request, HTTPException
from .jwt_utils import decode_jwt

# Оставляем сигнатуру (app, request) как у тебя было, чтобы ничего не сломать
async def get_current_user(app, request: Request):
    # 1. Сначала ищем в куках (для сайта)
    token = request.cookies.get("user_auth")
    
    # 2. ДОБАВЛЕНО: Если нет в куках, ищем в заголовке (для Лаунчера)
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
        user = await conn.fetchrow(
            "SELECT id, email, username, email_confirmed, created_at, last_login, group_name as \"group\", uid FROM users WHERE id=$1",
            user_id
        )
        # Примечание: в SQL запросе выше я добавил group_name и uid, 
        # так как они нужны для профиля лаунчера. Если у тебя столбцы называются иначе - поправь.
        # Обычно это: SELECT *, либо конкретные поля.
        # Если была ошибка "column does not exist", верни как было:
        # "SELECT id, email, username, email_confirmed, created_at, last_login FROM users WHERE id=$1"

    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    
    return user
