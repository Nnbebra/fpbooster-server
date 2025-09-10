# server.py — FPBooster License Server with Admin Panel (Flask + SQLite)
# Features:
# - /api/license    -> check license status
# - /api/update     -> update info + license gate
# - /admin          -> simple admin panel (list/add/edit/delete licenses)
# Security:
# - Admin routes require ADMIN_TOKEN (via query param ?token=... or header X-Admin-Token)
# Config via ENV:
#   ADMIN_TOKEN       - required to access admin panel (set this!)
#   LATEST_VERSION    - latest client version (default "1.5")
#   DOWNLOAD_URL      - URL to latest installer/binary (optional)
#   PORT              - port to bind (Render sets it automatically via PORT)
#
# Run locally:
#   set ADMIN_TOKEN=your_secret && python server.py
# Deploy (gunicorn):
#   gunicorn server:app

import os
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, abort, redirect, url_for

APP_NAME = "FPBooster License Server"

# -------------------------- App & Config --------------------------
app = Flask(__name__)

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
LATEST_VERSION = os.getenv("LATEST_VERSION", "1.5").strip()
DOWNLOAD_URL = os.getenv("DOWNLOAD_URL", "").strip()  # e.g. https://your-cdn/FPBooster_1.6.exe
DB_PATH = os.getenv("DB_PATH", "licenses.db")

# -------------------------- Database helpers --------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL,
            expires TEXT NOT NULL,
            user TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_check TEXT
        );
    """)
    conn.commit()

    # Seed one test license if table is empty
    cur.execute("SELECT COUNT(*) AS c FROM licenses;")
    if cur.fetchone()["c"] == 0:
        cur.execute("""
            INSERT INTO licenses (license_key, status, expires, user, created_at, last_check)
            VALUES (?, ?, ?, ?, ?, ?);
        """, ("ABC123", "active", "2025-12-31", "maksim", now(), None))
        conn.commit()
    conn.close()

def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def fetch_by_key(key: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM licenses WHERE license_key = ?;", (key,))
    row = cur.fetchone()
    conn.close()
    return row

def fetch_all(search: str = ""):
    conn = get_conn()
    cur = conn.cursor()
    if search:
        like = f"%{search}%"
        cur.execute("""
            SELECT * FROM licenses
            WHERE license_key LIKE ? OR user LIKE ?
            ORDER BY id DESC;
        """, (like, like))
    else:
        cur.execute("SELECT * FROM licenses ORDER BY id DESC;")
    rows = cur.fetchall()
    conn.close()
    return rows

def create_license(license_key: str, status: str, expires: str, user: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO licenses (license_key, status, expires, user, created_at, last_check)
        VALUES (?, ?, ?, ?, ?, NULL);
    """, (license_key, status, expires, user, now()))
    conn.commit()
    conn.close()

def update_license_by_id(lic_id: int, license_key: str, status: str, expires: str, user: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE licenses
        SET license_key = ?, status = ?, expires = ?, user = ?
        WHERE id = ?;
    """, (license_key, status, expires, user, lic_id))
    conn.commit()
    conn.close()

def delete_license_by_id(lic_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM licenses WHERE id = ?;", (lic_id,))
    conn.commit()
    conn.close()

def touch_last_check(license_key: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE licenses
        SET last_check = ?
        WHERE license_key = ?;
    """, (now(), license_key))
    conn.commit()
    conn.close()

# -------------------------- Security helpers --------------------------
def require_admin():
    token = request.args.get("token", "") or request.headers.get("X-Admin-Token", "")
    if not ADMIN_TOKEN:
        # If no token configured, block admin to avoid accidental open panel
        abort(403, description="Admin panel locked: set ADMIN_TOKEN env variable")
    if token != ADMIN_TOKEN:
        abort(403, description="Forbidden: invalid admin token")

# -------------------------- API: License check --------------------------
@app.route("/api/license", methods=["GET"])
def api_license():
    key = request.args.get("license", "").strip()
    if not key:
        return jsonify({"status": "invalid", "message": "license param required"}), 400
    row = fetch_by_key(key)
    if not row:
        return jsonify({"status": "invalid"})
    # Optionally check expiration date
    status = row["status"]
    expires = row["expires"]
    user = row["user"]
    touch_last_check(key)
    return jsonify({
        "status": status,
        "expires": expires,
        "user": user
    })

# -------------------------- API: Update gate --------------------------
@app.route("/api/update", methods=["GET"])
def api_update():
    version = request.args.get("version", "").strip()
    key = request.args.get("license", "").strip()
    lic = fetch_by_key(key) if key else None
    lic_status = "invalid"
    if lic:
        lic_status = lic["status"]
        touch_last_check(lic["license_key"])

    payload = {
        "license_status": lic_status,
        "latest_version": LATEST_VERSION
    }
    if DOWNLOAD_URL:
        payload["download_url"] = DOWNLOAD_URL

    return jsonify(payload)

