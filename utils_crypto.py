import os
from cryptography.fernet import Fernet

# Получаем ключ из переменных окружения (systemd)
DATA_KEY = os.getenv("DATA_ENCRYPTION_KEY")
if not DATA_KEY:
    print("WARNING: DATA_ENCRYPTION_KEY not found! Generating temp key.")
    DATA_KEY = Fernet.generate_key().decode()

_cipher_suite = Fernet(DATA_KEY.encode())

def encrypt_data(data: str) -> str:
    return _cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(token: str) -> str:
    return _cipher_suite.decrypt(token.encode()).decode()