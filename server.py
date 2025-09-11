import os
from typing import Optional, Literal
from datetime import date, datetime

import asyncpg
from fastapi import FastAPI, HTTPException, Request, Depends
from pydantic import BaseModel, validator

# ========= Конфигурация через переменные окружения =========
DB_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

# Метаданные обновления клиента (для автообновлений FPBooster)
UPDATE_VERSION = os.getenv("UPDATE_VERSION", "1.8")
UPDATE_URL = os.getenv("UPDATE_URL", "https://your-cdn-or-render-static/fpbooster_1_8.py")
UPDATE_SHA256 = os.getenv("UPDATE_SHA256", "PUT_SHA256_HERE")
UPDATE_CHANGELOG = os.getenv("UPDATE_CHANGELOG", "Персистентные лицензии, автообновления, улучшенный UI.")

if not DB_URL:
    # Это важно: без DATABASE_URL сервер не сможет работать
    # На Render добавьте Environment Variable DATABASE_URL с External Database URL
    raise RuntimeError("DATABASE_URL is not set")

app = FastAPI(title="FPBooster License Server", version="1.0.0")


# ========= Модели =========
class LicenseIn(BaseModel):
    license: str


class LicenseAdmin(BaseModel):
    license_key: str
    status: Literal["active", "expired", "banned"]
    # Разрешаем и строку (ISO), и объект date
    expires: Optional[date] = None
    user: Optional[str] = None

    @validator("expires", pre=True)
    def parse_expires(cls, v):
        if v in (None, "", "null"):
            return None
        if isinstance(v, date):
            return v
        # Пытаемся разобрать ISO-дату (YYYY-MM-DD)
        try:
            return date.fromisoformat(str(v))
        except Exception as e:
            raise ValueError(f"expires must be ISO date (YYYY-MM-DD), got {v!r}: {e}")


# ========= Пул соединений с БД =========
@app.on_event("startup")
async def startup():
    app.state.pool = await asyncpg.create_pool(dsn=DB_URL, min_size=1, max_size=5, command_timeout=10)


@app.on_event("shutdown")
async def shutdown():
    pool = app.state.pool
    if pool:
        await pool.close()


# ========= Зависимость: защита админ-эндпоинтов =========
def admin_guard(request: Request):
    token = request.headers.get("x-admin-token")
    if not ADMIN_TOKEN:
        # Если токен не задан в окружении — явно запрещаем
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN is not configured on server")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden: invalid admin token")
    return True


# ========= Служебные эндпоинты =========
@app.get("/api/health")
async def health():
    # Проверяем доступность БД через простой запрос
    try:
        async with app.state.pool.acquire() as conn:
            await conn.execute("SELECT 1;")
        return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ========= Лицензии: публичный эндпоинт для клиента =========
@app.get("/api/license")
async def check_license(license: str):
    if not license or not license.strip():
        return {"status": "invalid"}

    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT license_key, status, expires, user_name, created_at, last_check
            FROM licenses
            WHERE license_key = $1
            """,
            license.strip(),
        )
        if not row:
            return {"status": "invalid"}

        # Обновляем last_check
        await conn.execute(
            "UPDATE licenses SET last_check = NOW() WHERE license_key = $1",
            license.strip(),
        )

        return {
            "status": row["status"],
            "expires": row["expires"].isoformat() if row["expires"] else None,
            "user": row["user_name"],
            "created": row["created_at"].isoformat() if row["created_at"] else None,
            "last_check": row["last_check"].isoformat() if row["last_check"] else None,
        }


# ========= Лицензии: админ-эндпоинты =========
@app.post("/api/admin/license/create")
async def create_or_update_license(data: LicenseAdmin, _guard: bool = Depends(admin_guard)):
    async with app.state.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO licenses (license_key, status, expires, user_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (license_key)
            DO UPDATE
            SET status = EXCLUDED.status,
                expires = EXCLUDED.expires,
                user_name = EXCLUDED.user_name
            """,
            data.license_key.strip(),
            data.status,
            data.expires,
            data.user.strip() if data.user else None,
        )
        return {"ok": True}


@app.post("/api/admin/license/delete")
async def delete_license(data: LicenseIn, _guard: bool = Depends(admin_guard)):
    async with app.state.pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM licenses WHERE license_key = $1",
            data.license.strip(),
        )
        # result будет в виде 'DELETE <count>'
        return {"ok": True, "result": result}


@app.get("/api/admin/license/get")
async def get_license(license: str, _guard: bool = Depends(admin_guard)):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT license_key, status, expires, user_name, created_at, last_check
            FROM licenses
            WHERE license_key = $1
            """,
            license.strip(),
        )
        if not row:
            raise HTTPException(status_code=404, detail="License not found")
        return {
            "license_key": row["license_key"],
            "status": row["status"],
            "expires": row["expires"].isoformat() if row["expires"] else None,
            "user": row["user_name"],
            "created": row["created_at"].isoformat() if row["created_at"] else None,
            "last_check": row["last_check"].isoformat() if row["last_check"] else None,
        }


# ========= Мета-обновления для клиента =========
@app.get("/api/update")
async def update_meta():
    return {
        "version": UPDATE_VERSION,
        "url": UPDATE_URL,
        "sha256": UPDATE_SHA256,
        "changelog": UPDATE_CHANGELOG,
    }