# -------------------------- API: Admin CRUD (JSON) --------------------------
@app.route("/api/licenses", methods=["GET"])
def api_list_licenses():
    require_admin()
    q = request.args.get("q", "").strip()
    rows = fetch_all(q)
    data = [dict(row) for row in rows]
    return jsonify(data)

@app.route("/api/license", methods=["POST"])
def api_create_license():
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    license_key = (data.get("license_key") or "").strip()
    status = (data.get("status") or "active").strip()
    expires = (data.get("expires") or "").strip()
    user = (data.get("user") or "").strip()
    if not license_key or not expires or not user:
        return jsonify({"ok": False, "error": "license_key, expires, user required"}), 400
    try:
        create_license(license_key, status, expires, user)
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "license_key must be unique"}), 409

@app.route("/api/license/<int:lic_id>", methods=["PUT"])
def api_update_license(lic_id: int):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    license_key = (data.get("license_key") or "").strip()
    status = (data.get("status") or "active").strip()
    expires = (data.get("expires") or "").strip()
    user = (data.get("user") or "").strip()
    if not license_key or not expires or not user:
        return jsonify({"ok": False, "error": "license_key, expires, user required"}), 400
    try:
        update_license_by_id(lic_id, license_key, status, expires, user)
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "license_key must be unique"}), 409

@app.route("/api/license/<int:lic_id>", methods=["DELETE"])
def api_delete_license(lic_id: int):
    require_admin()
    delete_license_by_id(lic_id)
    return jsonify({"ok": True})

