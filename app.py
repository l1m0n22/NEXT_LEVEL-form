import os
import re
import time
import html
import requests
from threading import Thread
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, Response

# ====== Настройки / окружение ======
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")  # @groupusername или -100...

if not BOT_TOKEN:
    raise RuntimeError("В .env нет TELEGRAM_BOT_TOKEN")
if not ADMIN_CHAT_ID:
    raise RuntimeError("В .env нет TELEGRAM_ADMIN_CHAT_ID")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ====== Flask ======
app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

PHONE_RE = re.compile(r"^[0-9+()\s\-]{7,20}$", re.UNICODE)

@app.get("/favicon.ico")
def favicon():
    return Response(status=204)

# ====== Валидация формы ======
def validate(data: dict) -> dict:
    errors = {}

    def need(k):
        v = (data.get(k) or "").strip()
        return v, (len(v) == 0)

    first, empty = need("firstName")
    if empty or len(first) < 2:
        errors["firstName"] = "Имя обязательно и не короче 2 символов."
    last, empty = need("lastName")
    if empty or len(last) < 2:
        errors["lastName"] = "Фамилия обязательна и не короче 2 символов."
    middle = (data.get("middleName") or "").strip()
    if middle and len(middle) < 2:
        errors["middleName"] = "Отчество либо пусто, либо от 2 символов."
    phone, empty = need("phone")
    if empty or not PHONE_RE.match(phone):
        errors["phone"] = "Укажите корректный телефон."
    email, empty = need("email")
    if empty or "@" not in email or "." not in email:
        errors["email"] = "Укажите корректный e-mail."
    about, empty = need("about")
    if empty or len(about) < 20 or len(about) > 600:
        errors["about"] = "Поле «О себе»: 20–600 символов."

    return errors

# ====== Хелперы Telegram ======
def _normalize_chat_id(val: str):
    """Принимает '@groupname' либо числовой ID (-100...) и приводит к виду для sendMessage."""
    val = str(val).strip()
    return val if val.startswith("@") else int(val)

def tg_get(method: str, **params):
    r = requests.get(f"{API}/{method}", params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data["result"]

def tg_post(method: str, payload: dict):
    r = requests.post(f"{API}/{method}", json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data["result"]

def tg_send_message(chat_id, text, parse_mode=None):
    body = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        body["parse_mode"] = parse_mode
    return tg_post("sendMessage", body)

# Сообщение с анкеты -> в указанный чат (группа/ЛС/канал)
def send_form_to_telegram(data: dict) -> None:
    esc = lambda s: html.escape((s or "").strip(), quote=True)

    lines = [
        "<b>Yangi ariza</b>",
        f"F.I.O.: <b>{esc(data.get('firstName'))}</b>",
        f"Telefon: {esc(data.get('phone'))}",
        f"Email: {esc(data.get('email'))}",
    ]

    # необязательные поля — добавляем, только если заполнены
    opt = [
        ("Yoshi", "age"),
        ("Shahar / yashash joyi", "city"),
        ("Instagram", "instagram"),
        ("Telegram", "telegram"),
        ("TikTok", "tiktok"),
        ("YouTube", "youtube"),
        ("Obunachilar soni", "subs"),
        ("Kontent yo‘nalishi", "niche"),
    ]
    for label, key in opt:
        val = (data.get(key) or "").strip()
        if val:
            lines.append(f"{label}: {esc(val)}")

    lines.append("Nega biz bilan ishlashni xohlaysiz?")
    lines.append(esc(data.get("about")))

    text = "\n".join(lines)

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": _normalize_chat_id(ADMIN_CHAT_ID),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=10)
    r.raise_for_status()

# ====== Обработчики Flask ======
@app.get("/")
def index():
    return render_template("index.html")

@app.post("/submit")
def submit():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    errors = validate(data)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    try:
        send_form_to_telegram(data)
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"Ошибка Telegram: {e}"}), 502
    return jsonify({"ok": True})

# ====== Встроенный /chatid (long polling) ======
def poll_updates():
    """Фоновый поток: слушает обновления и отвечает на /chatid."""
    try:
        me = tg_get("getMe")
        bot_username = me.get("username", "")
        print(f"TG poller запущен. Бот @{bot_username}. Напишите в группе /chatid")
    except Exception as e:
        print("Не удалось вызвать getMe:", e)
        bot_username = ""

    offset = None
    while True:
        try:
            updates = tg_get(
                "getUpdates",
                timeout=50,
                offset=offset,
                allowed_updates=["message", "channel_post"]
            )
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("channel_post")
                if not msg:
                    continue

                chat = msg["chat"]
                text = (msg.get("text") or "").strip()

                is_cmd = (
                    text.startswith("/chatid") or
                    (bot_username and text.startswith(f"/chatid@{bot_username}")) or
                    text.startswith("/id")
                )
                if not is_cmd:
                    continue

                parts = [
                    f"chat.id = {chat['id']}",
                    f"chat.type = {chat.get('type')}"
                ]
                if chat.get("title"):
                    parts.append(f"chat.title = {chat['title']}")
                if chat.get("username"):
                    parts.append(f"chat.username = @{chat['username']}")

                user = msg.get("from", {})
                sender = f"{user.get('first_name','')} {user.get('last_name','')}".strip()
                parts.append(f"from.id = {user.get('id')} ({sender or '—'} @{user.get('username','')})")

                env_hint = f"@{chat['username']}" if chat.get("username") else str(chat["id"])
                parts.append("\nВ .env можно записать:\nTELEGRAM_ADMIN_CHAT_ID=" + env_hint)

                reply = "\n".join(parts)
                try:
                    tg_send_message(chat["id"], reply)
                except Exception as e:
                    print("Ошибка отправки ответа в чат:", e)
        except Exception as e:
            # Ошибки сети/таймауты — просто подождём и продолжим
            print("TG poller error:", e)
            time.sleep(2)

# ====== Точка входа ======
if __name__ == "__main__":
    # Запускаем Telegram-поллер в фоне (для /chatid)
    #Thread(target=poll_updates, daemon=True).start()

    # Запускаем веб-сервер
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
