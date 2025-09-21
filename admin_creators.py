# admin_creators.py — Part 1/3

from fastapi import APIRouter, Request, Form, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import bcrypt
import random
import string

from guards import admin_guard_ui  # общий guard, читает куку admin_auth

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Обёртка для Depends: берём актуальный токен из app.state
def guard(request: Request):
    return admin_guard_ui(request, request.app.state.ADMIN_TOKEN)

# ================= Портал контент‑мейкера =================

@router.get("/creators/login", response_class=HTMLResponse)
async def creator_login_form(request: Request):
    return templates.TemplateResponse("creator_login.html", {"request": request, "error": None})

@router.post("/creators/login")
async def creator_login(request: Request, nickname: str = Form(...), password: str = Form(...)):
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, nickname, password_hash FROM content_creators WHERE nickname=$1",
            nickname.strip(),
        )
    if not row or not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        return templates.TemplateResponse(
            "creator_login.html",
            {"request": request, "error": "Неверные данные"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
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
        data = await conn.fetchrow(
            """
            SELECT c.nickname,
                   c.promo_code,
                   c.commission_percent,
                   p.uses,
                   p.last_used,
                   p.discount,
                   p.bonus_days
            FROM content_creators c
            LEFT JOIN promocodes p ON p.code = c.promo_code
            WHERE c.id = $1
            """,
            int(cid),
        )
    if not data:
        return RedirectResponse("/creators/logout")
    return templates.TemplateResponse("creator_dashboard.html", {"request": request, "data": data})
# admin_creators.py — Part 2/3

# ================= Админка: список и создание =================

@router.get("/admin/creators", response_class=HTMLResponse)
async def list_creators(request: Request, _=Depends(guard)):
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, nickname, promo_code, commission_percent, created_at,
                   youtube, tiktok, telegram
            FROM content_creators
            ORDER BY created_at DESC
            """
        )
    return templates.TemplateResponse("creators_list.html", {"request": request, "rows": rows})


@router.get("/admin/creators/new", response_class=HTMLResponse)
async def new_creator_form(request: Request, _=Depends(guard)):
    return templates.TemplateResponse("creator_form.html", {"request": request, "creator": None})


def generate_promo_code(length=8):
    """Генерация случайного промокода из букв и цифр"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


@router.post("/admin/creators/new")
async def create_creator(
    request: Request,
    nickname: str = Form(...),
    password: str = Form(...),
    promo_code: str = Form(None),
    commission_percent: int = Form(0),
    youtube: str = Form(""),
    tiktok: str = Form(""),
    telegram: str = Form(""),
    _=Depends(guard),
):
    nickname = (nickname or "").strip()
    promo_code_clean = (promo_code or "").strip() or None

    if not nickname or not password:
        return templates.TemplateResponse(
            "creator_form.html",
            {"request": request, "creator": None, "error": "Никнейм и пароль обязательны"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    async with request.app.state.pool.acquire() as conn:
        # Если промокод не указан — генерируем новый
        if not promo_code_clean:
            while True:
                new_code = generate_promo_code()
                exists = await conn.fetchval("SELECT 1 FROM promocodes WHERE code=$1", new_code)
                if not exists:
                    promo_code_clean = new_code
                    # создаём промокод в таблице promocodes
                    await conn.execute(
                        "INSERT INTO promocodes (code, active) VALUES ($1, TRUE)",
                        promo_code_clean,
                    )
                    break
        else:
            # Если промокод указан — проверяем, что он существует
            exists = await conn.fetchval("SELECT 1 FROM promocodes WHERE code=$1", promo_code_clean)
            if not exists:
                return templates.TemplateResponse(
                    "creator_form.html",
                    {"request": request, "creator": None, "error": "Такого промокода не существует"},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        # Создаём автора
        await conn.execute(
            """
            INSERT INTO content_creators
                (nickname, password_hash, promo_code, commission_percent, youtube, tiktok, telegram)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            nickname,
            pw_hash,
            promo_code_clean,
            int(commission_percent or 0),
            (youtube or "").strip() or None,
            (tiktok or "").strip() or None,
            (telegram or "").strip() or None,
        )

    return RedirectResponse("/admin/creators", status_code=status.HTTP_303_SEE_OTHER)

# admin_creators.py — Part 3/3

# ================= Админка: редактирование и удаление =================

@router.post("/admin/creators/edit/{id}")
async def edit_creator(
    request: Request,
    id: int,
    nickname: str = Form(...),
    password: str = Form(None),
    promo_code: str = Form(None),
    commission_percent: int = Form(0),
    youtube: str = Form(""),
    tiktok: str = Form(""),
    telegram: str = Form(""),
    _=Depends(guard),
):
    nickname = (nickname or "").strip()
    promo_code_clean = (promo_code or "").strip() or None
    commission_percent = int(commission_percent or 0)

    async with request.app.state.pool.acquire() as conn:
        # Проверка промокода, если он указан
        if promo_code_clean:
            exists = await conn.fetchval("SELECT 1 FROM promocodes WHERE code=$1", promo_code_clean)
            if not exists:
                row = await conn.fetchrow(
                    """
                    SELECT id, nickname, promo_code, commission_percent,
                           youtube, tiktok, telegram
                    FROM content_creators
                    WHERE id=$1
                    """,
                    id,
                )
                return templates.TemplateResponse(
                    "creator_form.html",
                    {"request": request, "creator": row, "error": "Такого промокода не существует"},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        # Если пароль введён — обновляем и его
        if password:
            pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            await conn.execute(
                """
                UPDATE content_creators
                SET nickname=$1,
                    password_hash=$2,
                    promo_code=$3,
                    commission_percent=$4,
                    youtube=$5,
                    tiktok=$6,
                    telegram=$7
                WHERE id=$8
                """,
                nickname,
                pw_hash,
                promo_code_clean,
                commission_percent,
                (youtube or "").strip() or None,
                (tiktok or "").strip() or None,
                (telegram or "").strip() or None,
                id,
            )
        else:
            # Если пароль пустой — не трогаем password_hash
            await conn.execute(
                """
                UPDATE content_creators
                SET nickname=$1,
                    promo_code=$2,
                    commission_percent=$3,
                    youtube=$4,
                    tiktok=$5,
                    telegram=$6
                WHERE id=$7
                """,
                nickname,
                promo_code_clean,
                commission_percent,
                (youtube or "").strip() or None,
                (tiktok or "").strip() or None,
                (telegram or "").strip() or None,
                id,
            )

    return RedirectResponse("/admin/creators", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/creators/edit/{id}")
async def edit_creator(
    request: Request,
    id: int,
    nickname: str = Form(...),
    password: str = Form(None),
    promo_code: str = Form(None),
    commission_percent: int = Form(0),
    _=Depends(guard),
):
    nickname = (nickname or "").strip()
    promo_code = (promo_code or "").strip() or None
    commission_percent = int(commission_percent or 0)

    async with request.app.state.pool.acquire() as conn:
        if password:
            pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            await conn.execute(
                """
                UPDATE content_creators
                SET nickname=$1,
                    password_hash=$2,
                    promo_code=$3,
                    commission_percent=$4
                WHERE id=$5
                """,
                nickname,
                pw_hash,
                promo_code,
                commission_percent,
                id,
            )
        else:
            await conn.execute(
                """
                UPDATE content_creators
                SET nickname=$1,
                    promo_code=$2,
                    commission_percent=$3
                WHERE id=$4
                """,
                nickname,
                promo_code,
                commission_percent,
                id,
            )

    return RedirectResponse("/admin/creators", status_code=status.HTTP_303_SEE_OTHER)

@router.get("/admin/creators/delete/{id}")
async def delete_creator(request: Request, id: int, _=Depends(guard)):
    async with request.app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM content_creators WHERE id=$1", id)
    return RedirectResponse("/admin/creators", status_code=status.HTTP_303_SEE_OTHER)







