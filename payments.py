# payments.py
import os
import hashlib
import time
from datetime import date, timedelta

import httpx
from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from buy import PLANS
from auth.guards import get_current_user

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# ===== Конфиг PayPalych (pal24.pro) =====
SHOP_ID = os.getenv("PAYPALYCH_SHOP_ID")  # пример: "1g7WZ4k2Wz"
API_TOKEN = os.getenv("PAYPALYCH_TOKEN")  # пример: "24960|E6mYfLC07N..."
BILL_CREATE_URL = "https://pal24.pro/api/v1/bill/create"

def md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()

def verify_signature(out_sum: str, inv_id: str, signature_value: str, api_token: str) -> bool:
    expected = md5_upper(f"{out_sum}:{inv_id}:{api_token}")
    return expected == (signature_value or "").upper()


# ===== Success / Fail страницы (POST редирект с подписью) =====

@router.post("/payment/success", response_class=HTMLResponse)
async def payment_success(request: Request):
    """
    PayPalych делает POST-редирект сюда после успешной оплаты:
    fields: InvId, OutSum, CurrencyIn, custom (optional), SignatureValue
    """
    form = await request.form()
    inv_id = form.get("InvId", "")
    out_sum = form.get("OutSum", "")
    currency_in = form.get("CurrencyIn", "")
    signature = form.get("SignatureValue", "")
    custom = form.get("custom", "")

    # Проверка подписи
    if not verify_signature(out_sum, inv_id, signature, API_TOKEN):
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Можно показать пользователю подтверждение с номером заказа
    return templates.TemplateResponse(
        "payment_success.html",
        {
            "request": request,
            "inv_id": inv_id,
            "amount": out_sum,
            "currency": currency_in,
            "custom": custom,
        },
    )


@router.post("/payment/fail", response_class=HTMLResponse)
async def payment_fail(request: Request):
    """
    PayPalych делает POST-редирект сюда после неуспешной оплаты:
    fields: InvId, OutSum, CurrencyIn, custom (optional), SignatureValue
    """
    form = await request.form()
    inv_id = form.get("InvId", "")
    out_sum = form.get("OutSum", "")
    currency_in = form.get("CurrencyIn", "")
    signature = form.get("SignatureValue", "")
    custom = form.get("custom", "")

    # Проверка подписи
    if not verify_signature(out_sum, inv_id, signature, API_TOKEN):
        raise HTTPException(status_code=400, detail="Invalid signature")

    return templates.TemplateResponse(
        "payment_fail.html",
        {
            "request": request,
            "inv_id": inv_id,
            "amount": out_sum,
            "currency": currency_in,
            "custom": custom,
        },
    )


# ===== Старт оплаты: создаём счёт и редиректим на страницу оплаты =====

@router.get("/payment/start")
async def payment_start(request: Request, plan: str = Query(...), method: str = Query("card")):
    """
    Создаёт счёт через pal24.pro и редиректит пользователя на link_page_url.
    - Сумма берётся из PLANS[plan]["price"].
    - order_id уникален (plan + uid + ip + timestamp).
    - В custom передаём uid и план для дальнейшей привязки.
    """
    plan_data = PLANS.get(plan)
    if not plan_data:
        return {"ok": False, "error": "Неверный тариф"}

    # Текущий пользователь (если авторизован)
    try:
        user = await get_current_user(request.app, request)
    except Exception:
        user = None

    uid = (user.uid if (user and getattr(user, "uid", None)) else None)

    amount = plan_data["price"]
    # Уникальный идентификатор заказа. InvId вернётся в Success/Fail/Postback.
    order_id = f"order_{plan}_{uid or 'anon'}_{request.client.host}_{int(time.time())}"

    # custom: безопасная привязка данных, чтобы на postback восстановить контекст
    # формат: "uid:<uid>|plan:<plan>"
    custom = f"uid:{uid or 'anon'}|plan:{plan}"

    payload = {
        "amount": amount,
        "order_id": order_id,
        "description": f"Покупка {plan_data['title']}",
        "type": "normal",
        "shop_id": SHOP_ID,
        "currency_in": "RUB",
        "custom": custom,
        "payer_pays_commission": 1,  # по желанию: включает комиссию плательщику
        "name": "Платёж FPBooster",
        # method в pal24 необязателен; оставляем для будущей логики, если понадобится
    }

    headers = {"Authorization": f"Bearer {API_TOKEN}"}

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(BILL_CREATE_URL, headers=headers, data=payload)
        if r.status_code == 401:
            return {"ok": False, "error": "Ошибка авторизации в PayPalych (401)"}
        r.raise_for_status()
        data = r.json()

    # ожидаем success="true" и link_page_url
    link_page_url = data.get("link_page_url")
    success = str(data.get("success", "")).lower() == "true"

    if success and link_page_url:
        return RedirectResponse(url=link_page_url, status_code=302)
    else:
        return {"ok": False, "error": data}


