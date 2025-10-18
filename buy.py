# buy.py
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")
router = APIRouter()

@router.get("/buy", response_class=HTMLResponse)
async def buy_page(request: Request):
    # В будущем можно подгружать тарифы из БД
    plans = [
        {"id": "30", "title": "Лицензия на 30 дней", "old_price": 350, "price": 299, "img": "/static/products/30days.png"},
        {"id": "365", "title": "Лицензия на 365 дней", "old_price": 3500, "price": 2499, "img": "/static/products/365days.png"},
        {"id": "lifetime", "title": "Лицензия навсегда", "old_price": 6000, "price": 3999, "img": "/static/products/lifetime.png"},
    ]
    return templates.TemplateResponse("buy.html", {"request": request, "plans": plans})
