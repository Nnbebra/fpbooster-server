# auth/guards.py
from fastapi import Request, HTTPException
from .jwt_utils import decode_jwt

async def get_current_user(request: Request, app=None):
    """
    Универсальная версия:
    - Если app передан явно (как в server.py: get_current_user(request.app, request)),
      то используется он.
    - Если app не передан (как при Depends), берём request.app.
    """
    if app is None:
        app = request.app

    token = request.cookies.get("user_auth")
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        data = decode_jwt(token)
        user_id = int(data.get("sub"))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    async with app.state.pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id, uid, email, username, user_group, email_confirmed, created_at, last_login
            FROM users
            WHERE id=$1
            """,
            user_id
        )

    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user
