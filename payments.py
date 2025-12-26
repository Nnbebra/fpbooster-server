# payments.py
import os
import hashlib
import time
from datetime import date, timedelta, datetime

import httpx
from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# Импортируем PLANS
from buy import PLANS
from auth.guards import get_current_user

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# ===== Конфиг PayPalych (pal24.pro) =====
SHOP_ID = os.getenv("PAYPALYCH_SHOP_ID")
API_TOKEN = os.getenv("PAYPALYCH_TOKEN")
BILL_CREATE_URL = "https://pal24.pro/api/v1/bill/create"

def md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()

def verify_signature(out_sum: str, inv_id: str, signature_value: str, api_token: str) -> bool:
    expected = md5_upper(f"{out_sum}:{inv_id}:{api_token}")
    return expected == (signature_value or "").upper()


# ===== Success / Fail страницы =====

@router.post("/payment/success", response_class=HTMLResponse)
async def payment_success(request: Request):
    form = await request.form()
    inv_id = form.get("InvId", "")
    out_sum = form.get("OutSum", "")
    currency_in = form.get("CurrencyIn", "")
    signature = form.get("SignatureValue", "")
    custom = form.get("custom", "")

    if not verify_signature(out_sum, inv_id, signature, API_TOKEN):
        raise HTTPException(status_code=400, detail="Invalid signature")

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
    form = await request.form()
    inv_id = form.get("InvId", "")
    out_sum = form.get("OutSum", "")
    currency_in = form.get("CurrencyIn", "")
    signature = form.get("SignatureValue", "")
    custom = form.get("custom", "")

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


# ===== Старт оплаты =====

@router.get("/payment/start")
async def payment_start(request: Request, plan: str = Query(...), method: str = Query("card")):
    plan_data = PLANS.get(plan)
    if not plan_data:
        return {"ok": False, "error": "Неверный тариф"}

    try:
        user = await get_current_user(request)
    except Exception:
        user = None

    uid = (user.uid if (user and getattr(user, "uid", None)) else None)

    amount = plan_data["price"]
    order_id = f"order_{plan}_{uid or 'anon'}_{request.client.host}_{int(time.time())}"
    custom = f"uid:{uid or 'anon'}|plan:{plan}"

    payload = {
        "amount": amount,
        "order_id": order_id,
        "description": f"Покупка {plan_data['title']}",
        "type": "normal",
        "shop_id": SHOP_ID,
        "currency_in": "RUB",
        "custom": custom,
        "name": "Платёж FPBooster",
    }

    headers = {"Authorization": f"Bearer {API_TOKEN}"}

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(BILL_CREATE_URL, headers=headers, data=payload)
        if r.status_code == 401:
            return {"ok": False, "error": "Ошибка авторизации в PayPalych (401)"}
        r.raise_for_status()
        data = r.json()

    link_page_url = data.get("link_page_url")
    success = str(data.get("success", "")).lower() == "true"

    if success and link_page_url:
        return RedirectResponse(url=link_page_url, status_code=302)
    else:
        return {"ok": False, "error": data}


# ===== Postback: Обработка результата =====

