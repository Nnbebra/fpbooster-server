import os
import jwt
from datetime import datetime, timedelta
from passlib.context import CryptContext

# ВАЖНО: Фиксированный ключ. В продакшене лучше брать из os.getenv("SECRET_KEY")
# Если ключ менять при каждом запуске, всех пользователей будет выкидывать.
SECRET_KEY = "CHANGE_ME_TO_SOMETHING_VERY_SECRET_AND_STATIC" 
ALGORITHM = "HS256"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def make_jwt(user_id: int, email: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.utcnow() + timedelta(days=30) # Токен на 30 дней
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_jwt(token: str):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except:
        return None
