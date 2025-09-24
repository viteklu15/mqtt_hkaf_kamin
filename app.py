from flask import Flask, request, redirect, url_for, session, render_template, flash, g, jsonify
import sqlite3, os, json
from datetime import datetime
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

# MQTT-мост
from mqtt_bridge import bridge, ONLINE, CODE_INDEX, register_state_handler

APP_SECRET = os.environ.get("APP_SECRET", "dev_secret_change_me")
DB_PATH = "sf.db"

app = Flask(__name__)
app.secret_key = APP_SECRET

FIREPLACE_MODE_OPTIONS = [(str(i), str(i)) for i in range(1, 5)]
FIREPLACE_SOUND_OPTIONS = [(str(i), str(i)) for i in range(1, 4)]

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
            kind TEXT NOT NULL DEFAULT 'dryer',
            on_state INTEGER DEFAULT 0,
            program TEXT DEFAULT NULL,
            state_json TEXT DEFAULT NULL,
            last_seen TEXT DEFAULT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """)

        # миграция: если колонка kind отсутствует (старая БД) — добавляем
        cols = {row["name"] for row in con.execute("PRAGMA table_info(devices)")}
        if "kind" not in cols:
            con.execute("ALTER TABLE devices ADD COLUMN kind TEXT NOT NULL DEFAULT 'dryer'")
        if "state_json" not in cols:
            con.execute("ALTER TABLE devices ADD COLUMN state_json TEXT DEFAULT NULL")
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


def _load_state_payload(raw_state):
    if not raw_state:
        return {}
    try:
        return json.loads(raw_state)
    except json.JSONDecodeError:
        return {}


def _bool_from_payload(value, default=False):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes"}
    return bool(value)


def fetch_user_devices(user_id: int):
    with db() as con:
        rows = con.execute("""
            SELECT device_id, name, kind, on_state, program, state_json, last_seen, created_at
            FROM devices WHERE user_id=? ORDER BY created_at DESC
        """, (user_id,)).fetchall()

    devices = []
    for row in rows:
        device_id = row["device_id"]
        kind = (row["kind"] or "dryer").lower()
        state_payload = _load_state_payload(row["state_json"])

        power_state = bool(row["on_state"])
        if "on" in state_payload:
            power_state = _bool_from_payload(state_payload.get("on"), power_state)

        base = {
            "id": device_id,
            "kind": kind,
            "title": row["name"] or ("Камин" if kind == "fireplace" else "Сушильный шкаф"),
            "online": bool(ONLINE.get(device_id)),
            "power": power_state,
            "state": state_payload,
            "last_seen": row["last_seen"],
            "created_at": row["created_at"],
        }

        if kind == "fireplace":
            mode_value = state_payload.get("mode")
            if mode_value is None:
                mode_value = row["program"] or FIREPLACE_MODE_OPTIONS[0][0]
            else:
                mode_value = str(mode_value)

            sound_value = state_payload.get("sound")
            if sound_value is None:
                sound_value = FIREPLACE_SOUND_OPTIONS[0][0]
            else:
                sound_value = str(sound_value)

            backlight = _bool_from_payload(state_payload.get("backlight"), False)

            base.update({
                "mode_options": FIREPLACE_MODE_OPTIONS,
                "mode_value": mode_value,
                "sound_options": FIREPLACE_SOUND_OPTIONS,
                "sound_value": sound_value,
                "backlight": backlight,
            })
        else:
            program_value = state_payload.get("program")
            if program_value is None:
                program_value = row["program"] or "standard"
            else:
                program_value = str(program_value)

            sound_flag = _bool_from_payload(state_payload.get("sound"), False)

            temp_c = state_payload.get("temp_c")
            if isinstance(temp_c, (int, float)):
                temp_c = round(float(temp_c), 1)
                if float(temp_c).is_integer():
                    temp_c = int(temp_c)
            elif temp_c is not None:
                try:
                    temp_c = float(temp_c)
                except (TypeError, ValueError):
                    temp_c = None

            time_left = state_payload.get("time_left")
            if isinstance(time_left, (int, float)):
                total_seconds = int(time_left)
                minutes, seconds = divmod(max(total_seconds, 0), 60)
                time_left = f"{minutes:02d}:{seconds:02d}"
            elif time_left is not None:
                time_left = str(time_left)

            base.update({
                "program_value": program_value,
                "sound": sound_flag,
                "temp_c": temp_c,
                "time_left": time_left,
            })

        devices.append(base)
    return devices

# ---------- Проверка серийного номера через MQTT индекс ----------
def lookup_claim_status(serial: str, user_id: int, con=None):
    """Возвращает информацию о статусе устройства по серийному номеру."""
    code = (serial or "").strip().upper()

    device_id = CODE_INDEX.get(code)
    online = False
    owner_id = None

    if device_id:
        online = bool(ONLINE.get(device_id))

        close_conn = False
        if con is None:
            con = db()
            close_conn = True
        try:
            row = con.execute(
                "SELECT user_id FROM devices WHERE device_id=?",
                (device_id,)
            ).fetchone()
            if row:
                owner_id = row["user_id"]
        finally:
            if close_conn:
                con.close()

        if owner_id is None:
            status = "available"
            if online:
                message = "Устройство свободно и готово к подключению."
            else:
                message = "Устройство найдено, но сейчас не в сети."
        elif owner_id == user_id:
            status = "owned_by_you"
            message = "Устройство уже привязано к вашему аккаунту."
        else:
            status = "occupied"
            message = "Устройство уже привязано к другому аккаунту."
    else:
        status = "not_found"
        message = "Устройство не в сети или уже привязано."

    return {
        "serial": code,
        "device_id": device_id,
        "status": status,
        "online": online,
        "message": message,
        "owner_id": owner_id,
    }


def _pair_error_message(error_code: str) -> str:
    code = (error_code or "unknown").strip()
    friendly = {
        "bad_code": "устройство отклонило код привязки",
        "timeout_no_pair_result": "устройство не ответило на запрос",
    }
    return friendly.get(code, code)


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
                values.append(1 if _bool_from_payload(payload.get("on")) else 0)

            program_value = None
            if isinstance(payload.get("program"), str) and payload.get("program"):
                program_value = payload["program"]
            elif isinstance(payload.get("mode"), str) and payload.get("mode"):
                program_value = payload["mode"]

            if program_value is not None:
                fields.append("program=?")
                values.append(program_value)

            fields.append("state_json=?")
            values.append(json.dumps(payload, ensure_ascii=False))
            fields.append("last_seen=?")
            values.append(datetime.utcnow().isoformat())
            sql = f"UPDATE devices SET {', '.join(fields)} WHERE device_id=?"
            values.append(device_id)
            con.execute(sql, tuple(values))

register_state_handler(_state_to_db)

# ---------- РОУТЫ ----------
@app.get("/")
def index():
    devices = fetch_user_devices(g.user["id"]) if g.user else []
    return render_template("index.html", user=g.user, devices=devices, year=datetime.now().year)

@app.get("/devices")
@login_required
def devices():
    devices = fetch_user_devices(g.user["id"]) if g.user else []
    return render_template("index.html", user=g.user, devices=devices, year=datetime.now().year)

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
            SELECT device_id, name, kind, on_state, program, state_json, last_seen, created_at
            FROM devices WHERE user_id=? ORDER BY created_at DESC
        """, (g.user["id"],)).fetchall()
    devices = []
    for r in rows:
        dev_id = r["device_id"]
        kind = (r["kind"] or "dryer").lower()
        state_payload = _load_state_payload(r["state_json"])

        power_state = bool(r["on_state"])
        if "on" in state_payload:
            power_state = _bool_from_payload(state_payload.get("on"), power_state)

        device_info = {
            "device_id": dev_id,
            "name": r["name"],
            "kind": kind,
            "online": bool(ONLINE.get(dev_id)),
            "on": power_state,
            "program": r["program"],
            "last_seen": r["last_seen"],
            "created_at": r["created_at"],
            "state": state_payload,
        }

        if kind == "fireplace":
            if "mode" in state_payload and isinstance(state_payload.get("mode"), str):
                device_info["program"] = state_payload["mode"]
            device_info.update({
                "mode": state_payload.get("mode"),
                "sound": state_payload.get("sound"),
                "backlight": _bool_from_payload(state_payload.get("backlight"), False),
            })
        else:
            program_value = state_payload.get("program")
            if isinstance(program_value, str):
                device_info["program"] = program_value
            device_info.update({
                "sound": _bool_from_payload(state_payload.get("sound"), False),
                "temp_c": state_payload.get("temp_c"),
                "time_left": state_payload.get("time_left"),
            })

        devices.append(device_info)
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
        return jsonify(ok=False, message=f"Ошибка привязки: {_pair_error_message(res.get('error'))}"), 502

    with db() as con:
        exists = con.execute("SELECT 1 FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if not exists:
            con.execute("""
                INSERT INTO devices (user_id, device_id, name, kind, on_state, program, last_seen, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                g.user["id"],
                device_id,
                "Сушильный шкаф",
                "dryer",
                0,
                "standard",
                None,
                datetime.utcnow().isoformat()
            ))
        else:
            con.execute("UPDATE devices SET user_id=?, kind=?, name=?, program=? WHERE device_id=?",
                        (g.user["id"], "dryer", "Сушильный шкаф", "standard", device_id))

    return jsonify(ok=True, device_id=device_id, name="Сушильный шкаф")


@app.post("/api/devices/check_serial")
@login_required
def api_devices_check_serial():
    if not request.is_json:
        return jsonify(ok=False, message="Тело должно быть JSON"), 400

    data = request.get_json(silent=True) or {}
    serial = (data.get("serial") or "").strip().upper()

    if len(serial) != 6 or not serial.isalnum():
        return jsonify(ok=False, message="Серийный номер должен состоять из 6 символов"), 400

    res = lookup_claim_status(serial, g.user["id"])
    return jsonify(ok=True, **res)


@app.post("/api/devices/manual")
@login_required
def api_devices_manual_add():
    if not request.is_json:
        return jsonify(ok=False, message="Тело должно быть JSON"), 400

    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or "").strip().lower()
    serial = (data.get("serial") or "").strip().upper()

    if kind not in {"fireplace", "dryer"}:
        return jsonify(ok=False, message="Неизвестный тип устройства"), 400

    if len(serial) != 6 or not serial.isalnum():
        return jsonify(ok=False, message="Серийный номер должен состоять из 6 символов"), 400

    device_name = "Камин" if kind == "fireplace" else "Сушильный шкаф"
    program_default = FIREPLACE_MODE_OPTIONS[0][0] if kind == "fireplace" else "standard"

    with db() as con:
        claim = lookup_claim_status(serial, g.user["id"], con=con)
        status = claim["status"]

        if status == "not_found":
            return jsonify(ok=False, message="Устройство не найдено. Проверьте питание и подключение к сети."), 404
        if status == "occupied":
            return jsonify(ok=False, message="Устройство уже привязано к другому аккаунту"), 409
        if status == "owned_by_you":
            return jsonify(ok=False, message="Устройство уже привязано к вашему аккаунту"), 409
        if not claim.get("online", False):
            return jsonify(ok=False, message="Устройство найдено, но сейчас не в сети"), 409

        device_id = claim.get("device_id")
        if not device_id:
            return jsonify(ok=False, message="Не удалось определить устройство"), 400

        pair_result = bridge.publish_pair_and_wait(device_id, serial, timeout_sec=12)
        if not pair_result.get("ok"):
            return jsonify(ok=False, message=f"Ошибка привязки: {_pair_error_message(pair_result.get('error'))}"), 502

        try:
            con.execute("""
                INSERT INTO devices (user_id, device_id, name, kind, on_state, program, last_seen, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                g.user["id"], device_id, device_name, kind, 0, program_default, None, datetime.utcnow().isoformat()
            ))
        except sqlite3.IntegrityError:
            # На случай гонки добавления — обновляем владельца и параметры
            con.execute("UPDATE devices SET user_id=?, kind=?, name=?, program=? WHERE device_id=?",
                        (g.user["id"], kind, device_name, program_default, device_id))

    return jsonify(ok=True, device_id=device_id, kind=kind, name=device_name)

def _get_user_device(device_id: str):
    if not g.user:
        return None
    with db() as con:
        row = con.execute(
            "SELECT device_id, kind, name FROM devices WHERE user_id=? AND device_id=?",
            (g.user["id"], device_id),
        ).fetchone()
    return dict(row) if row else None


def _update_device_field(device_id: str, field: str, value):
    with db() as con:
        con.execute(f"UPDATE devices SET {field}=? WHERE device_id=?", (value, device_id))


@app.post("/api/device/<device_id>/power")
@login_required
def api_device_power(device_id):
    device = _get_user_device(device_id)
    if not device:
        return jsonify(ok=False, message="Устройство не найдено"), 404

    if not request.is_json:
        return jsonify(ok=False, message="Тело должно быть JSON"), 400

    data = request.get_json(silent=True) or {}
    if "on" not in data:
        return jsonify(ok=False, message="Параметр 'on' обязателен"), 400

    on_value = data["on"]
    if isinstance(on_value, str):
        on_state = on_value.strip().lower() in {"1", "true", "on"}
    else:
        on_state = bool(on_value)

    try:
        bridge.publish_cmd(device_id, {"on": on_state})
    except Exception as e:
        return jsonify(ok=False, message=f"MQTT ошибка: {e}"), 502

    _update_device_field(device_id, "on_state", 1 if on_state else 0)
    return jsonify(ok=True)


@app.post("/api/device/<device_id>/set")
@login_required
def api_device_set(device_id):
    device = _get_user_device(device_id)
    if not device:
        return jsonify(ok=False, message="Устройство не найдено"), 404

    if not request.is_json:
        return jsonify(ok=False, message="Тело должно быть JSON"), 400

    data = request.get_json(silent=True) or {}
    items = list(data.items())
    if not items:
        return jsonify(ok=False, message="Не переданы параметры"), 400

    key, value = items[0]

    try:
        bridge.publish_cmd(device_id, {key: value})
    except Exception as e:
        return jsonify(ok=False, message=f"MQTT ошибка: {e}"), 502

    if key in {"program", "mode"} and isinstance(value, str):
        _update_device_field(device_id, "program", value)

    return jsonify(ok=True)


@app.post("/api/device/<device_id>/rename")
@login_required
def api_device_rename(device_id):
    device = _get_user_device(device_id)
    if not device:
        return jsonify(ok=False, message="Устройство не найдено"), 404

    if not request.is_json:
        return jsonify(ok=False, message="Тело должно быть JSON"), 400

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify(ok=False, message="Имя устройства не может быть пустым"), 400

    with db() as con:
        con.execute(
            "UPDATE devices SET name=? WHERE user_id=? AND device_id=?",
            (title, g.user["id"], device_id),
        )

    return jsonify(ok=True, title=title)


@app.post("/api/device/<device_id>/cmd")
@login_required
def api_device_cmd(device_id):
    device = _get_user_device(device_id)
    if not device:
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
