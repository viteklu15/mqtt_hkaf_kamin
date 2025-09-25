from flask import Flask, request, redirect, url_for, session, render_template, flash, g, jsonify, Response, stream_with_context
import sqlite3, os, json, queue, threading, secrets, base64
from datetime import datetime, timedelta
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from urllib.parse import urlencode

# MQTT-мост
from mqtt_bridge import bridge, ONLINE, CODE_INDEX, register_state_handler

APP_SECRET = os.environ.get("APP_SECRET", "dev_secret_change_me")
DB_PATH = "sf.db"

YANDEX_CLIENT_ID = os.environ.get("YANDEX_CLIENT_ID", "dbe5273a922045bc8005032f76ca5aac")
YANDEX_CLIENT_SECRET = os.environ.get("YANDEX_CLIENT_SECRET", "82f16ceb5eb4095bf456a523febedb9")
YANDEX_LINK_URL = os.environ.get(
    "YANDEX_LINK_URL",
    "https://yandex.ru/iot/iot/linking/7c169e54-a714-4398-9cf2-87f2bb868341/"
    "?app_build_number=unknown&app_id=unknown&app_platform=unknown&app_version=unknown&app_version_name=unknown"
    "&dp=2&lang=ru-RU&manufacturer=unknown&model=unknown&os_version=unknown&size=1080%2C1920&uuid=unknown"
)

YANDEX_TOKEN_TTL = int(os.environ.get("YANDEX_TOKEN_TTL", 3600))
YANDEX_REFRESH_TTL = int(os.environ.get("YANDEX_REFRESH_TTL", 30 * 24 * 3600))

app = Flask(__name__)
app.secret_key = APP_SECRET

# Очереди SSE-обновлений устройств
DEVICE_EVENT_SUBSCRIBERS = {}
DEVICE_EVENT_LOCK = threading.Lock()
SSE_PING_INTERVAL = 15


# Доступные режимы камина: устройства поддерживают шесть предустановок,
# поэтому генерируем список строковых значений от 1 до 6 для отображения в UI.
FIREPLACE_MODE_OPTIONS = [(str(i), str(i)) for i in range(1, 7)]
FIREPLACE_SOUND_OPTIONS = [(str(i), str(i)) for i in range(1, 4)]

# Поддерживаемые режимы сушильного шкафа для Алисы (шесть предустановок)
DRYER_PROGRAM_MODES = [
    ("one", "1"),
    ("two", "2"),
    ("three", "3"),
    ("four", "4"),
    ("five", "5"),
    ("six", "6"),
]

DRYER_PROGRAM_YA_TO_DEVICE = {ya: device for ya, device in DRYER_PROGRAM_MODES}
DRYER_PROGRAM_DEVICE_TO_YA = {device: ya for ya, device in DRYER_PROGRAM_MODES}
DRYER_PROGRAM_ALIASES = {
    "standard": DRYER_PROGRAM_MODES[0][1],
    "std": DRYER_PROGRAM_MODES[0][1],
}
DRYER_PROGRAM_DEFAULT_DEVICE = DRYER_PROGRAM_MODES[0][1]


def _normalize_dryer_program_device_value(value):
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    lower = raw.lower()
    if lower in DRYER_PROGRAM_ALIASES:
        return DRYER_PROGRAM_ALIASES[lower]
    if lower in DRYER_PROGRAM_YA_TO_DEVICE:
        return DRYER_PROGRAM_YA_TO_DEVICE[lower]
    if lower in DRYER_PROGRAM_DEVICE_TO_YA:
        return raw
    return None


def _dryer_program_device_to_yandex(value):
    normalized = _normalize_dryer_program_device_value(value)
    if not normalized:
        normalized = DRYER_PROGRAM_DEFAULT_DEVICE
    return DRYER_PROGRAM_DEVICE_TO_YA.get(normalized, DRYER_PROGRAM_MODES[0][0])


def _generate_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def _cleanup_expired_codes(con):
    now = _now_iso()
    con.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))