# ===== Postback: смена статуса заказа в нашей системе =====

@router.post("/payment/result")
async def payment_result(request: Request):
    """
    Postback от PayPalych на Result URL (после оплаты):
    {
      "Status": "SUCCESS" | "FAIL",
      "InvId": "...",
      "OutSum": "18.54",
      "CurrencyIn": "RUB",
      "custom": "uid:<...>|plan:<...>",
      "SignatureValue": "<MD5_UPPER(OutSum:InvId:apiToken)>"
    }
    Логика:
    - Проверяем подпись.
    - Если SUCCESS: активируем/продлеваем лицензию и логируем покупку в purchases.
    - Если FAIL: просто возвращаем FAIL (по желанию можно логировать).
    """
    data = await request.form()

    status = (data.get("Status") or "").upper()
    inv_id = data.get("InvId", "")
    out_sum = data.get("OutSum", "")
    currency_in = data.get("CurrencyIn", "")
    signature = data.get("SignatureValue", "")
    custom = data.get("custom", "")

    # 1) Проверка подписи (обязательна)
    if not verify_signature(out_sum, inv_id, signature, API_TOKEN):
        raise HTTPException(status_code=400, detail="Invalid signature")

    # 2) Разбор custom: uid и plan
    uid = None
    plan = None
    try:
        parts = dict(p.split(":", 1) for p in custom.split("|") if ":" in p)
        uid = parts.get("uid")
        plan = parts.get("plan")
    except Exception:
        uid = None
        plan = None

    # 3) Приведение плана к дням
    days_map = {"30": 30, "90": 90, "365": 365}
    days = days_map.get(plan or "", 30)

    # 4) Смена статуса в нашей БД и логирование покупки
    if status == "SUCCESS" and uid:
        async with request.app.state.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT expires FROM licenses WHERE user_uid=$1",
                    uid,
                )

                today = date.today()
                if row and row["expires"] and row["expires"] > today:
                    new_expires = row["expires"] + timedelta(days=days)
                else:
                    new_expires = today + timedelta(days=days)

                # Обновляем лицензию
                await conn.execute(
                    """
                    UPDATE licenses
                    SET status='active', expires=$1
                    WHERE user_uid=$2
                    """,
                    new_expires,
                    uid,
                )

                # Логируем покупку в purchases (источник: payment)
                # Приводим сумму к NUMERIC через ::numeric на стороне SQL при необходимости
                await conn.execute(
                    """
                    INSERT INTO purchases (user_uid, plan, amount, currency, source)
                    VALUES ($1, $2, $3, $4, 'payment')
                    """,
                    uid,
                    plan,
                    out_sum,       # строка; таблица purchases принимает NUMERIC(10,2) — Postgres приведёт сам при вставке
                    currency_in,
                )

        return {
            "ok": True,
            "status": "SUCCESS",
            "uid": uid,
            "plan": plan,
            "expires": str(new_expires),
            "inv_id": inv_id,
            "amount": out_sum,
            "currency": currency_in,
        }

    elif status == "FAIL":
        return {"ok": False, "status": "FAIL", "inv_id": inv_id}

    return {"ok": False, "status": status or "UNKNOWN", "inv_id": inv_id}
