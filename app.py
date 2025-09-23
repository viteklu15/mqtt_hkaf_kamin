from flask import Flask, request, redirect, url_for, session, render_template, flash, g, jsonify
import sqlite3, os
from datetime import datetime
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

# MQTT-мост
from mqtt_bridge import bridge, ONLINE, CODE_INDEX, register_state_handler

APP_SECRET = os.environ.get("APP_SECRET", "dev_secret_change_me")
DB_PATH = "sf.db"

app = Flask(__name__)
app.secret_key = APP_SECRET

# ---------- БД ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        # Таблица устройств (для MQTT/привязки)
        con.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            device_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            on_state INTEGER DEFAULT 0,
            program TEXT DEFAULT NULL,
            last_seen TEXT DEFAULT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """)
init_db()

# ---------- Текущий пользователь ----------
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    with db() as con:
        row = con.execute("SELECT id, email FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

@app.before_request
def load_user_to_g():
    g.user = current_user()

# ---------- Хелперы ----------
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.user:
            return redirect(url_for("index") + "#login")
        return fn(*args, **kwargs)
    return wrapper

# ---------- Обработчик апдейтов от MQTT (пишем состояние в БД) ----------
def _state_to_db(device_id: str, kind: str, payload):
    with db() as con:
        row = con.execute("SELECT id FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if not row:
            return
        if kind == "availability":
            if payload is True:
                con.execute("UPDATE devices SET last_seen=? WHERE device_id=?",
                            (datetime.utcnow().isoformat(), device_id))
            return
        if kind == "state" and isinstance(payload, dict):
            fields, values = [], []
            if "on" in payload:
                fields.append("on_state=?")
                values.append(1 if bool(payload["on"]) else 0)
            if isinstance(payload.get("program"), str):
                fields.append("program=?")
                values.append(payload["program"])
            fields.append("last_seen=?")
            values.append(datetime.utcnow().isoformat())
            sql = f"UPDATE devices SET {', '.join(fields)} WHERE device_id=?"
            values.append(device_id)
            con.execute(sql, tuple(values))

register_state_handler(_state_to_db)

# ---------- РОУТЫ ----------
@app.get("/")
def index():
    return render_template("index.html", user=g.user, year=datetime.now().year)

@app.get("/devices")
@login_required
def devices():
    return render_template("index.html", user=g.user, year=datetime.now().year)

# --- Аутентификация ---
@app.post("/register")
def register():
    email = (request.form.get("email") or "").strip().lower()
    pwd = request.form.get("password") or ""
    pwd2 = request.form.get("password2") or ""

    if not email or len(pwd) < 6 or pwd != pwd2:
        flash("Проверьте email и пароли (минимум 6 символов, пароли должны совпадать).")
        return redirect(url_for("index") + "#register")

    with db() as con:
        try:
            cur = con.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, generate_password_hash(pwd), datetime.utcnow().isoformat())
            )
            uid = cur.lastrowid
        except sqlite3.IntegrityError:
            flash("Такой email уже зарегистрирован.")
            return redirect(url_for("index") + "#register")

    session["uid"] = uid
    return redirect(url_for("devices"), code=303)

@app.post("/login")
def login():
    email = (request.form.get("email") or "").strip().lower()
    pwd = request.form.get("password") or ""
    with db() as con:
        row = con.execute("SELECT id, password_hash FROM users WHERE email=?", (email,)).fetchone()
        if not row or not check_password_hash(row["password_hash"], pwd):
            flash("Неверный email или пароль.")
            return redirect(url_for("index") + "#login")
        session["uid"] = row["id"]
    return redirect(url_for("devices"), code=303)

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# --- Эндпойнты аккаунта (их ждёт твой index.html) ---
@app.post("/account/password")
def account_change_password():
    if not g.user:
        return jsonify(ok=False, message="Не авторизован"), 401
    if not request.is_json:
        return jsonify(ok=False, message="Неверный формат"), 400

    data = request.get_json(silent=True) or {}
    old_password = (data.get("old_password") or "").strip()
    new_password = (data.get("new_password") or "").strip()

    if len(new_password) < 6:
        return jsonify(ok=False, message="Минимум 6 символов"), 400

    with db() as con:
        row = con.execute("SELECT password_hash FROM users WHERE id=?", (session["uid"],)).fetchone()
        if not row:
            return jsonify(ok=False, message="Пользователь не найден"), 404
        if not check_password_hash(row["password_hash"], old_password):
            return jsonify(ok=False, message="Текущий пароль неверен"), 400

        con.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (generate_password_hash(new_password), session["uid"])
        )

    return jsonify(ok=True, message="Пароль обновлён")

@app.post("/account/delete")
def account_delete():
    if not g.user:
        return jsonify(ok=False, message="Не авторизован"), 401
    if not request.is_json:
        return jsonify(ok=False, message="Неверный формат"), 400

    data = request.get_json(silent=True) or {}
    password = (data.get("password") or "").strip()

    with db() as con:
        row = con.execute("SELECT password_hash FROM users WHERE id=?", (session["uid"],)).fetchone()
        if not row:
            return jsonify(ok=False, message="Пользователь не найден"), 404
        if not check_password_hash(row["password_hash"], password):
            return jsonify(ok=False, message="Пароль неверен"), 400

        con.execute("DELETE FROM users WHERE id=?", (session["uid"],))

    session.clear()
    return jsonify(ok=True, message="Аккаунт удалён")

# --- API устройств (привязка по коду, список, команды, удаление) ---
@app.get("/api/devices")
@login_required
def api_devices_list():
    with db() as con:
        rows = con.execute("""
            SELECT device_id, name, on_state, program, last_seen, created_at
            FROM devices WHERE user_id=? ORDER BY created_at DESC
        """, (g.user["id"],)).fetchall()
    devices = []
    for r in rows:
        dev_id = r["device_id"]
        devices.append({
            "device_id": dev_id,
            "name": r["name"],
            "online": bool(ONLINE.get(dev_id)),
            "on": bool(r["on_state"]),
            "program": r["program"],
            "last_seen": r["last_seen"],
            "created_at": r["created_at"],
        })
    return jsonify(ok=True, devices=devices)

@app.post("/api/devices/pair")
@login_required
def api_devices_pair():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify(ok=False, message="Не указан код"), 400

    device_id = CODE_INDEX.get(code)
    if not device_id:
        return jsonify(ok=False, message="Код не найден (устройство ещё не прислало индекс)"), 404

    res = bridge.publish_pair_and_wait(device_id, code, timeout_sec=12)
    if not res.get("ok"):
        return jsonify(ok=False, message=f"Ошибка привязки: {res.get('error','unknown')}"), 502

    with db() as con:
        exists = con.execute("SELECT 1 FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if not exists:
            con.execute("""
                INSERT INTO devices (user_id, device_id, name, on_state, program, last_seen, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                g.user["id"],
                device_id,
                "Сушильный шкаф",
                0,
                None,
                None,
                datetime.utcnow().isoformat()
            ))
        else:
            con.execute("UPDATE devices SET user_id=? WHERE device_id=?",
                        (g.user["id"], device_id))

    return jsonify(ok=True, device_id=device_id, name="Сушильный шкаф")

@app.post("/api/device/<device_id>/cmd")
@login_required
def api_device_cmd(device_id):
    with db() as con:
        row = con.execute("SELECT 1 FROM devices WHERE user_id=? AND device_id=?",
                          (g.user["id"], device_id)).fetchone()
        if not row:
            return jsonify(ok=False, message="Устройство не найдено"), 404

    if not request.is_json:
        return jsonify(ok=False, message="Тело должно быть JSON"), 400
    payload = request.get_json(silent=True) or {}

    try:
        bridge.publish_cmd(device_id, payload)
    except Exception as e:
        return jsonify(ok=False, message=f"MQTT ошибка: {e}"), 502

    return jsonify(ok=True)

@app.delete("/api/device/<device_id>")
@login_required
def api_device_delete(device_id):
    with db() as con:
        con.execute("DELETE FROM devices WHERE user_id=? AND device_id=?",
                    (g.user["id"], device_id))
    return jsonify(ok=True)

# ---------- Запуск ----------
if __name__ == "__main__":
    # Стартуем MQTT-мост один раз перед сервером (Flask 3.x — без before_first_request)
    bridge.start()
    # Отключаем авто-перезапуск, чтобы мост не стартовал дважды
    app.run(debug=True, use_reloader=False)