def _store_oauth_code(user_id: int, client_id: str, redirect_uri: str, scope=None, state=None):
    code = _generate_token(24)
    expires_at = (_now_utc() + timedelta(minutes=5)).isoformat()
    with db() as con:
        _cleanup_expired_codes(con)
        con.execute(
            "INSERT INTO oauth_codes (code, user_id, client_id, redirect_uri, scope, state, expires_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (code, user_id, client_id, redirect_uri, scope, state, expires_at, _now_iso()),
        )
    return code, expires_at


def _load_oauth_code(code: str):
    if not code:
        return None
    with db() as con:
        _cleanup_expired_codes(con)
        row = con.execute(
            "SELECT code, user_id, client_id, redirect_uri, scope, state, expires_at FROM oauth_codes WHERE code=?",
            (code,),
        ).fetchone()
        if not row:
            return None
        con.execute("DELETE FROM oauth_codes WHERE code=?", (code,))
    expires_at = _parse_iso(row["expires_at"])
    if not expires_at or expires_at < _now_utc():
        return None
    return dict(row)


def _create_or_update_yandex_tokens(user_id: int, client_id: str, scope=None, refresh_token=None,
                                    external_id=None):
    access_token = _generate_token(32)
    new_refresh_token = _generate_token(32)
    access_exp = (_now_utc() + timedelta(seconds=YANDEX_TOKEN_TTL)).isoformat()
    refresh_exp = (_now_utc() + timedelta(seconds=YANDEX_REFRESH_TTL)).isoformat()
    with db() as con:
        if refresh_token:
            row = con.execute(
                "SELECT id FROM yandex_tokens WHERE refresh_token=? AND user_id=?",
                (refresh_token, user_id),
            ).fetchone()
        else:
            row = None
        if row:
            con.execute(
                "UPDATE yandex_tokens SET access_token=?, refresh_token=?, scope=?, external_account_id=?,"
                " expires_at=?, refresh_expires_at=?, updated_at=? WHERE id=?",
                (
                    access_token,
                    new_refresh_token,
                    scope,
                    external_id,
                    access_exp,
                    refresh_exp,
                    _now_iso(),
                    row["id"],
                ),
            )
        else:
            con.execute(
                "INSERT INTO yandex_tokens (user_id, client_id, access_token, refresh_token, scope, external_account_id,"
                " expires_at, refresh_expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    client_id,
                    access_token,
                    new_refresh_token,
                    scope,
                    external_id,
                    access_exp,
                    refresh_exp,
                    _now_iso(),
                    _now_iso(),
                ),
            )
    return access_token, new_refresh_token, access_exp


def _get_user_id_from_bearer(auth_header: str):
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None, None
    token = auth_header.split(None, 1)[1].strip()
    if not token:
        return None, None
    with db() as con:
        row = con.execute(
            "SELECT id, user_id, expires_at, refresh_expires_at FROM yandex_tokens WHERE access_token=?",
            (token,),
        ).fetchone()
    if not row:
        return None, None
    expires_at = _parse_iso(row["expires_at"])
    if not expires_at or expires_at < _now_utc():
        return None, None
    refresh_exp = _parse_iso(row["refresh_expires_at"])
    if refresh_exp and refresh_exp < _now_utc():
        return None, None
    return row["user_id"], row["id"]


def _count_yandex_links(user_id: int) -> int:
    with db() as con:
        row = con.execute(
            "SELECT COUNT(*) AS cnt FROM yandex_tokens WHERE user_id=? AND refresh_expires_at>?",
            (user_id, _now_iso()),
        ).fetchone()
    return int(row["cnt"] if row else 0)


def _validate_yandex_client(client_id: str, client_secret=None) -> bool:
    if client_id != YANDEX_CLIENT_ID:
        return False
    if client_secret is not None and client_secret != YANDEX_CLIENT_SECRET:
        return False
    return True


