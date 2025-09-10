# server.py — Часть 1/2
from flask import Flask, request, jsonify

app = Flask(__name__)

# ====== Настройки ======
# Фейковая база лицензий (в будущем заменим на БД)
LICENSES = {
    "ABC123": "active",   # активная подписка
    "TEST000": "expired"  # истекшая подписка
}

# Актуальная версия и ссылка на exe
LATEST_VERSION = "1.5"
DOWNLOAD_URL = "https://example.com/files/FPBooster_v1.5.exe"
CHANGELOG = "Исправлены ошибки, добавлен автоапдейт"

# ====== Вспомогательная функция ======
def check_license_status(license_key: str) -> str:
    """
    Проверяет статус лицензии.
    Возвращает 'active' или 'expired'.
    """
    return LICENSES.get(license_key, "expired")
# server.py — Часть 2/2

@app.route("/api/update", methods=["GET"])
def update():
    """
    Эндпоинт проверки обновлений.
    Принимает:
      - version (текущая версия клиента)
      - license (ключ лицензии)
    Возвращает JSON с информацией об обновлении.
    """
    version = request.args.get("version", "").strip()
    license_key = request.args.get("license", "").strip()

    license_status = check_license_status(license_key)

    return jsonify({
        "latest_version": LATEST_VERSION,
        "download_url": DOWNLOAD_URL,
        "changelog": CHANGELOG,
        "license_status": license_status
    })

if __name__ == "__main__":
    # Локальный запуск (для теста)
    app.run(host="0.0.0.0", port=5000)
