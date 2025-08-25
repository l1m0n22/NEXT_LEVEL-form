import os
import re
import html
import mimetypes
import json
import hmac
import hashlib
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, Response

# --- Env: отдельные переменные для бота формы ---
load_dotenv()
FORM_BOT_TOKEN = os.getenv("FORM_BOT_TOKEN")
FORM_CHAT_ID   = os.getenv("FORM_CHAT_ID")   # @groupusername или -100...

# URL вебхука бота-воронки:
# 1) задаёшь напрямую FUNNEL_SUBMIT_URL, например: https://my-funnel.onrender.com/submitted
#    или
# 2) задаёшь FUNNEL_BASE_URL, тогда возьмём {FUNNEL_BASE_URL}/submitted
FUNNEL_SUBMIT_URL = os.getenv("FUNNEL_SUBMIT_URL")
if not FUNNEL_SUBMIT_URL:
    base = os.getenv("FUNNEL_BASE_URL")
    if base:
        FUNNEL_SUBMIT_URL = base.rstrip("/") + "/submitted"
# Секрет для подписи HMAC (должен совпадать с тем, что в боте-воронке)
FUNNEL_SIGNING_SECRET = os.getenv("FUNNEL_SIGNING_SECRET", "")

if not FORM_BOT_TOKEN or not FORM_CHAT_ID:
    raise RuntimeError("Задайте FORM_BOT_TOKEN и FORM_CHAT_ID в .env / Render env")

API = f"https://api.telegram.org/bot{FORM_BOT_TOKEN}"

# --- Flask ---
app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
# защита от слишком больших аплоадов (чуть больше лимита Телеграма на фото ~10MB)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

PHONE_RE = re.compile(r"^[0-9+()\s\-]{7,20}$", re.UNICODE)
MAX_PHOTO_SIZE = 10 * 1024 * 1024  # лимит для sendPhoto
ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
ALLOWED_EXTS  = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


# --- Exceptions ---
class ChatMigrated(Exception):
    """Поднявается когда Telegram сообщает migrate_to_chat_id (group -> supergroup)."""
    def __init__(self, new_chat_id: int, payload: dict | None = None):
        super().__init__(f"Chat migrated to {new_chat_id}")
        self.new_chat_id = int(new_chat_id)
        self.payload = payload or {}


# --- Helpers ---
def _normalize_chat_id(val: str | int):
    """Вернёт '@group' или числовой int(-100...) как требует Telegram API."""
    s = str(val).strip()
    return s if s.startswith("@") else int(s)

def _file_allowed(file):
    """
    Проверяет файл (если есть): MIME/расширение и размер.
    Возвращает (ok: bool, message: str)
    """
    if not file or not getattr(file, "filename", ""):
        return True, ""

    # MIME/расширение
    mime = (file.mimetype or "").lower()
    if mime not in ALLOWED_MIMES:
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in ALLOWED_EXTS:
            return False, "Ruxsat etilgan formatlar: JPG/PNG/WEBP/GIF."
        # попытка угадать по расширению
        guessed, _ = mimetypes.guess_type(file.filename)
        if guessed:
            mime = (guessed or "").lower()

    # размер (не вычитывая содержимое)
    size = None
    if getattr(file, "content_length", None):
        size = file.content_length
    else:
        try:
            cur = file.stream.tell()
            file.stream.seek(0, os.SEEK_END)
            size = file.stream.tell()
            file.stream.seek(cur, os.SEEK_SET)
        except Exception:
            size = None

    if size and size > MAX_PHOTO_SIZE:
        return False, "Rasm hajmi 10 MB dan oshmasin."

    return True, ""

def build_caption(data: dict) -> str:
    esc = lambda s: html.escape((s or "").strip(), quote=True)
    lines = [
        "<b>Yangi ariza</b>",
        f"F.I.O.: <b>{esc(data.get('firstName'))}</b>",
        f"Telefon: {esc(data.get('phone'))}",
        f"Email: {esc(data.get('email'))}",
    ]
    for label, key in [
        ("Yoshi", "age"),
        ("Shahar / yashash joyi", "city"),
        ("Instagram", "instagram"),
        ("Telegram", "telegram"),
        ("TikTok", "tiktok"),
        ("YouTube", "youtube"),
        ("Obunachilar soni", "subs"),
        ("Kontent yo‘nalishi", "niche"),
    ]:
        val = (data.get(key) or "").strip()
        if val:
            lines.append(f"{label}: {esc(val)}")

    lines.append("Nega biz bilan ishlashni xohlaysiz?")
    lines.append(esc(data.get("about")))
    return "\n".join(lines)