def _append_query(url: str, params: dict) -> str:
    params = {k: v for k, v in params.items() if v is not None}
    if not params:
        return url
    separator = '&' if '?' in url else '?'
    return f"{url}{separator}{urlencode(params)}"


def _fetch_yandex_devices(user_id: int):
    with db() as con:
        rows = con.execute(
            "SELECT device_id, name, kind, on_state, program, state_json FROM devices WHERE user_id=?",
            (user_id,),
        ).fetchall()

    devices = []
    for row in rows:
        kind = (row["kind"] or "dryer").lower()
        if kind != "dryer":
            continue
        state_payload = _load_state_payload(row["state_json"])
        power_state = bool(row["on_state"])
        if "on" in state_payload:
            power_state = _bool_from_payload(state_payload.get("on"), power_state)

        program_value = state_payload.get("program") if isinstance(state_payload, dict) else None
        if program_value is None:
            program_value = row["program"]
        program_device_value = _normalize_dryer_program_device_value(program_value)
        if not program_device_value:
            program_device_value = DRYER_PROGRAM_DEFAULT_DEVICE
        devices.append({
            "id": row["device_id"],
            "name": row["name"] or "Сушильный шкаф",
            "online": bool(ONLINE.get(row["device_id"])),
            "power": power_state,
            "program": program_device_value,
        })
    return devices

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
            serial TEXT DEFAULT NULL,
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
        if "serial" not in cols:
            con.execute("ALTER TABLE devices ADD COLUMN serial TEXT DEFAULT NULL")

        con.execute("""
        CREATE TABLE IF NOT EXISTS oauth_codes (
            code TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            scope TEXT DEFAULT NULL,
            state TEXT DEFAULT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS yandex_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            client_id TEXT NOT NULL,
            access_token TEXT NOT NULL UNIQUE,
            refresh_token TEXT NOT NULL UNIQUE,
            scope TEXT DEFAULT NULL,
            external_account_id TEXT DEFAULT NULL,
            expires_at TEXT NOT NULL,
            refresh_expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """)
init_db()


def _now_utc():
    return datetime.utcnow()


def _now_iso():
    return _now_utc().isoformat()


def _parse_iso(dt: str):
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt)
    except ValueError:
        return None

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
def _subscribe_device_events(user_id: int):
    if not user_id:
        return None
    q = queue.Queue()
    with DEVICE_EVENT_LOCK:
        DEVICE_EVENT_SUBSCRIBERS.setdefault(user_id, set()).add(q)
    return q


def _unsubscribe_device_events(user_id: int, q):
    if not user_id or q is None:
        return
    with DEVICE_EVENT_LOCK:
        listeners = DEVICE_EVENT_SUBSCRIBERS.get(user_id)
        if not listeners:
            return
        listeners.discard(q)
        if not listeners:
            DEVICE_EVENT_SUBSCRIBERS.pop(user_id, None)


def _broadcast_device_event(user_id: int, kind: str, device_payload: dict):
    if not user_id or not device_payload:
        return
    with DEVICE_EVENT_LOCK:
        listeners = list(DEVICE_EVENT_SUBSCRIBERS.get(user_id, ()))
    if not listeners:
        return
    message = {"type": kind, "device": device_payload}
    for q in listeners:
        try:
            q.put_nowait(message)
        except queue.Full:
            continue


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
            SELECT device_id, serial, name, kind, on_state, program, state_json, last_seen, created_at
            FROM devices WHERE user_id=? ORDER BY created_at ASC
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
            "serial": row["serial"],
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


