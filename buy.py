from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from auth.guards import get_current_user

templates = Jinja2Templates(directory="templates")
router = APIRouter()

# === КОНФИГУРАЦИЯ ТАРИФОВ ===
# Мы добавили поле 'group_slug', чтобы связать тарифы с новой системой групп.
# payment_router будет читать это поле при успешной оплате.

PLANS = {
    # === СТАНДАРТНАЯ ВЕРСИЯ (FPBooster Basic) ===
    "30": {
        "id": "30",
        "title": "Лицензия на 30 дней",
        "old_price": 299,
        "price": 199,
        "discount": "-33%",
        "img": "/static/products/30days.png",
        "available": True,
        "type": "license",
        "days": 30,
        "group_slug": "basic", # <--- СВЯЗЬ С ГРУППОЙ
        "desc": "Полный доступ ко всем основным функциям FPBooster: авто-restock, авто-поднятие, копирование чужих лотов..."
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
        "days": 90,
        "group_slug": "basic", # <--- СВЯЗЬ С ГРУППОЙ
        "desc": "Выгодный вариант для постоянных продавцов. Включает все основные функции на 3 месяца."
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
        "days": 365,
        "group_slug": "basic", # <--- СВЯЗЬ С ГРУППОЙ
        "desc": "Максимальная выгода. Год полного доступа ко всему основному функционалу без ограничений + FPBooster+ в подарок."
    },

    # === FPBooster Alpha (FPBooster Alpha Access) ===
    "alpha_30": {
        "id": "alpha_30",
        "title": "FPBooster Alpha (30 дней)",
        "old_price": 450,
        "price": 350,
        "discount": None,
        "img": "/static/Alpha30.png",
        "available": False, 
        "type": "license_alpha",
        "days": 30,
        "group_slug": "alpha", # <--- СВЯЗЬ С ГРУППОЙ
        "desc": "Доступ к 20+ доп. функциям, автоматизация через сервер, дополнительные темы и эксклюзивный визуал."
    },
    "alpha_90": {
        "id": "alpha_90",
        "title": "FPBooster Alpha (90 дней)",
        "old_price": 1100,
        "price": 899,
        "discount": "-18%",
        "img": "/static/Alpha90.png",
        "available": False, 
        "type": "license_alpha",
        "days": 90,
        "group_slug": "alpha", # <--- СВЯЗЬ С ГРУППОЙ
        "desc": "Доступ к 20+ доп. функциям, автоматизация через сервер, дополнительные темы и эксклюзивный визуал."
    },
    "alpha_365": {
        "id": "alpha_365",
        "title": "FPBooster Alpha (365 дней)",
        "old_price": 3200,
        "price": 2699,
        "discount": "-15%",
        "img": "/static/Alpha365.png",
        "available": False, 
        "type": "license_alpha",
        "days": 365,
        "group_slug": "alpha", # <--- СВЯЗЬ С ГРУППОЙ
        "desc": "Доступ к 20+ доп. функциям, автоматизация через сервер, дополнительные темы и эксклюзивный визуал."
    },

    # === FPBooster+ (FPBooster Plus) ===
    "plus_lifetime": {
        "id": "plus_lifetime",
        "title": "FPBooster+ (Навсегда)",
        "old_price": 500,
        "price": 299,
        "discount": "HOT",
        "img": "/static/FPBooster+.png",
        "available": False, 
        "type": "license_plus",
        "days": 36500, # 100 лет
        "group_slug": "plus", # <--- СВЯЗЬ С ГРУППОЙ
        "desc": "Дополнение к лицензии. Позволяет автоматизировать некоторые процессы через сервер и даёт доп. темы."
    },

    # === Услуги (Без группы) ===
    "hwid_reset": {
        "id": "hwid_reset",
        "title": "Сброс HWID",
        "old_price": None,
        "price": 149,
        "discount": None,
        "img": "/static/hwid_reset.png", 
        "available": True,
        "type": "service",
        "days": 0,
        "group_slug": None, # Услуга не выдает группу
        "desc": "Сброс привязки к железу (HWID) для запуска софта на новом компьютере."
    },
}

@router.get("/buy", response_class=HTMLResponse)
async def buy_page(request: Request):
    user = None
    try:
        # Корректное получение пользователя для шапки сайта
        user = await get_current_user(request)
    except Exception:
        user = None
        
    return templates.TemplateResponse("buy.html", {
        "request": request, 
        "plans": PLANS.values(),
        "user": user 
    })

@router.get("/checkout/{plan_id}", response_class=HTMLResponse)
async def checkout_page(request: Request, plan_id: str):
    # 1. Проверяем авторизацию (строгая проверка для покупки)
    user = None
    try:
        user = await get_current_user(request)
        if not user:
            raise Exception("No user")
    except Exception:
        # Если не авторизован -> редирект на логин с возвратом обратно
        return RedirectResponse(url=f"/login?next=/checkout/{plan_id}", status_code=302)

    # 2. Ищем тариф
    plan = PLANS.get(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Тариф не найден")
    
    # 3. Проверка доступности тарифа
    if not plan.get("available", True):
         raise HTTPException(status_code=403, detail="Этот товар пока недоступен для покупки")

    # 4. Рендер страницы
    return templates.TemplateResponse("checkout.html", {
        "request": request, 
        "plan": plan, 
        "user": user 
    })
