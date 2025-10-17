# auth/email_service.py
import os, secrets, smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta

CONFIRM_TTL_HOURS = int(os.getenv("CONFIRM_TTL_HOURS", "24"))

SMTP_HOST = os.getenv("SMTP_HOST", "in-v3.mailjet.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("d5c826b671171c3b292a8e47b30ddea4")  # Mailjet API key
SMTP_PASS = os.getenv("6e96366a642c7217f36546918667ccfd")  # Mailjet secret
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@fpbooster.shop")
FROM_NAME = os.getenv("FROM_NAME", "FPBooster")

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
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = to_email

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(FROM_EMAIL, [to_email], msg.as_string())

async def create_and_send_confirmation(app, user_id: int, email: str):
    token = await create_token(app, user_id)
    confirm_url = os.getenv("BASE_URL", "https://fpbooster.shop").rstrip("/") + f"/confirm?token={token}"
    subject = "Подтверждение email для FPBooster"
    html = f"""
    <p>Здравствуйте! Для подтверждения email нажмите:</p>
    <p><a href="{confirm_url}">Подтвердить адрес</a></p>
    <p>Ссылка действительна {CONFIRM_TTL_HOURS} часов.</p>
    """
    await send_email(app, email, subject, html)

