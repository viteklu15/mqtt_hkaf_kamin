from datetime import datetime
from flask import Flask, render_template, redirect, url_for, request, session, flash

app = Flask(__name__)
app.secret_key = "change-me"

# Заглушка текущего юзера
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return {"email": session.get("email")}

@app.context_processor
def inject_globals():
    return {
        "year": datetime.now().year,
        "user": current_user()
    }

@app.get("/")
def index():
    if current_user():
        # devices можно подтянуть из БД
        devices = []
        return render_template("pages/dashboard.html", devices=devices)
    return render_template("pages/home.html")

@app.post("/login")
def login():
    email = request.form.get("email")
    # здесь проверяешь пароль и т.п.
    session["uid"] = "1"
    session["email"] = email
    flash("Вы успешно вошли.")
    return redirect(url_for("index"))

@app.post("/register")
def register():
    # создаёшь пользователя в БД
    flash("Аккаунт создан, можно войти.")
    return redirect(url_for("index") + "#login")

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))