# -------------------------- Admin HTML Panel --------------------------
def html_base(content: str, token: str, title: str = "Admin"):
    # Minimal Bootstrap-based layout
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>{APP_NAME} — {title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
 body {{ padding: 24px; background: #0f2027; background: linear-gradient(135deg, #0f2027, #203a43, #2c5364); color: #fff; }}
 .card {{ background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.15); }}
 .form-control, .form-select {{ background: rgba(0,0,0,0.25); color: #fff; border: 1px solid rgba(255,255,255,0.25); }}
 .form-control::placeholder {{ color: #bbb; }}
 a, .btn-link {{ color: #8ad; }}
 .badge {{ font-size: 0.9rem; }}
</style>
</head>
<body>
<div class="container">
  <h2 class="mb-4">FPBooster — Админ-панель лицензий</h2>
  <div class="mb-3">
    <a class="btn btn-primary me-2" href="/admin?token={token}">Список</a>
    <a class="btn btn-success" href="/admin/new?token={token}">Добавить</a>
  </div>
  {content}
</div>
</body>
</html>"""

def status_badge(status: str) -> str:
    colors = {"active": "success", "expired": "secondary", "banned": "danger", "invalid": "warning"}
    color = colors.get(status, "light")
    return f'<span class="badge bg-{color}">{status}</span>'

@app.route("/")
def index_root():
    # no public landing — redirect to admin if token provided, otherwise 404-like info
    token = request.args.get("token", "")
    if token:
        return redirect(url_for("admin_list") + f"?token={token}")
    return ("FPBooster License Server. Add ?token=YOUR_ADMIN_TOKEN to access /admin", 404)

@app.route("/admin")
def admin_list():
    require_admin()
    token = request.args.get("token", "")
    q = request.args.get("q", "").strip()
    rows = fetch_all(q)
    items = []
    for r in rows:
        items.append(f"""
        <tr>
            <td>{r["id"]}</td>
            <td><code>{r["license_key"]}</code></td>
            <td>{status_badge(r["status"])}</td>
            <td>{r["expires"]}</td>
            <td>{r["user"]}</td>
            <td>{r["created_at"]}</td>
            <td>{r["last_check"] or "-"}</td>
            <td>
                <a class="btn btn-sm btn-warning me-2" href="/admin/edit/{r["id"]}?token={token}">Изменить</a>
                <form method="post" action="/admin/delete/{r["id"]}?token={token}" style="display:inline" onsubmit="return confirm('Удалить лицензию?');">
                    <button type="submit" class="btn btn-sm btn-danger">Удалить</button>
                </form>
            </td>
        </tr>
        """)
    content = f"""
    <div class="card p-3 mb-3">
      <form method="get" action="/admin">
        <input type="hidden" name="token" value="{token}">
        <div class="row g-2 align-items-center">
          <div class="col-sm-8">
            <input class="form-control" type="text" name="q" value="{q}" placeholder="Поиск по ключу или пользователю">
          </div>
          <div class="col-sm-4">
            <button class="btn btn-outline-light w-100" type="submit">Поиск</button>
          </div>
        </div>
      </form>
    </div>
    <div class="card p-3">
      <div class="table-responsive">
        <table class="table table-dark table-sm align-middle">
          <thead><tr>
            <th>ID</th><th>Ключ</th><th>Статус</th><th>Истекает</th>
            <th>Пользователь</th><th>Создано</th><th>Последняя проверка</th><th></th>
          </tr></thead>
          <tbody>
            {''.join(items) if items else '<tr><td colspan="8" class="text-center text-muted">Нет записей</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>
    """
    return html_base(content, token, title="Список лицензий")

@app.route("/admin/new", methods=["GET", "POST"])
def admin_new():
    require_admin()
    token = request.args.get("token", "")
    if request.method == "POST":
        license_key = (request.form.get("license_key") or "").strip()
        status = (request.form.get("status") or "active").strip()
        expires = (request.form.get("expires") or "").strip()
        user = (request.form.get("user") or "").strip()
        if not license_key or not expires or not user:
            msg = '<div class="alert alert-danger">Заполните все поля</div>'
        else:
            try:
                create_license(license_key, status, expires, user)
                return redirect(url_for("admin_list") + f"?token={token}")
            except sqlite3.IntegrityError:
                msg = '<div class="alert alert-danger">Ключ уже существует</div>'
    else:
        msg = ""

    form = f"""
    {msg}
    <div class="card p-3">
      <form method="post" action="/admin/new?token={token}">
        <div class="mb-3">
          <label class="form-label">Ключ</label>
          <input class="form-control" name="license_key" placeholder="Например: ABC123">
        </div>
        <div class="mb-3">
          <label class="form-label">Статус</label>
          <select class="form-select" name="status">
            <option value="active">active</option>
            <option value="expired">expired</option>
            <option value="banned">banned</option>
          </select>
        </div>
        <div class="mb-3">
          <label class="form-label">Истекает (YYYY-MM-DD)</label>
          <input class="form-control" name="expires" placeholder="2025-12-31">
        </div>
        <div class="mb-3">
          <label class="form-label">Пользователь</label>
          <input class="form-control" name="user" placeholder="maksim">
        </div>
        <div class="d-flex gap-2">
          <a class="btn btn-secondary" href="/admin?token={token}">Отмена</a>
          <button class="btn btn-success" type="submit">Создать</button>
        </div>
      </form>
    </div>
    """
    return html_base(form, token, title="Новая лицензия")

@app.route("/admin/edit/<int:lic_id>", methods=["GET", "POST"])
def admin_edit(lic_id: int):
    require_admin()
    token = request.args.get("token", "")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM licenses WHERE id = ?;", (lic_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        abort(404, description="Лицензия не найдена")

    if request.method == "POST":
        license_key = (request.form.get("license_key") or "").strip()
        status = (request.form.get("status") or "active").strip()
        expires = (request.form.get("expires") or "").strip()
        user = (request.form.get("user") or "").strip()
        if not license_key or not expires or not user:
            msg = '<div class="alert alert-danger">Заполните все поля</div>'
        else:
            try:
                update_license_by_id(lic_id, license_key, status, expires, user)
                return redirect(url_for("admin_list") + f"?token={token}")
            except sqlite3.IntegrityError:
                msg = '<div class="alert alert-danger">Ключ уже существует</div>'
    else:
        msg = ""

    selected = lambda s: 'selected="selected"' if s == row["status"] else ""
    form = f"""
    {msg}
    <div class="card p-3">
      <form method="post" action="/admin/edit/{lic_id}?token={token}">
        <div class="mb-3">
          <label class="form-label">Ключ</label>
          <input class="form-control" name="license_key" value="{row['license_key']}">
        </div>
        <div class="mb-3">
          <label class="form-label">Статус</label>
          <select class="form-select" name="status">
            <option value="active" {selected("active")}>active</option>
            <option value="expired" {selected("expired")}>expired</option>
            <option value="banned" {selected("banned")}>banned</option>
          </select>
        </div>
        <div class="mb-3">
          <label class="form-label">Истекает (YYYY-MM-DD)</label>
          <input class="form-control" name="expires" value="{row['expires']}">
        </div>
        <div class="mb-3">
          <label class="form-label">Пользователь</label>
          <input class="form-control" name="user" value="{row['user']}">
        </div>
        <div class="d-flex gap-2">
          <a class="btn btn-secondary" href="/admin?token={token}">Отмена</a>
          <button class="btn btn-warning" type="submit">Сохранить</button>
        </div>
      </form>
    </div>
    """
    return html_base(form, token, title="Редактирование лицензии")

@app.route("/admin/delete/<int:lic_id>", methods=["POST"])
def admin_delete(lic_id: int):
    require_admin()
    token = request.args.get("token", "")
    delete_license_by_id(lic_id)
    return redirect(url_for("admin_list") + f"?token={token}")

# -------------------------- Startup --------------------------
init_db()

# -------------------------- Dev server --------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    # IMPORTANT: set ADMIN_TOKEN before running in production
    app.run(host="0.0.0.0", port=port)