def _tg_request(method: str, *, data=None, json=None, files=None, timeout=30):
    """
    POST к Telegram + явная проверка ok:false для понятных ошибок.
    Специально ловим migrate_to_chat_id и бросаем ChatMigrated с new_chat_id.
    """
    r = requests.post(f"{API}/{method}", data=data, json=json, files=files, timeout=timeout)
    # если не JSON — кинем HTTP ошибку
    try:
        payload = r.json()
    except Exception:
        r.raise_for_status()
        return
    if not payload.get("ok", False):
        desc = payload.get("description") or "unknown"
        params = payload.get("parameters") or {}
        migrate_to = params.get("migrate_to_chat_id")
        if migrate_to:
            # Телеграм сообщает новый chat_id (supergroup)
            raise ChatMigrated(migrate_to, payload)
        raise RuntimeError(f"Telegram API error: {desc} | method={method} | payload={payload}")
    return payload.get("result")

def validate(data: dict, file=None) -> dict:
    """Проверка обязательных полей + (необяз.) фото."""
    errors = {}

    def need(k):
        v = (data.get(k) or "").strip()
        return v, (len(v) == 0)

    first, empty = need("firstName")
    if empty or len(first) < 2:
        errors["firstName"] = "Iltimos, to‘liq ismingizni yozing (kamida 2 belgi)."

    phone, empty = need("phone")
    if empty or not PHONE_RE.match(phone):
        errors["phone"] = "Telefon raqamini to‘g‘ri kiriting."

    email, empty = need("email")
    if empty or "@" not in email or "." not in email:
        errors["email"] = "To‘g‘ri email manzilini kiriting."

    about, empty = need("about")
    if empty or len(about) < 20 or len(about) > 600:
        errors["about"] = "1–2 jumla (20–600 belgi)."

    ok, msg = _file_allowed(file)
    if not ok:
        errors["photo"] = msg

    return errors

def send_to_telegram(data: dict, photo_file=None) -> None:
    """
    Если есть файл — sendPhoto, иначе sendMessage.
    При ошибке 'group chat was upgraded to a supergroup chat' делаем ретрай на новый chat_id.
    """
    global FORM_CHAT_ID  # чтобы обновить на лету и дальше использовать новый id
    chat_id = _normalize_chat_id(FORM_CHAT_ID)
    caption = build_caption(data)

    def _send_photo(_chat_id):
        # Гарантируем начало стрима
        if photo_file and getattr(photo_file, "filename", ""):
            try:
                photo_file.stream.seek(0)
            except Exception:
                pass
            return _tg_request(
                "sendPhoto",
                data={"chat_id": _chat_id, "caption": caption, "parse_mode": "HTML"},
                files={
                    "photo": (
                        photo_file.filename,
                        photo_file.stream,
                        photo_file.mimetype or "application/octet-stream"
                    )
                },
                timeout=30,
            )
        else:
            return _tg_request(
                "sendMessage",
                json={"chat_id": _chat_id, "text": caption, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=15,
            )

    try:
        _send_photo(chat_id)
    except ChatMigrated as cm:
        # Ретрай на новый chat_id (формат int -100...)
        new_id = cm.new_chat_id
        app.logger.info(f"[telegram] Chat migrated -> retry with {new_id}")
        _send_photo(new_id)
        # Обновляем глобалку, чтобы следующие отправки уже шли в супергруппу
        FORM_CHAT_ID = str(new_id)
    # Остальные исключения пусть пробросятся наружу (обработаются в submit)

def notify_funnel_if_any(chat_id_str: str, form_data: dict):
    """Шлём уведомление боту-воронке о том, что заявка подана (если сконфигурирован вебхук и есть c)."""
    if not chat_id_str or not FUNNEL_SUBMIT_URL:
        return
    payload = {
        "chat_id": chat_id_str.strip(),
        "event": "form_submitted",
        # чуть контекста — по желанию
        "firstName": form_data.get("firstName"),
        "phone": form_data.get("phone"),
        "email": form_data.get("email"),
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if FUNNEL_SIGNING_SECRET:
        sig = hmac.new(FUNNEL_SIGNING_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Signature-256"] = f"sha256={sig}"]
    try:
        r = requests.post(FUNNEL_SUBMIT_URL, data=body, headers=headers, timeout=5)
        r.raise_for_status()
    except Exception as e:
        # не роняем ответ пользователю, просто логируем
        app.logger.warning(f"Funnel webhook failed: {e}")

# --- Routes ---
@app.get("/favicon.ico")
def favicon():
    return Response(status=204)

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/")
def index():
    return render_template("index.html")

@app.post("/submit")
def submit():
    # фронт шлёт multipart/form-data (FormData), тогда файл в request.files['photo']
    if request.form or request.files:
        data = request.form.to_dict()
        photo = request.files.get("photo")
    else:
        data = request.get_json(silent=True) or {}
        photo = None

    errors = validate(data, photo)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    try:
        # 1) в админ-чат
        send_to_telegram(data, photo)
        # 2) в бота-воронку (если пришёл chat_id в скрытом поле c и настроен вебхук)
        notify_funnel_if_any(data.get("c"), data)
    except Exception as e:
        app.logger.exception("Telegram/Funnel send failed")
        return jsonify({"ok": False, "error": f"Ошибка отправки: {e}"}), 502

    return jsonify({"ok": True})


if __name__ == "__main__":
    # Локальный запуск: python app.py
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
