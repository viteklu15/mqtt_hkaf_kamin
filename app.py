from flask import Flask, request, redirect, url_for, session, render_template, flash, g
import sqlite3, os
from datetime import datetime
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

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
            # нет логина — отправляем на главную и открываем форму входа
            return redirect(url_for("index") + "#login")
        return fn(*args, **kwargs)
    return wrapper

# ---------- РОУТЫ ----------
@app.get("/")
def index():
    # Ваш index.html уже умеет показывать «гостевой экран» или «кабинет» по наличию user
    return render_template("index.html", user=g.user, year=datetime.now().year)

@app.get("/devices")
@login_required
def devices():
    # Можно отрисовывать тот же шаблон: в нём есть ветка {% else %} с «Кабинет»
    return render_template("index.html", user=g.user, year=datetime.now().year)

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

    # сразу логиним и ведём на страницу устройств
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
    # после логина — на устройства
    return redirect(url_for("devices"), code=303)

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(debug=True)