def _build_device_api_payload(row):
    dev_id = row["device_id"]
    kind = (row["kind"] or "dryer").lower()
    state_payload = _load_state_payload(row["state_json"])

    power_state = bool(row["on_state"])
    if "on" in state_payload:
        power_state = _bool_from_payload(state_payload.get("on"), power_state)

    device_info = {
        "device_id": dev_id,
        "name": row["name"],
        "kind": kind,
        "online": bool(ONLINE.get(dev_id)),
        "on": power_state,
        "program": row["program"],
        "last_seen": row["last_seen"],
        "created_at": row["created_at"],
        "state": state_payload,
        "serial": row["serial"],
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

    return device_info

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


def _load_device_payload_for_user(con, device_id: str, user_id: int):
    if not user_id:
        return None
    row = con.execute(
        """
        SELECT device_id, user_id, serial, name, kind, on_state, program, state_json, last_seen, created_at
        FROM devices WHERE device_id=?
        """,
        (device_id,),
    ).fetchone()
    if not row or row["user_id"] != user_id:
        return None
    return _build_device_api_payload(row)


# ---------- Обработчик апдейтов от MQTT (пишем состояние в БД) ----------
def _state_to_db(device_id: str, kind: str, payload):
    with db() as con:
        row = con.execute(
            "SELECT id, user_id FROM devices WHERE device_id=?",
            (device_id,),
        ).fetchone()
        if not row:
            return
        user_id = row["user_id"]
        if kind == "availability":
            if payload is True:
                con.execute("UPDATE devices SET last_seen=? WHERE device_id=?",
                            (datetime.utcnow().isoformat(), device_id))
            device_payload = _load_device_payload_for_user(con, device_id, user_id)
            if device_payload:
                _broadcast_device_event(user_id, kind, device_payload)
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
            device_payload = _load_device_payload_for_user(con, device_id, user_id)
            if device_payload:
                _broadcast_device_event(user_id, kind, device_payload)

register_state_handler(_state_to_db)

# ---------- OAuth 2.0 для Яндекс.Алисы ----------
@app.route("/callback", methods=["GET", "POST"])
def oauth_callback():
    if request.method == "POST":
        if request.mimetype == "application/json":
            body = request.get_json(silent=True) or {}
        else:
            body = request.form.to_dict() or {}
    else:
        body = request.args.to_dict() or {}

    code = body.get("code") or request.args.get("code")
    state = body.get("state") or request.args.get("state")

    if not code:
        app.logger.warning("/callback called without authorization code", extra={"state": state, "body": body})
        return jsonify(error="invalid_request", error_description="code_required"), 400

    code_payload = _load_oauth_code(code)
    if not code_payload:
        app.logger.warning("/callback received invalid or expired code", extra={"code": code, "state": state})
        return jsonify(error="invalid_grant"), 400

    client_id = code_payload.get("client_id") or body.get("client_id") or YANDEX_CLIENT_ID
    if not _validate_yandex_client(client_id):
        app.logger.warning(
            "/callback received code for unexpected client", extra={"client_id": client_id, "state": state}
        )
        return jsonify(error="unauthorized_client"), 401

    access_token, refresh_token, expires_at = _create_or_update_yandex_tokens(
        code_payload["user_id"],
        client_id,
        code_payload.get("scope"),
        external_id=code_payload.get("state"),
    )

    response_payload = {
        "status": "ok",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": YANDEX_TOKEN_TTL,
        "state": state or code_payload.get("state") or "",
    }

    return jsonify(response_payload)


@app.route("/oauth/authorize", methods=["GET", "POST"])
def oauth_authorize():
    response_type = request.values.get("response_type", "code").lower()
    client_id = request.values.get("client_id", "")
    redirect_uri = request.values.get("redirect_uri", "")
    scope = request.values.get("scope")
    state = request.values.get("state")
    external_id = request.values.get("yandexuid") or request.values.get("external_user_id")

    if response_type != "code":
        return jsonify(error="unsupported_response_type"), 400
    if not redirect_uri:
        return jsonify(error="invalid_request", error_description="redirect_uri_required"), 400
    if not _validate_yandex_client(client_id):
        return jsonify(error="unauthorized_client"), 401

    if not g.user:
        next_url = request.full_path.rstrip("?") if request.query_string else request.path
        if not next_url.startswith("/"):
            next_url = "/"
        session["post_login_redirect"] = next_url
        return redirect(url_for("index") + "#login")

    if request.method == "POST":
        decision = request.form.get("decision")
        if decision == "approve":
            code, _ = _store_oauth_code(g.user["id"], client_id, redirect_uri, scope, external_id or state)
            session.pop("post_login_redirect", None)
            return redirect(_append_query(redirect_uri, {"code": code, "state": state}))
        else:
            return redirect(_append_query(redirect_uri, {"error": "access_denied", "state": state}))

    return render_template(
        "oauth_authorize.html",
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        state=state,
        external_id=external_id,
        user=g.user,
    )


def _extract_basic_credentials(auth_header: str):
    if not auth_header or not auth_header.lower().startswith("basic "):
        return None, None
    try:
        decoded = base64.b64decode(auth_header.split(None, 1)[1]).decode("utf-8")
    except Exception:
        return None, None
    if ":" not in decoded:
        return None, None
    client_id, client_secret = decoded.split(":", 1)
    return client_id, client_secret


@app.post("/oauth/token")
def oauth_token():
    if request.mimetype == "application/json":
        data = request.get_json(silent=True) or {}
    else:
        data = request.form.to_dict() or {}

    header_client_id, header_client_secret = _extract_basic_credentials(request.headers.get("Authorization"))
    client_id = header_client_id or data.get("client_id", "")
    client_secret = header_client_secret or data.get("client_secret")

    if not _validate_yandex_client(client_id, client_secret):
        return jsonify(error="invalid_client"), 401

    grant_type = (data.get("grant_type") or "").lower()
    if grant_type == "authorization_code":
        code = data.get("code")
        if not code:
            return jsonify(error="invalid_request", error_description="code_required"), 400
        code_payload = _load_oauth_code(code)
        if not code_payload or code_payload.get("client_id") != client_id:
            return jsonify(error="invalid_grant"), 400
        user_id = code_payload["user_id"]
        scope = code_payload.get("scope")
        external_id = code_payload.get("state")
        access_token, refresh_token, _ = _create_or_update_yandex_tokens(
            user_id, client_id, scope, external_id=external_id
        )
        return jsonify(
            access_token=access_token,
            token_type="bearer",
            expires_in=YANDEX_TOKEN_TTL,
            refresh_token=refresh_token,
            scope=scope or "",
        )

    if grant_type == "refresh_token":
        refresh_token = data.get("refresh_token")
        if not refresh_token:
            return jsonify(error="invalid_request", error_description="refresh_token_required"), 400
        with db() as con:
            row = con.execute(
                "SELECT user_id, scope, external_account_id, refresh_expires_at FROM yandex_tokens"
                " WHERE refresh_token=? AND client_id=?",
                (refresh_token, client_id),
            ).fetchone()
        if not row:
            return jsonify(error="invalid_grant"), 400
        refresh_exp = _parse_iso(row["refresh_expires_at"])
        if refresh_exp and refresh_exp < _now_utc():
            return jsonify(error="invalid_grant"), 400
        access_token, new_refresh_token, _ = _create_or_update_yandex_tokens(
            row["user_id"], client_id, row["scope"], refresh_token=refresh_token,
            external_id=row["external_account_id"],
        )
        return jsonify(
            access_token=access_token,
            token_type="bearer",
            expires_in=YANDEX_TOKEN_TTL,
            refresh_token=new_refresh_token,
            scope=row["scope"] or "",
        )

    return jsonify(error="unsupported_grant_type"), 400


def _yandex_unauthorized():
    return jsonify(error="invalid_token"), 401


def _yandex_response(request_id, payload, status=200):
    return jsonify({
        "request_id": request_id or "",
        "payload": payload,
    }), status


@app.route("/yandex/v1.0/user/devices", methods=["POST"])
@app.route("/v1.0/user/devices", methods=["POST", "GET"])
def yandex_devices():
    user_id, _ = _get_user_id_from_bearer(request.headers.get("Authorization"))
    if not user_id:
        return _yandex_unauthorized()

    body = request.get_json(silent=True) or {}
    request_id = body.get("request_id") or body.get("requestId")

    devices = []
    for device in _fetch_yandex_devices(user_id):
        devices.append({
            "id": device["id"],
            "name": device["name"],
            "type": "devices.types.other",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "retrievable": True,
                },
                {
                    "type": "devices.capabilities.mode",
                    "retrievable": True,
                    "parameters": {
                        "instance": "program",
                        "modes": [{"value": ya_value} for ya_value, _ in DRYER_PROGRAM_MODES],
                    },
                },
            ],
            "properties": [],
        })

    payload = {
        "user_id": str(user_id),
        "devices": devices,
    }
    return _yandex_response(request_id, payload)


