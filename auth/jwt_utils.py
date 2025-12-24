import os
from datetime import datetime, timedelta
from jose import jwt, JWTError
from passlib.context import CryptContext

JWT_SECRET = os.getenv("JWT_SECRET", "dev-change-me")
JWT_ALG = "HS256"
JWT_EXPIRES_SECONDS = 7 * 24 * 3600

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)

def verify_password(password: str, hashed: str) -> bool:
    return pwd_ctx.verify(password, hashed)

def make_jwt(user_id: int, email: str) -> str:
    now = int(datetime.utcnow().timestamp())
    # Исправил логику времени для jose
    payload = {"sub": str(user_id), "email": email, "iat": now, "exp": now + JWT_EXPIRES_SECONDS}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)

def decode_jwt(token: str):
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