@router.post("/payment/result")
async def payment_result(request: Request):
    """
    Основная логика выдачи товара.
    1. Проверяет подпись.
    2. Определяет тариф и группу.
    3. Выдает лицензию И группу в одной транзакции.
    """
    data = await request.form()

    status = (data.get("Status") or "").upper()
    inv_id = data.get("InvId", "")
    out_sum = data.get("OutSum", "")
    currency_in = data.get("CurrencyIn", "")
    signature = data.get("SignatureValue", "")
    custom = data.get("custom", "")

    # 1. Проверка подписи
    if not verify_signature(out_sum, inv_id, signature, API_TOKEN):
        raise HTTPException(status_code=400, detail="Invalid signature")

    # 2. Парсинг custom
    uid = None
    plan = None
    try:
        parts = dict(p.split(":", 1) for p in custom.split("|") if ":" in p)
        uid = parts.get("uid")
        plan = parts.get("plan")
    except Exception:
        pass

    plan_data = PLANS.get(plan)
    if not plan_data:
        # Если план не найден, но оплата прошла - логируем ошибку (или просто OK, чтобы шлюз не долбил)
        return {"ok": True, "status": "UNKNOWN_PLAN"}

    days = plan_data.get("days", 30)
    group_slug = plan_data.get("group_slug") # Получаем slug группы из нового конфига

    # 3. Обработка успешной оплаты
    if status == "SUCCESS" and uid:
        async with request.app.state.pool.acquire() as conn:
            async with conn.transaction():
                
                # --- A. СБРОС HWID ---
                if plan == "hwid_reset":
                    await conn.execute("UPDATE licenses SET hwid = NULL WHERE user_uid=$1", uid)
                    new_expires = "HWID Reset"

                # --- B. ВЫДАЧА ЛИЦЕНЗИИ И ГРУППЫ ---
                else:
                    # 1. Расчет новой даты окончания
                    # Смотрим текущую лицензию в таблице licenses (она главная для лаунчера)
                    row = await conn.fetchrow("SELECT expires FROM licenses WHERE user_uid=$1", uid)
                    
                    today = date.today()
                    current_expires = row["expires"] if row else None
                    
                    # Логика продления: если лицензия активна и не истекла, добавляем к ней. Иначе - от сегодня.
                    if current_expires and current_expires > today:
                        new_expires_date = current_expires + timedelta(days=days)
                    else:
                        new_expires_date = today + timedelta(days=days)
                    
                    # Обработка "Вечной" лицензии (36500 дней)
                    if days > 10000:
                         # Если покупаем вечную, то ставим вечную дату, игнорируя старую
                         new_expires_date = today + timedelta(days=36500)

                    # 2. Обновляем таблицу LICENSES (для лаунчера)
                    # Определяем ключ лицензии на основе типа плана (маппинг типов)
                    license_type_map = {
                        "license": "Default",
                        "license_alpha": "Alpha",
                        "license_plus": "Plus"
                    }
                    license_key_val = license_type_map.get(plan_data.get("type"), "Default")

                    existing_lic = await conn.fetchrow("SELECT user_uid FROM licenses WHERE user_uid=$1", uid)
                    if existing_lic:
                        await conn.execute(
                            """
                            UPDATE licenses
                            SET status='active', expires=$1, license_key=$2
                            WHERE user_uid=$3
                            """,
                            new_expires_date, license_key_val, uid
                        )
                    else:
                        # Если вдруг записи нет (новый юзер), нужно получить username
                        user_info = await conn.fetchrow("SELECT username FROM users WHERE uid=$1", uid)
                        u_name = user_info["username"] if user_info else "Unknown"
                        await conn.execute(
                            """
                            INSERT INTO licenses (user_uid, user_name, status, expires, created_at, duration_days, license_key)
                            VALUES ($1, $2, 'active', $3, NOW(), $4, $5)
                            """,
                            uid, u_name, new_expires_date, days, license_key_val
                        )

                    # 3. Обновляем таблицу USER_GROUPS (Новая система!)
                    if group_slug:
                        # Получаем ID группы
                        group_row = await conn.fetchrow("SELECT id FROM groups WHERE slug=$1", group_slug)
                        if group_row:
                            group_id = group_row["id"]
                            
                            # Конвертируем date в datetime для user_groups (там timestamp)
                            expires_ts = datetime(
                                new_expires_date.year, 
                                new_expires_date.month, 
                                new_expires_date.day, 
                                23, 59, 59
                            )
                            
                            # Upsert в user_groups
                            await conn.execute(
                                """
                                INSERT INTO user_groups (user_uid, group_id, granted_at, expires_at, is_active, granted_by)
                                VALUES ($1, $2, NOW(), $3, TRUE, NULL)
                                ON CONFLICT (user_uid, group_id) 
                                DO UPDATE SET expires_at = $3, is_active = TRUE, granted_at = NOW()
                                """,
                                uid, group_id, expires_ts
                            )

                    new_expires = str(new_expires_date)

                # --- C. ЛОГИРОВАНИЕ ПОКУПКИ ---
                await conn.execute(
                    """
                    INSERT INTO purchases (user_uid, plan, amount, currency, source)
                    VALUES ($1, $2, $3, $4, 'payment')
                    """,
                    uid, plan, out_sum, currency_in
                )

        return {
            "ok": True,
            "status": "SUCCESS",
            "uid": uid,
            "plan": plan,
            "expires": new_expires,
            "inv_id": inv_id
        }

    elif status == "FAIL":
        return {"ok": False, "status": "FAIL", "inv_id": inv_id}

    return {"ok": False, "status": status or "UNKNOWN", "inv_id": inv_id}
