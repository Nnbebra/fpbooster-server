# payments.py
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from datetime import date, timedelta

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# ===== Success / Fail страницы для редиректа пользователя =====

@router.get("/payment/success", response_class=HTMLResponse)
async def payment_success(request: Request):
    return templates.TemplateResponse("payment_success.html", {"request": request})

@router.get("/payment/fail", response_class=HTMLResponse)
async def payment_fail(request: Request):
    return templates.TemplateResponse("payment_fail.html", {"request": request})

# ===== Callback-и от платёжки =====

@router.post("/payment/result")
async def payment_result(request: Request):
    """
    Заглушка с тестовой логикой:
    - принимает uid и plan (30/90/365)
    - активирует лицензию для указанного UID
    - выставляет срок действия
    """
    data = await request.form()
    uid = data.get("uid")
    plan = data.get("plan")  # 30 / 90 / 365

    days = {"30": 30, "90": 90, "365": 365}.get(plan, 30)
    expires = date.today() + timedelta(days=days)

    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE licenses
            SET status='active', expires=$1
            WHERE user_uid=$2
            """,
            expires,
            uid,
        )

    return {"ok": True, "uid": uid, "plan": plan, "expires": str(expires)}

@router.post("/payment/refund")
async def payment_refund(request: Request):
    """
    Заглушка: помечает лицензию как expired
    """
    data = await request.form()
    uid = data.get("uid")

    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            "UPDATE licenses SET status='expired' WHERE user_uid=$1",
            uid,
        )

    return {"ok": True, "uid": uid, "status": "expired"}

@router.post("/payment/chargeback")
async def payment_chargeback(request: Request):
    """
    Заглушка: помечает лицензию как banned
    """
    data = await request.form()
    uid = data.get("uid")

    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            "UPDATE licenses SET status='banned' WHERE user_uid=$1",
            uid,
        )

    return {"ok": True, "uid": uid, "status": "banned"}
