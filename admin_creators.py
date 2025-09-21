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
            "SELECT id, nickname, social_links FROM content_creators ORDER BY id"
        )
    return templates.TemplateResponse(
        "creators_list.html", {"request": request, "creators": rows}
    )


@router.get("/admin/creators/{creator_id}", response_class=HTMLResponse)
async def edit_creator(request: Request, creator_id: int, _=Depends(guard)):
    """Форма редактирования автора"""
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, nickname, social_links FROM content_creators WHERE id=$1",
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
    social_links: str = Form(""),
    _=Depends(guard),
):
    """Сохранение изменений автора"""
    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            "UPDATE content_creators SET nickname=$1, social_links=$2 WHERE id=$3",
            nickname,
            social_links,
            creator_id,
        )
    return RedirectResponse("/admin/creators", status_code=HTTP_303_SEE_OTHER)
