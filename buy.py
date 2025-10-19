from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")
router = APIRouter()

# Тарифы: 30, 90, 365 — без "навсегда"
PLANS = {
    "30": {
        "id": "30",
        "title": "Лицензия на 30 дней",
        "old_price": 350,
        "price": 299,
        "discount": "-15%",
        "img": "/static/products/30days.png",
    },
    "90": {
        "id": "90",
        "title": "Лицензия на 90 дней",
        "old_price": 900,
        "price": 749,
        "discount": "-20%",
        "img": "/static/products/90days.png",
    },
    "365": {
        "id": "365",
        "title": "Лицензия на 365 дней",
        "old_price": 3500,
        "price": 2399,
        "discount": "-30%",
        "img": "/static/products/365days.png",
    },
}

@router.get("/buy", response_class=HTMLResponse)
async def buy_page(request: Request):
    return templates.TemplateResponse("buy.html", {"request": request, "plans": PLANS.values()})

@router.get("/checkout/{plan_id}", response_class=HTMLResponse)
async def checkout_page(request: Request, plan_id: str):
    plan = PLANS.get(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Тариф не найден")
    return templates.TemplateResponse("checkout.html", {"request": request, "plan": plan})
