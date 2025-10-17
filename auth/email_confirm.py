# auth/email_confirm.py
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime

router = APIRouter()
templates = Jinja2Templates(directory="templates")

@router.get("/confirm", response_class=HTMLResponse)
async def confirm_email(request: Request, token: str):
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, expires, used FROM email_confirmations WHERE token=$1",
            token
        )
        if not row:
            raise HTTPException(status_code=404, detail="Токен не найден")

        if row["used"]:
            return templates.TemplateResponse(
                "email_confirm.html",
                {"request": request, "ok": False, "msg": "Токен уже использован"}
            )

        # ✅ Исправлено: сравнение с datetime.utcnow()
        if row["expires"] < datetime.utcnow():
            return templates.TemplateResponse(
                "email_confirm.html",
                {"request": request, "ok": False, "msg": "Токен истёк"}
            )

        # Подтверждаем email
        await conn.execute("UPDATE users SET email_confirmed=TRUE WHERE id=$1", row["user_id"])
        await conn.execute("UPDATE email_confirmations SET used=TRUE WHERE token=$1", token)

    return templates.TemplateResponse(
        "email_confirm.html",
        {"request": request, "ok": True, "msg": "Email подтверждён"}
    )
