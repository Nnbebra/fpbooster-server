# guards.py
from fastapi import Request, HTTPException

def admin_guard_ui(request: Request, admin_token: str):
    if request.cookies.get("admin_auth") != admin_token:
        raise HTTPException(
            status_code=302,
            detail="Redirect",
            headers={"Location": "/admin/login"}
        )
    return True