@app.route("/yandex/v1.0/user/devices/query", methods=["POST"])
@app.route("/v1.0/user/devices/query", methods=["POST"])
def yandex_devices_query():
    user_id, _ = _get_user_id_from_bearer(request.headers.get("Authorization"))
    if not user_id:
        return _yandex_unauthorized()

    body = request.get_json(silent=True) or {}
    request_id = body.get("request_id") or body.get("requestId")
    requested_devices = body.get("payload", {}).get("devices") or []

    devices = {d["id"]: d for d in _fetch_yandex_devices(user_id)}

    results = []
    for item in requested_devices:
        dev_id = item.get("id")
        current = devices.get(dev_id)
        if not current:
            results.append({
                "id": dev_id,
                "error_code": "DEVICE_UNREACHABLE",
                "error_message": "Устройство не найдено",
            })
            continue
        program_state_value = _dryer_program_device_to_yandex(current.get("program"))
        results.append({
            "id": current["id"],
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "state": {
                        "instance": "on",
                        "value": bool(current["power"]),
                    },
                },
                {
                    "type": "devices.capabilities.mode",
                    "state": {
                        "instance": "program",
                        "value": program_state_value,
                    },
                },
            ],
            "properties": [],
        })

    return _yandex_response(request_id, {"devices": results})


