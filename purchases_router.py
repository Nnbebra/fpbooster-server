# purchases_router.py
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# Import the raw guard and wrap it for Depends
from auth.guards import get_current_user as get_current_user_raw

router = APIRouter()
templates = Jinja2Templates(directory="templates")

async def current_user(request: Request):
    # wrapper so Depends doesn't look for "app" param
    return await get_current_user_raw(request)

@router.get("/purchases", response_class=HTMLResponse)
async def purchases_page(request: Request, user=Depends(current_user)):
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, plan, amount, currency, source, token_code, created_at
            FROM purchases
            WHERE user_uid = $1
            ORDER BY created_at DESC
            """,
            user["uid"],
        )
    return templates.TemplateResponse(
        "purchases.html",
        {"request": request, "user": user, "purchases": rows}
    )

