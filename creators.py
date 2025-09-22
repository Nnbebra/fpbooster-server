# creators.py
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import bcrypt

router = APIRouter()
templates = Jinja2Templates(directory="templates")

@router.get("/creators/login", response_class=HTMLResponse)
async def creator_login_form(request: Request):
    return templates.TemplateResponse("creator_login.html", {"request": request, "error": None})

@router.post("/creators/login")
async def creator_login(request: Request, nickname: str = Form(...), password: str = Form(...)):
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, nickname, password_hash FROM content_creators WHERE nickname=$1",
            (nickname or "").strip()
        )
    if not row or not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        return templates.TemplateResponse("creator_login.html", {"request": request, "error": "Неверные данные"})
    resp = RedirectResponse(url="/creators/dashboard", status_code=302)
    resp.set_cookie("creator_auth", str(row["id"]), httponly=True, samesite="lax")
    return resp

@router.get("/creators/logout")
async def creator_logout():
    resp = RedirectResponse(url="/creators/login", status_code=302)
    resp.delete_cookie("creator_auth")
    return resp

@router.get("/creators/dashboard", response_class=HTMLResponse)
async def creator_dashboard(request: Request):
    cid = request.cookies.get("creator_auth")
    if not cid:
        return RedirectResponse("/creators/login")
    async with request.app.state.pool.acquire() as conn:
        data = await conn.fetchrow("""
            SELECT c.nickname,
                   c.promo_code,
                   c.commission_percent,
                   c.youtube,
                   c.tiktok,
                   c.telegram,
                   p.uses,
                   p.last_used,
                   p.discount,
                   p.bonus_days
            FROM content_creators c
            LEFT JOIN promocodes p ON p.code = c.promo_code
            WHERE c.id = $1
        """, int(cid))
    if not data:
        return RedirectResponse("/creators/logout")
    return templates.TemplateResponse("creator_dashboard.html", {"request": request, "data": data})

# Обновление соцсетей прямо с дашборда
@router.post("/creators/dashboard")
async def update_creator_dashboard(
    request: Request,
    youtube: str = Form(""),
    tiktok: str = Form(""),
    telegram: str = Form(""),
):
    cid = request.cookies.get("creator_auth")
    if not cid:
        return RedirectResponse("/creators/login")

    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE content_creators
            SET youtube=$1, tiktok=$2, telegram=$3
            WHERE id=$4
            """,
            (youtube or "").strip() or None,
            (tiktok or "").strip() or None,
            (telegram or "").strip() or None,
            int(cid),
        )

    return RedirectResponse("/creators/dashboard", status_code=303)
