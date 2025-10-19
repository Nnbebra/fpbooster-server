# payments.py
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="templates")

@router.get("/payment/success", response_class=HTMLResponse)
async def payment_success(request: Request):
    return templates.TemplateResponse("payment_success.html", {"request": request})

@router.get("/payment/fail", response_class=HTMLResponse)
async def payment_fail(request: Request):
    return templates.TemplateResponse("payment_fail.html", {"request": request})

@router.post("/payment/result")
async def payment_result(request: Request):
    data = await request.form()
    # TODO: проверить подпись, обновить статус заказа в БД
    return JSONResponse({"ok": True})

@router.post("/payment/refund")
async def payment_refund(request: Request):
    data = await request.form()
    # TODO: обработка возврата
    return JSONResponse({"ok": True})

@router.post("/payment/chargeback")
async def payment_chargeback(request: Request):
    data = await request.form()
    # TODO: обработка чарджбэка
    return JSONResponse({"ok": True})
