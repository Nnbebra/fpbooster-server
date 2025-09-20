from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# ====== Список промокодов ======
@router.get("/admin/promocodes", response_class=HTMLResponse)
async def list_promocodes(request: Request):
    if request.cookies.get("admin_auth") != request.app.state.ADMIN_TOKEN:
        return RedirectResponse("/admin/login")
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
              code,
              owner,
              discount
            FROM promocodes
            ORDER BY code ASC
            """
        )
    return templates.TemplateResponse(
        "promocodes.html",
        {"request": request, "rows": rows}
    )

# ====== Создание ======
@router.get("/admin/promocodes/new", response_class=HTMLResponse)
async def new_promocode_form(request: Request):
    if request.cookies.get("admin_auth") != request.app.state.ADMIN_TOKEN:
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse(
        "promo_form.html",
        {"request": request, "row": None, "error": None}
    )

@router.post("/admin/promocodes/new")
async def create_promocode(request: Request,
                           code: str = Form(...),
                           owner: str = Form(...),
                           discount: int = Form(...)):
    if request.cookies.get("admin_auth") != request.app.state.ADMIN_TOKEN:
        return RedirectResponse("/admin/login")
    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO promocodes (code, owner, discount)
            VALUES ($1, $2, $3)
            ON CONFLICT (code) DO UPDATE
            SET owner=EXCLUDED.owner,
                discount=EXCLUDED.discount
            """,
            code.strip(), owner.strip(), discount
        )
    return RedirectResponse("/admin/promocodes", status_code=302)

# ====== Редактирование ======
@router.get("/admin/promocodes/edit/{code}", response_class=HTMLResponse)
async def edit_promocode_form(request: Request, code: str):
    if request.cookies.get("admin_auth") != request.app.state.ADMIN_TOKEN:
        return RedirectResponse("/admin/login")
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM promocodes WHERE code=$1", code)
    if not row:
        return RedirectResponse("/admin/promocodes")
    return templates.TemplateResponse(
        "promo_form.html",
        {"request": request, "row": row, "error": None}
    )

@router.post("/admin/promocodes/edit/{code}")
async def edit_promocode(request: Request, code: str,
                         owner: str = Form(...),
                         discount: int = Form(...)):
    if request.cookies.get("admin_auth") != request.app.state.ADMIN_TOKEN:
        return RedirectResponse("/admin/login")
    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            "UPDATE promocodes SET owner=$1, discount=$2 WHERE code=$3",
            owner.strip(), discount, code
        )
    return RedirectResponse("/admin/promocodes", status_code=302)

# ====== Удаление ======
@router.get("/admin/promocodes/delete/{code}")
async def delete_promocode(request: Request, code: str):
    if request.cookies.get("admin_auth") != request.app.state.ADMIN_TOKEN:
        return RedirectResponse("/admin/login")
    async with request.app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM promocodes WHERE code=$1", code)
    return RedirectResponse("/admin/promocodes", status_code=302)


@router.post("/api/promocode/use")
async def use_promocode(request: Request,
                        license_key: str = Form(...),
                        code: str = Form(...)):
    async with request.app.state.pool.acquire() as conn:
        # Проверяем лицензию
        lic = await conn.fetchrow("SELECT * FROM licenses WHERE license_key=$1", license_key)
        if not lic:
            return {"ok": False, "error": "Лицензия не найдена"}

        # Проверяем, не использовался ли уже промокод
        if lic.get("promocode_used"):
            return {"ok": False, "error": "Промокод уже был использован"}

        # Проверяем промокод
        promo = await conn.fetchrow("SELECT * FROM promocodes WHERE code=$1", code)
        if not promo:
            return {"ok": False, "error": "Промокод не найден"}

        # Применяем бонус (например, +30 дней)
        await conn.execute(
            "UPDATE licenses SET expires = COALESCE(expires, CURRENT_DATE) + interval '30 days', promocode_used=$1 WHERE license_key=$2",
            code, license_key
        )

        # Обновляем статистику промокода
        await conn.execute(
            "UPDATE promocodes SET uses = COALESCE(uses,0)+1, last_used=NOW() WHERE code=$1",
            code
        )

    return {"ok": True, "message": "Промокод успешно применён"}


