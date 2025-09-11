import os
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request, Depends
from pydantic import BaseModel
import asyncpg

# === Конфигурация ===
DB_URL = os.getenv("DATABASE_URL")  # строка подключения к PostgreSQL
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")  # токен для админки

app = FastAPI(title="FPBooster License Server", version="1.0")

# === Модели ===
class LicenseIn(BaseModel):
    license: str

class LicenseAdmin(BaseModel):
    license_key: str
    status: str
    expires: str | None = None
    user: str | None = None

# === Подключение к БД ===
async def get_conn():
    return await asyncpg.connect(DB_URL)

# === Middleware для админки ===
def admin_guard(req: Request):
    token = req.headers.get("x-admin-token")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

# === Эндпоинты ===

@app.get("/api/license")
async def check_license(license: str):
    conn = await get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT license_key, status, expires, user_name, created_at, last_check "
            "FROM licenses WHERE license_key=$1", license
        )
        if not row:
            return {"status": "invalid"}
        # обновляем last_check
        await conn.execute("UPDATE licenses SET last_check=NOW() WHERE license_key=$1", license)
        return {
            "status": row["status"],
            "expires": row["expires"].isoformat() if row["expires"] else None,
            "user": row["user_name"],
            "created": row["created_at"].isoformat(),
            "last_check": row["last_check"].isoformat() if row["last_check"] else None
        }
    finally:
        await conn.close()

@app.post("/api/admin/license/create")
async def create_license(data: LicenseAdmin, req: Request = Depends(admin_guard)):
    conn = await get_conn()
    try:
        await conn.execute(
            """
            INSERT INTO licenses (license_key, status, expires, user_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (license_key) DO UPDATE
            SET status=EXCLUDED.status, expires=EXCLUDED.expires, user_name=EXCLUDED.user_name
            """,
            data.license_key, data.status, data.expires, data.user
        )
        return {"ok": True}
    finally:
        await conn.close()

@app.post("/api/admin/license/delete")
async def delete_license(data: LicenseIn, req: Request = Depends(admin_guard)):
    conn = await get_conn()
    try:
        await conn.execute("DELETE FROM licenses WHERE license_key=$1", data.license)
        return {"ok": True}
    finally:
        await conn.close()

@app.get("/api/update")
async def update_meta():
    return {
        "version": "1.8",
        "url": "https://your-cdn-or-render-static/fpbooster_1_8.py",
        "sha256": "PUT_SHA256_HERE",
        "changelog": "Персистентные лицензии, автообновления, улучшенный UI."
    }
