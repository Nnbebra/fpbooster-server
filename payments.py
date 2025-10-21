# payments.py
import os
import hashlib
from datetime import date, timedelta

import httpx
from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .buy import PLANS

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# ===== Конфиг PayPalych =====
SHOP_ID = os.getenv("PAYPALYCH_SHOP_ID")
API_TOKEN = os.getenv("PAYPALYCH_TOKEN")
API_URL = "https://paypalych.com/api/create"


def make_sign(shop_id: str, amount: int, order_id: str, token: str) -> str:
    """
    Генерация подписи для PayPalych.
    Формула: md5(f"{shop_id}:{amount}:{order_id}:{token}")
    """
    sign_str = f"{shop_id}:{amount}:{order_id}:{token}"
    return hashlib.md5(sign_str.encode()).hexdigest()


# ===== Success / Fail страницы =====

@router.get("/payment/success", response_class=HTMLResponse)
async def payment_success(request: Request):
    return templates.TemplateResponse("payment_success.html", {"request": request})


@router.get("/payment/fail", response_class=HTMLResponse)
async def payment_fail(request: Request):
    return templates.TemplateResponse("payment_fail.html", {"request": request})


# ===== Старт оплаты =====

@router.get("/payment/start")
async def payment_start(request: Request, plan: str = Query(...), method: str = Query("card")):
    plan_data = PLANS.get(plan)
    if not plan_data:
        return {"ok": False, "error": "Неверный тариф"}

    amount = plan_data["price"]
    order_id = f"order_{plan}_{request.client.host}"

    sign = make_sign(SHOP_ID, amount, order_id, API_TOKEN)

    payload = {
        "shop_id": SHOP_ID,
        "amount": amount,
        "currency": "RUB",
        "order_id": order_id,
        "method": method,
        "desc": f"Покупка {plan_data['title']}",
        "success_url": "https://fpbooster.shop/payment/success",
        "fail_url": "https://fpbooster.shop/payment/fail",
        "result_url": "https://fpbooster.shop/payment/result",
        "sign": sign,
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(API_URL, data=payload)
        r.raise_for_status()
        data = r.json()

    if data.get("ok") and "pay_url" in data:
        return RedirectResponse(url=data["pay_url"])
    else:
        return {"ok": False, "error": data.get("error", "Неизвестная ошибка")}


# ===== Callback-и от платёжки =====

@router.post("/payment/result")
async def payment_result(request: Request):
    """
    Callback от PayPalych:
    - принимает uid и plan (30/90/365)
    - активирует или продлевает лицензию
    """
    data = await request.form()
    uid = data.get("uid")
    plan = data.get("plan")  # 30 / 90 / 365

    days = {"30": 30, "90": 90, "365": 365}.get(plan, 30)

    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT expires FROM licenses WHERE user_uid=$1", uid
        )

        if row and row["expires"] and row["expires"] > date.today():
            # продлеваем от текущей даты окончания
            new_expires = row["expires"] + timedelta(days=days)
        else:
            # новая активация
            new_expires = date.today() + timedelta(days=days)

        await conn.execute(
            """
            UPDATE licenses
            SET status='active', expires=$1
            WHERE user_uid=$2
            """,
            new_expires,
            uid,
        )

    return {"ok": True, "uid": uid, "plan": plan, "expires": str(new_expires)}


@router.post("/payment/refund")
async def payment_refund(request: Request):
    """
    Callback: возврат — помечает лицензию как expired
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
    Callback: чарджбэк — помечает лицензию как banned
    """
    data = await request.form()
    uid = data.get("uid")

    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            "UPDATE licenses SET status='banned' WHERE user_uid=$1",
            uid,
        )

    return {"ok": True, "uid": uid, "status": "banned"}