@app.route("/yandex/v1.0/user/devices/action", methods=["POST"])
@app.route("/v1.0/user/devices/action", methods=["POST"])
def yandex_devices_action():
    user_id, _ = _get_user_id_from_bearer(request.headers.get("Authorization"))
    if not user_id:
        return _yandex_unauthorized()

    body = request.get_json(silent=True) or {}
    request_id = body.get("request_id") or body.get("requestId")
    payload_devices = body.get("payload", {}).get("devices") or []

    devices = {d["id"]: d for d in _fetch_yandex_devices(user_id)}
    results = []

    for item in payload_devices:
        dev_id = item.get("id")
        current = devices.get(dev_id)
        device_result = {"id": dev_id, "capabilities": []}
        if not current:
            device_result["capabilities"].append({
                "type": "devices.capabilities.on_off",
                "state": {
                    "instance": "on",
                    "action_result": {
                        "status": "ERROR",
                        "error_code": "DEVICE_NOT_FOUND",
                    },
                },
            })
            results.append(device_result)
            continue

        caps = item.get("capabilities") or []
        for cap in caps:
            cap_type = cap.get("type")
            cap_state = cap.get("state") or {}
            instance = cap_state.get("instance")

            if cap_type == "devices.capabilities.on_off" and instance in (None, "on"):
                desired = cap_state.get("value")
                desired_bool = _bool_from_payload(desired, False)
                if not current["online"]:
                    device_result["capabilities"].append({
                        "type": "devices.capabilities.on_off",
                        "state": {
                            "instance": "on",
                            "action_result": {
                                "status": "ERROR",
                                "error_code": "DEVICE_UNREACHABLE",
                            },
                        },
                    })
                    continue

                try:
                    bridge.publish_cmd(dev_id, {"on": desired_bool})
                    _update_device_field(dev_id, "on_state", 1 if desired_bool else 0)
                    device_result["capabilities"].append({
                        "type": "devices.capabilities.on_off",
                        "state": {
                            "instance": "on",
                            "action_result": {
                                "status": "DONE",
                            },
                        },
                    })
                except Exception:
                    device_result["capabilities"].append({
                        "type": "devices.capabilities.on_off",
                        "state": {
                            "instance": "on",
                            "action_result": {
                                "status": "ERROR",
                                "error_code": "INTERNAL_ERROR",
                            },
                        },
                    })
            elif cap_type == "devices.capabilities.mode" and instance == "program":
                desired_value = cap_state.get("value")
                if not isinstance(desired_value, str):
                    device_result["capabilities"].append({
                        "type": "devices.capabilities.mode",
                        "state": {
                            "instance": "program",
                            "action_result": {
                                "status": "ERROR",
                                "error_code": "INVALID_VALUE",
                            },
                        },
                    })
                    continue

                desired_device_value = DRYER_PROGRAM_YA_TO_DEVICE.get(desired_value.lower())
                if not desired_device_value:
                    device_result["capabilities"].append({
                        "type": "devices.capabilities.mode",
                        "state": {
                            "instance": "program",
                            "action_result": {
                                "status": "ERROR",
                                "error_code": "INVALID_VALUE",
                            },
                        },
                    })
                    continue

                if not current["online"]:
                    device_result["capabilities"].append({
                        "type": "devices.capabilities.mode",
                        "state": {
                            "instance": "program",
                            "action_result": {
                                "status": "ERROR",
                                "error_code": "DEVICE_UNREACHABLE",
                            },
                        },
                    })
                    continue

                try:
                    bridge.publish_cmd(dev_id, {"program": desired_device_value})
                    _update_device_field(dev_id, "program", desired_device_value)
                    device_result["capabilities"].append({
                        "type": "devices.capabilities.mode",
                        "state": {
                            "instance": "program",
                            "action_result": {
                                "status": "DONE",
                            },
                        },
                    })
                except Exception:
                    device_result["capabilities"].append({
                        "type": "devices.capabilities.mode",
                        "state": {
                            "instance": "program",
                            "action_result": {
                                "status": "ERROR",
                                "error_code": "INTERNAL_ERROR",
                            },
                        },
                    })
            else:
                device_result["capabilities"].append({
                    "type": cap_type,
                    "state": {
                        "instance": instance or "on",
                        "action_result": {
                            "status": "ERROR",
                            "error_code": "INVALID_ACTION",
                        },
                    },
                })

        results.append(device_result)

    return _yandex_response(request_id, {"devices": results})


