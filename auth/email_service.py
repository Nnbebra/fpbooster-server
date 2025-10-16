# auth/email_service.py
import os, secrets
from datetime import datetime, timedelta

CONFIRM_TTL_HOURS = int(os.getenv("CONFIRM_TTL_HOURS", "24"))

async def create_token(app, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=CONFIRM_TTL_HOURS)
    async with app.state.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO email_confirmations (user_id, token, expires) VALUES ($1, $2, $3)",
            user_id, token, expires
        )
    return token

async def send_email(app, to_email: str, subject: str, html_body: str):
    # Можем реализовать два варианта:
    # 1) SMTP: использовать os.environ SMTP_HOST/PORT/USER/PASS
    # 2) SendGrid: SENDGRID_API_KEY
    # Здесь оставляю заглушку — подключим по твоему выбору.
    pass

async def create_and_send_confirmation(app, user_id: int, email: str):
    token = await create_token(app, user_id)
    confirm_url = os.getenv("BASE_URL", "https://example.com").rstrip("/") + f"/confirm?token={token}"
    subject = "Подтверждение email для FPBooster"
    html = f"""
    <p>Здравствуйте! Для подтверждения email нажмите:</p>
    <p><a href="{confirm_url}">Подтвердить адрес</a></p>
    <p>Ссылка действительна {CONFIRM_TTL_HOURS} часов.</p>
    """
    await send_email(app, email, subject, html)
