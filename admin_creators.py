from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER
from guards import guard
from server import templates

router = APIRouter()


@router.get("/admin/creators", response_class=HTMLResponse)
async def list_creators(request: Request, _=Depends(guard)):
    """Список всех авторов"""
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, nickname, youtube, tiktok, telegram
            FROM content_creators
            ORDER BY id
            """
        )
    return templates.TemplateResponse(
        "creators_list.html", {"request": request, "creators": rows}
    )


@router.get("/admin/creators/{creator_id}", response_class=HTMLResponse)
async def edit_creator(request: Request, creator_id: int, _=Depends(guard)):
    """Форма редактирования автора"""
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, nickname, youtube, tiktok, telegram
            FROM content_creators
            WHERE id=$1
            """,
            creator_id,
        )
    if not row:
        return RedirectResponse("/admin/creators", status_code=HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        "creator_form.html", {"request": request, "creator": row}
    )


@router.post("/admin/creators/{creator_id}")
async def update_creator(
    creator_id: int,
    nickname: str = Form(...),
    youtube: str = Form(""),
    tiktok: str = Form(""),
    telegram: str = Form(""),
    _=Depends(guard),
):
    """Сохранение изменений автора"""
    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE content_creators
            SET nickname=$1,
                youtube=$2,
                tiktok=$3,
                telegram=$4
            WHERE id=$5
            """,
            nickname,
            youtube,
            tiktok,
            telegram,
            creator_id,
        )
    return RedirectResponse("/admin/creators", status_code=HTTP_303_SEE_OTHER)


@router.get("/admin/creators/new", response_class=HTMLResponse)
async def new_creator_form(request: Request, _=Depends(guard)):
    """Форма создания нового автора"""
    return templates.TemplateResponse(
        "creator_form.html",
        {"request": request, "creator": {"id": None, "nickname": "", "youtube": "", "tiktok": "", "telegram": ""}},
    )


@router.post("/admin/creators/new")
async def create_creator(
    nickname: str = Form(...),
    youtube: str = Form(""),
    tiktok: str = Form(""),
    telegram: str = Form(""),
    _=Depends(guard),
):
    """Создание нового автора"""
    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO content_creators (nickname, youtube, tiktok, telegram)
            VALUES ($1, $2, $3, $4)
            """,
            nickname,
            youtube,
            tiktok,
            telegram,
        )
    return RedirectResponse("/admin/creators", status_code=HTTP_303_SEE_OTHER)


@router.get("/admin/creators/{creator_id}/delete")
async def delete_creator(request: Request, creator_id: int, _=Depends(guard)):
    """Удаление автора"""
    async with request.app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM content_creators WHERE id=$1", creator_id)
    return RedirectResponse("/admin/creators", status_code=HTTP_303_SEE_OTHER)