@app.post("/yandex/v1.0/user/unlink")
def yandex_unlink():
    body = request.get_json(silent=True) or {}
    user_id, _ = _get_user_id_from_bearer(request.headers.get("Authorization"))
    if not user_id:
        # fallback to payload user_id
        payload_user = body.get("payload", {}).get("user_id") if isinstance(body.get("payload"), dict) else None
        if payload_user:
            try:
                user_id = int(str(payload_user).split(":", 1)[0])
            except ValueError:
                user_id = None
    if not user_id:
        return _yandex_unauthorized()

    with db() as con:
        con.execute("DELETE FROM yandex_tokens WHERE user_id=?", (user_id,))

    request_id = body.get("request_id") or body.get("requestId")
    return _yandex_response(request_id, {"status": "OK"})


@app.post("/yandex/unlink_all")
@login_required
def yandex_unlink_all_ui():
    with db() as con:
        con.execute("DELETE FROM yandex_tokens WHERE user_id=?", (g.user["id"],))
    flash("Все аккаунты Яндекс.Алисы отвязаны.")
    return redirect(url_for("devices"))


# ---------- РОУТЫ ----------
@app.get("/")
def index():
    devices = fetch_user_devices(g.user["id"]) if g.user else []
    yandex_count = _count_yandex_links(g.user["id"]) if g.user else 0
    return render_template(
        "index.html",
        user=g.user,
        devices=devices,
        year=datetime.now().year,
        yandex_account_count=yandex_count,
        yandex_link_url=YANDEX_LINK_URL,
    )

