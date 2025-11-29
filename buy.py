from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")
router = APIRouter()

# Структура тарифов:
# available=False -> покажет табличку "Скоро в продаже"
PLANS = {
    # === СТАНДАРТНАЯ ВЕРСИЯ (Репрайсинг) ===
    "30": {
        "id": "30",
        "title": "Лицензия на 30 дней",
        "old_price": 299,
        "price": 199,
        "discount": "-33%",
        "img": "/static/products/30days.png",
        "available": True,
        "type": "license",
        "days": 30
    },
    "90": {
        "id": "90",
        "title": "Лицензия на 90 дней",
        "old_price": 749,
        "price": 579,
        "discount": "-22%",
        "img": "/static/products/90days.png",
        "available": True,
        "type": "license",
        "days": 90
    },
    "365": {
        "id": "365",
        "title": "Лицензия на 365 дней",
        "old_price": 2399,
        "price": 1699,
        "discount": "-30%",
        "img": "/static/products/365days.png",
        "available": True,
        "type": "license",
        "days": 365
    },

    # === FPBooster Alpha (Пока недоступно) ===
    "alpha_30": {
        "id": "alpha_30",
        "title": "FPBooster Alpha (30 дней)",
        "old_price": 450,
        "price": 350,
        "discount": None,
        "img": "/static/Alpha30.png",
        "available": False, # Недоступно
        "type": "license_alpha",
        "days": 30
    },
    "alpha_90": {
        "id": "alpha_90",
        "title": "FPBooster Alpha (90 дней)",
        "old_price": 1100,
        "price": 899,
        "discount": "-18%",
        "img": "/static/Alpha90.png",
        "available": False, # Недоступно
        "type": "license_alpha",
        "days": 90
    },
    "alpha_365": {
        "id": "alpha_365",
        "title": "FPBooster Alpha (365 дней)",
        "old_price": 3200,
        "price": 2699,
        "discount": "-15%",
        "img": "/static/Alpha365.png",
        "available": False, # Недоступно
        "type": "license_alpha",
        "days": 365
    },

    # === FPBooster+ (Навсегда, пока недоступно) ===
    "plus_lifetime": {
        "id": "plus_lifetime",
        "title": "FPBooster+ (Навсегда)",
        "old_price": 500,
        "price": 299,
        "discount": "HOT",
        "img": "/static/FPBooster+.png",
        "available": False, # Недоступно
        "type": "license_plus",
        "days": 36500 # 100 лет
    },

    # === Услуги ===
    "hwid_reset": {
        "id": "hwid_reset",
        "title": "Сброс HWID",
        "old_price": None,
        "price": 149,
        "discount": None,
        "img": "/static/products/hwid_reset.png", # Убедись, что картинка есть, или замени
        "available": True,
        "type": "service",
        "days": 0
    },
}

@router.get("/buy", response_class=HTMLResponse)
async def buy_page(request: Request):
    # Передаем список тарифов в шаблон
    return templates.TemplateResponse("buy.html", {"request": request, "plans": PLANS.values()})

@router.get("/checkout/{plan_id}", response_class=HTMLResponse)
async def checkout_page(request: Request, plan_id: str):
    plan = PLANS.get(plan_id)
    
    if not plan:
        raise HTTPException(status_code=404, detail="Тариф не найден")
    
    # Если пытаются купить по прямой ссылке недоступный товар
    if not plan.get("available", True):
         # Можно либо кидать ошибку, либо редиректить назад. Пока оставим ошибку.
         raise HTTPException(status_code=403, detail="Этот товар пока недоступен для покупки")

    return templates.TemplateResponse("checkout.html", {"request": request, "plan": plan})
