# referrals.py — Part 1/3

from fastapi import APIRouter, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi import Depends
from guards import admin_guard_ui


router = APIRouter()
templates = Jinja2Templates(directory="templates")


def guard(request: Request):
    return admin_guard_ui(request, request.app.state.ADMIN_TOKEN)

@router.get("/admin/promocodes", response_class=HTMLResponse)
async def list_promocodes(request: Request, _=Depends(guard)):
    ...



@router.get("/admin/promocodes", response_class=HTMLResponse)
async def list_promocodes(request: Request):
    if request.cookies.get("admin_auth") != request.app.state.ADMIN_TOKEN:
        return RedirectResponse("/admin/login")
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT code,
                   owner,
                   discount,
                   bonus_days,
                   uses,
                   last_used
            FROM promocodes
            ORDER BY code ASC
            """
        )
    return templates.TemplateResponse(
        "promocodes.html",
        {"request": request, "rows": rows}
    )


@router.get("/admin/promocodes/new", response_class=HTMLResponse)
async def new_promocode_form(request: Request):
    if request.cookies.get("admin_auth") != request.app.state.ADMIN_TOKEN:
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse(
        "promo_form.html",
        {"request": request, "row": None, "error": None}
    )


@router.post("/admin/promocodes/new")
async def create_promocode(
    request: Request,
    code: str       = Form(...),
    owner: str      = Form(...),
    discount: int   = Form(...),
    bonus_days: int = Form(0),
):
    if request.cookies.get("admin_auth") != request.app.state.ADMIN_TOKEN:
        return RedirectResponse("/admin/login")
    code = code.strip()
    owner = owner.strip()
    if not code:
        return templates.TemplateResponse(
            "promo_form.html",
            {"request": request, "row": None, "error": "Код не может быть пустым"},
            status_code=status.HTTP_400_BAD_REQUEST
        )
    async with request.app.state.pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM promocodes WHERE code=$1", code
        )
        if exists:
            return templates.TemplateResponse(
                "promo_form.html",
                {"request": request, "row": None, "error": "Такой код уже существует"},
                status_code=status.HTTP_400_BAD_REQUEST
            )
        await conn.execute(
            """
            INSERT INTO promocodes
                (code, owner, discount, bonus_days, uses, last_used)
            VALUES ($1, $2, $3, $4, 0, NULL)
            """,
            code, owner, discount, bonus_days
        )
    return RedirectResponse("/admin/promocodes", status_code=status.HTTP_303_SEE_OTHER)
# referrals.py — Part 2/3

@router.get("/admin/promocodes/edit/{code}", response_class=HTMLResponse)
async def edit_promocode_form(request: Request, code: str):
    if request.cookies.get("admin_auth") != request.app.state.ADMIN_TOKEN:
        return RedirectResponse("/admin/login")
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT code,
                   owner,
                   discount,
                   bonus_days,
                   uses,
                   last_used
            FROM promocodes
            WHERE code=$1
            """,
            code
        )
    if not row:
        return RedirectResponse("/admin/promocodes", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        "promo_form.html",
        {"request": request, "row": row, "error": None}
    )


@router.post("/admin/promocodes/edit/{code}")
async def edit_promocode(
    request: Request,
    code: str,
    owner: str      = Form(...),
    discount: int   = Form(...),
    bonus_days: int = Form(0),
):
    if request.cookies.get("admin_auth") != request.app.state.ADMIN_TOKEN:
        return RedirectResponse("/admin/login")
    owner = owner.strip()
    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE promocodes
            SET owner=$1,
                discount=$2,
                bonus_days=$3
            WHERE code=$4
            """,
            owner, discount, bonus_days, code
        )
    return RedirectResponse("/admin/promocodes", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/promocodes/delete/{code}")
async def delete_promocode(request: Request, code: str):
    if request.cookies.get("admin_auth") != request.app.state.ADMIN_TOKEN:
        return RedirectResponse("/admin/login")
    async with request.app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM promocodes WHERE code=$1", code)
    return RedirectResponse("/admin/promocodes", status_code=status.HTTP_303_SEE_OTHER)
# referrals.py — Part 3/3

@router.post("/api/promocode/use")
async def api_use_promocode(
    request: Request,
    license_key: str = Form(...),
    code: str        = Form(...)
):
    try:
        async with request.app.state.pool.acquire() as conn:
            # 1) Проверяем лицензию
            lic = await conn.fetchrow(
                "SELECT license_key, expires, promocode_used FROM licenses WHERE license_key=$1",
                license_key
            )
            if not lic:
                return JSONResponse({"ok": False, "error": "Лицензия не найдена"})

            if lic["promocode_used"]:
                return JSONResponse({"ok": False, "error": "Промокод уже был использован"})

            # 2) Проверяем промокод
            promo = await conn.fetchrow(
                "SELECT code, discount, bonus_days FROM promocodes WHERE code=$1",
                code
            )
            if not promo:
                return JSONResponse({"ok": False, "error": "Промокод не найден"})

            # 3) Берём bonus_days для продления
            days_to_add = int(promo["bonus_days"] or 0)

            # 4) Обновляем лицензию: если есть дни — прибавляем, иначе только помечаем usage
            if days_to_add > 0:
                await conn.execute(
                    """
                    UPDATE licenses
                    SET
                      expires = (
                        COALESCE((expires)::timestamp, NOW()) + ($1 || ' days')::interval
                      )::date,
                      promocode_used = $2
                    WHERE license_key = $3
                    """,
                    str(days_to_add), code, license_key
                )
            else:
                await conn.execute(
                    "UPDATE licenses SET promocode_used=$1 WHERE license_key=$2",
                    code, license_key
                )

            # 5) Обновляем статистику промокода
            await conn.execute(
                """
                UPDATE promocodes
                SET
                  uses = COALESCE(uses, 0) + 1,
                  last_used = NOW()
                WHERE code=$1
                """,
                code
            )

        msg = "Промокод применён"
        if days_to_add > 0:
            msg += f". Лицензия продлена на {days_to_add} дней"
        return JSONResponse({"ok": True, "message": msg})

    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Ошибка сервера при применении промокода: {e}"})



from fastapi import Depends
from server import admin_guard_ui  # импортируем guard

@router.get("/admin/creators", response_class=HTMLResponse)
async def list_creators(request: Request, _=Depends(admin_guard_ui)):
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM content_creators ORDER BY created_at DESC")
    return templates.TemplateResponse("creators_list.html", {"request": request, "rows": rows})



@router.get("/admin/creators", response_class=HTMLResponse)
async def list_creators(request: Request, _=Depends(guard)):
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM content_creators ORDER BY created_at DESC")
    return templates.TemplateResponse("creators_list.html", {"request": request, "rows": rows})