@app.get("/devices")
@login_required
def devices():
    devices = fetch_user_devices(g.user["id"]) if g.user else []
    yandex_count = _count_yandex_links(g.user["id"]) if g.user else 0
    return render_template(
        "index.html",
        user=g.user,
        devices=devices,
        year=datetime.now().year,
        yandex_account_count=yandex_count,
        yandex_link_url=YANDEX_LINK_URL,
    )

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
    next_url = session.pop("post_login_redirect", None)
    if next_url and next_url.startswith("/"):
        return redirect(next_url, code=303)
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
    next_url = session.pop("post_login_redirect", None)
    if next_url and next_url.startswith("/"):
        return redirect(next_url, code=303)
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
            SELECT device_id, serial, name, kind, on_state, program, state_json, last_seen, created_at
            FROM devices WHERE user_id=? ORDER BY created_at ASC
        """, (g.user["id"],)).fetchall()
    devices = [_build_device_api_payload(r) for r in rows]
    return jsonify(ok=True, devices=devices)


@app.get("/api/devices/stream")
@login_required
def api_devices_stream():
    user_id = g.user["id"]
    q = _subscribe_device_events(user_id)
    if q is None:
        return jsonify(ok=False, message="stream_not_available"), 503

    def generator():
        yield ": connected\n\n"
        try:
            while True:
                try:
                    item = q.get(timeout=SSE_PING_INTERVAL)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if not item:
                    continue
                payload = json.dumps(item, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        finally:
            _unsubscribe_device_events(user_id, q)

    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "X-Accel-Buffering": "no",
    }
    return Response(stream_with_context(generator()), mimetype="text/event-stream", headers=headers)

@app.post("/api/devices/pair")
@login_required
def api_devices_pair():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    serial = (data.get("serial") or "").strip().upper() or None
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
                INSERT INTO devices (user_id, device_id, name, kind, serial, on_state, program, last_seen, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                g.user["id"],
                device_id,
                "Сушильный шкаф",
                "dryer",
                serial,
                0,
                "standard",
                None,
                datetime.utcnow().isoformat()
            ))
        else:
            update_fields = ["user_id=?", "kind=?", "name=?", "program=?"]
            values = [g.user["id"], "dryer", "Сушильный шкаф", "standard"]
            if serial:
                update_fields.append("serial=?")
                values.append(serial)
            values.append(device_id)
            sql = f"UPDATE devices SET {', '.join(update_fields)} WHERE device_id=?"
            con.execute(sql, tuple(values))

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
                INSERT INTO devices (user_id, device_id, name, kind, serial, on_state, program, last_seen, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                g.user["id"],
                device_id,
                device_name,
                kind,
                serial,
                0,
                program_default,
                None,
                datetime.utcnow().isoformat()
            ))
        except sqlite3.IntegrityError:
            # На случай гонки добавления — обновляем владельца и параметры
            con.execute(
                "UPDATE devices SET user_id=?, kind=?, name=?, program=?, serial=? WHERE device_id=?",
                (g.user["id"], kind, device_name, program_default, serial, device_id),
            )

    return jsonify(ok=True, device_id=device_id, kind=kind, name=device_name, serial=serial)

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


def _update_device_state_json_fields(device_id: str, updates: dict):
    if not updates:
        return

    with db() as con:
        row = con.execute(
            "SELECT state_json FROM devices WHERE device_id=?",
            (device_id,),
        ).fetchone()

        if not row:
            return

        state_payload = _load_state_payload(row["state_json"])
        changed = False

        for key, value in updates.items():
            if value is None:
                if key in state_payload:
                    del state_payload[key]
                    changed = True
                continue

            if state_payload.get(key) != value:
                state_payload[key] = value
                changed = True

        if not changed:
            return

        con.execute(
            "UPDATE devices SET state_json=? WHERE device_id=?",
            (json.dumps(state_payload, ensure_ascii=False), device_id),
        )


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
    _update_device_state_json_fields(device_id, {"on": on_state})
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

    _update_device_state_json_fields(device_id, {key: value})

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
