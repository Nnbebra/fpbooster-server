from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
import asyncpg

router = APIRouter()
templates = Jinja2Templates(directory="templates")


# ===== API для проверки промокода (клиент) =====
@router.get("/api/promocode")
async def check_promocode(code: str, request: Request):
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT code, owner, discount, uses, last_used
            FROM promocodes
            WHERE code = $1
            """,
            code.strip().upper(),
        )
        if not row:
            return {"valid": False}

        # Обновляем uses и last_used
        await conn.execute(
            """
            UPDATE promocodes
            SET uses = uses + 1, last_used = NOW()
            WHERE code = $1
            """,
            code.strip().upper(),
        )

        return {
            "valid": True,
            "code": row["code"],
            "owner": row["owner"],
            "discount": row["discount"],
            "uses": row["uses"] + 1,
            "last_used": datetime.utcnow().isoformat(),
        }


# ===== Админка: список промокодов =====
@router.get("/admin/promocodes", response_class=HTMLResponse)
async def admin_promocodes(request: Request):
    if not request.cookies.get("admin_auth") == request.app.state.ADMIN_TOKEN:
        return RedirectResponse(url="/admin/login", status_code=302)

    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT code, owner, discount, uses, last_used
            FROM promocodes
            ORDER BY uses DESC
            """
        )
    return templates.TemplateResponse(
        "promocodes.html",
        {"request": request, "rows": rows},
    )


# ===== Админка: форма создания =====
@router.get("/admin/promocodes/new", response_class=HTMLResponse)
async def new_promocode_form(request: Request):
    if not request.cookies.get("admin_auth") == request.app.state.ADMIN_TOKEN:
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse(
        "promo_form.html",
        {"request": request, "error": None},
    )


@router.post("/admin/promocodes/new")
async def create_promocode(
    request: Request,
    code: str = Form(...),
    owner: str = Form(...),
    discount: int = Form(14),
):
    if not request.cookies.get("admin_auth") == request.app.state.ADMIN_TOKEN:
        return RedirectResponse(url="/admin/login", status_code=302)

    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO promocodes (code, owner, discount, uses)
            VALUES ($1, $2, $3, 0)
            ON CONFLICT (code) DO NOTHING
            """,
            code.strip().upper(),
            owner.strip(),
            discount,
        )
    return RedirectResponse(url="/admin/promocodes", status_code=302)
