from datetime import datetime, timedelta
from jose import jwt, JWTError
from passlib.context import CryptContext

# ВАЖНО: Статичный ключ. Не меняй его, иначе все пользователи вылетят.
JWT_SECRET = "b4c9a288-static-super-secret-key-for-fpbooster-fixed"
JWT_ALG = "HS256"
# Время жизни сессии (30 дней)
JWT_EXPIRES_SECONDS = 30 * 24 * 3600

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)

def verify_password(password: str, hashed: str) -> bool:
    return pwd_ctx.verify(password, hashed)

def make_jwt(user_id: int, email: str) -> str:
    now = int(datetime.utcnow().timestamp())
    # Исправлена работа с датой
    exp_time = datetime.utcnow() + timedelta(seconds=JWT_EXPIRES_SECONDS)
    payload = {"sub": str(user_id), "email": email, "iat": now, "exp": exp_time}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)

def decode_jwt(token: str):
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
