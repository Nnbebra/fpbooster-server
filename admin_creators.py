# admin_creators.py

from fastapi import APIRouter, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import bcrypt

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def admin_guard_ui(request: Request):
    """
    Проверяет cookie admin_auth против ADMIN_TOKEN.
    Если не совпадает, перенаправляет на /admin/login.
    """
    if request.cookies.get("admin_auth") != request.app.state.ADMIN_TOKEN:
        return RedirectResponse("/admin/login")
    return True


@router.get("/admin/creators", response_class=HTMLResponse)
async def list_creators(request: Request):
    admin_guard_ui(request)
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id,
                   nickname,
                   promo_code,
                   commission_percent,
                   created_at
            FROM content_creators
            ORDER BY created_at DESC
            """
        )
    return templates.TemplateResponse(
        "creators_list.html",
        {"request": request, "rows": rows}
    )


@router.get("/admin/creators/new", response_class=HTMLResponse)
async def new_creator_form(request: Request):
    admin_guard_ui(request)
    return templates.TemplateResponse(
        "creator_form.html",
        {"request": request, "row": None, "error": None}
    )


@router.post("/admin/creators/new")
async def create_creator(
    request: Request,
    nickname: str        = Form(...),
    password: str        = Form(...),
    promo_code: str      = Form(None),
    commission_percent: int = Form(0),
):
    admin_guard_ui(request)
    nickname = nickname.strip()
    if not nickname or not password:
        return templates.TemplateResponse(
            "creator_form.html",
            {
                "request": request,
                "row": None,
                "error": "Никнейм и пароль обязательны"
            },
            status_code=status.HTTP_400_BAD_REQUEST
        )

    # Хешируем пароль
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO content_creators
                (nickname, password_hash, promo_code, commission_percent)
            VALUES ($1, $2, $3, $4)
            """,
            nickname, pw_hash, promo_code or None, commission_percent
        )

    return RedirectResponse("/admin/creators", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/creators/edit/{id}", response_class=HTMLResponse)
async def edit_creator_form(request: Request, id: int):
    admin_guard_ui(request)
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id,
                   nickname,
                   promo_code,
                   commission_percent
            FROM content_creators
            WHERE id=$1
            """,
            id
        )
    if not row:
        return RedirectResponse("/admin/creators", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        "creator_form.html",
        {"request": request, "row": row, "error": None}
    )


@router.post("/admin/creators/edit/{id}")
async def edit_creator(
    request: Request,
    id: int,
    nickname: str        = Form(...),
    password: str        = Form(None),
    promo_code: str      = Form(None),
    commission_percent: int = Form(0),
):
    admin_guard_ui(request)
    nickname = nickname.strip()

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
                nickname, pw_hash, promo_code or None, commission_percent, id
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
                nickname, promo_code or None, commission_percent, id
            )

    return RedirectResponse("/admin/creators", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/creators/delete/{id}")
async def delete_creator(request: Request, id: int):
    admin_guard_ui(request)
    async with request.app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM content_creators WHERE id=$1", id)
    return RedirectResponse("/admin/creators", status_code=status.HTTP_303_SEE_OTHER)
