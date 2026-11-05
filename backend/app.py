"""
نوا (Nava) — backend API
A small, self-contained Flask + SQLite backend for the Nava social app.

Run locally:
    pip install -r requirements.txt
    python app.py

Environment variables (see ../.env.example):
    SECRET_KEY   - Flask session signing key. MUST be changed for real deployment.
    PORT         - port to listen on (default 5000)
    DEBUG        - "1" to enable Flask debug mode (never use in production)
"""
import os
import base64
import hashlib
import hmac
import json
import re
import io
import time
import uuid
import sqlite3
import threading
from datetime import datetime, timezone

from flask import (
    Flask, request, jsonify, session, send_from_directory, g, abort, redirect
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    HAS_PIL = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "nava.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 90 * 1024 * 1024  # 90 MB hard ceiling per request
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")
TAG_RE = re.compile(r"#[\w\u0600-\u06FF\u200c]+", re.UNICODE)
MENTION_RE = re.compile(r"@([a-z0-9_]{3,20})", re.IGNORECASE)

IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}
VIDEO_EXTS = {"mp4", "webm", "mov", "m4v"}
AUDIO_EXTS = {"mp3", "wav", "ogg", "m4a", "webm", "aac"}
MAX_IMAGE = 8 * 1024 * 1024
MAX_VIDEO = 80 * 1024 * 1024
MAX_AUDIO = 30 * 1024 * 1024

DEMO_BOTS = {"sara", "arash", "negar", "kaveh", "mahsa"}
BOT_REPLIES = {
    "sara":  ["سلام! خوشحال شدم پیام دادی ☕", "چه جالب! بیشتر بگو.", "فردا صبح یک قهوه با هم بخوریم؟", "عکسش را برایم بفرست."],
    "arash": ["سلام، بفرما! چه کمکی از من برمی‌آید؟", "این را باید یک نگاه کنم؛ بعداً جواب می‌دهم.", "به نظرم راهش ساده‌تر از این حرف‌هاست 🙂", "عالی بود، دستت درد نکند."],
    "negar": ["سلام! ممنون از لطفت.", "این جمله را باید یادداشت کنم.", "چند روز فرصت بده، برایت می‌نویسم.", "چه فکر قشنگی."],
    "kaveh": ["سلام رفیق! قطعه‌ی جدید را شنیدی؟", "جمعه شب بیا، برایت جا نگه می‌دارم.", "ممنون، انرژی گرفتم 🎶", "یک ساز جدید هم دارم، بیا ببین."],
    "mahsa": ["سلام! ممنون که پیام دادی.", "ساختمان قدیمی را حتماً از نزدیک ببین.", "نقشه‌اش را برایت می‌فرستم.", "چه ایده‌ی خوبی!"],
}
_bot_reply_idx = {}


# ---------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    bio TEXT NOT NULL DEFAULT '',
    hue INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS follows (
    follower TEXT NOT NULL,
    followee TEXT NOT NULL,
    PRIMARY KEY (follower, followee)
);
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    author TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    media_kind TEXT,
    media_path TEXT,
    media_alt TEXT,
    like_base INTEGER NOT NULL DEFAULT 0,
    repost_base INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS likes (
    post_id TEXT NOT NULL, username TEXT NOT NULL,
    PRIMARY KEY (post_id, username)
);
CREATE TABLE IF NOT EXISTS reposts (
    post_id TEXT NOT NULL, username TEXT NOT NULL,
    PRIMARY KEY (post_id, username)
);
CREATE TABLE IF NOT EXISTS replies (
    id TEXT PRIMARY KEY, post_id TEXT NOT NULL,
    author TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, convo TEXT NOT NULL,
    sender TEXT NOT NULL, recipient TEXT NOT NULL,
    text TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_convo ON messages(convo, created_at);
CREATE TABLE IF NOT EXISTS education_sso_nonces (
    jti TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    consumed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_education_sso_expiry ON education_sso_nonces(expires_at);
"""


def hue_for(name):
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) % 360
    return h


def generate_seed_media():
    """Create small demo image/audio assets used by the seeded posts, if missing."""
    img_path = os.path.join(UPLOAD_DIR, "seed_arch.jpg")
    if HAS_PIL and not os.path.exists(img_path):
        from PIL import ImageDraw
        w, h = 900, 560
        im = Image.new("RGB", (w, h), (227, 189, 139))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, w, 76], fill=(11, 128, 125))
        for cx in (180, 450, 720):
            d.pieslice([cx - 82, 134, cx + 82, 134 + 340], 180, 360, fill=(36, 80, 90))
            d.rectangle([cx - 82, 134 + 170, cx + 82, h], fill=(36, 80, 90))
            d.pieslice([cx - 60, 156, cx + 60, 156 + 300], 180, 360, fill=(246, 197, 107))
            d.rectangle([cx - 60, 156 + 150, cx + 60, h], fill=(246, 197, 107))
        d.rectangle([0, h - 24, w, h], fill=(185, 138, 87))
        im.save(img_path, "JPEG", quality=85)

    aud_path = os.path.join(UPLOAD_DIR, "seed_tar.wav")
    if not os.path.exists(aud_path):
        import wave as wv
        import struct
        import math as m
        sr, dur = 16000, 8
        n = sr * dur
        scale = [293.66, 349.23, 392.0, 440.0, 523.25]
        melody = [3, 2, 1, 2, 3, 4, 3, 1, 2, 1, 0, 1, 2, 3, 2, 0]
        frames = bytearray()
        for i in range(n):
            t = i / sr
            v = 0.0
            for k, note in enumerate(melody):
                d0 = t - k * 0.5
                if 0 <= d0 < 1.1:
                    f = scale[note]
                    env = m.exp(-d0 * 4.2) * (1 - m.exp(-d0 * 90))
                    v += (m.sin(2 * m.pi * f * d0) + 0.35 * m.sin(4 * m.pi * f * d0)) * env
            v = max(-1.0, min(1.0, v * 0.32))
            frames += struct.pack("<h", int(v * 32767))
        with wv.open(aud_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(bytes(frames))


def init_db():
    fresh = not os.path.exists(DB_PATH)
    generate_seed_media()
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    if fresh:
        seed(db)
    db.commit()
    db.close()


def seed(db):
    now = time.time()
    HOUR, MIN = 3600, 60
    users = [
        ("sara", "سارا احمدی", "عکاس خیابان و عاشق قهوه"),
        ("arash", "آرش کیانی", "برنامه‌نویس؛ گاهی هم آشپز"),
        ("negar", "نگار رستمی", "نویسنده و مترجم"),
        ("kaveh", "کاوه میرزایی", "تار می‌زنم و آهنگ می‌سازم"),
        ("mahsa", "مهسا فرهادی", "معمار و شیفته‌ی بناهای قدیمی"),
    ]
    bot_password = generate_password_hash(uuid.uuid4().hex)  # unusable random password
    for uname, name, bio in users:
        db.execute(
            "INSERT INTO users (username, password_hash, name, bio, hue, created_at) VALUES (?,?,?,?,?,?)",
            (uname, bot_password, name, bio, hue_for(uname), now - 30 * 86400),
        )
    follows = [("sara", "arash"), ("sara", "negar"), ("arash", "sara"),
               ("negar", "sara"), ("negar", "mahsa"), ("kaveh", "sara"),
               ("kaveh", "negar"), ("mahsa", "sara")]
    db.executemany("INSERT OR IGNORE INTO follows (follower, followee) VALUES (?,?)", follows)
    posts = [
        ("mahsa", "نور بعدازظهر توی این ساختمان قدیمی هر روز یک جور دیگر است. امروز از همیشه قشنگ‌تر بود. #معماری", now - 6 * HOUR, 41, 6,
         "image", "seed_arch.jpg", "طاق‌های یک ساختمان قدیمی در نور بعدازظهر"),
        ("kaveh", "یک قطعه‌ی کوتاه با تار، همین امشب ضبط شد. نظرتان را بگویید 🎶 #موسیقی", now - 90 * MIN, 27, 4,
         "audio", "seed_tar.wav", None),
        ("arash", "بعد از سه ساعت فهمیدم چرا کدم کار نمی‌کرد: یک نقطه‌ویرگول جا مانده بود 😅 #برنامه_نویسی", now - 38 * MIN, 15, 1, None, None, None),
        ("sara", "اولین قهوه‌ی پاییزی امسال. بوی باران هنوز توی کوچه مانده. #پاییز", now - 12 * MIN, 9, 0, None, None, None),
        ("negar", "نوشتن یعنی هر روز کمی از شرم پیش‌نویس اول فاصله بگیری. #نوشتن", now - 3 * HOUR, 33, 8, None, None, None),
        ("arash", "برای آخر هفته یک کتاب پیشنهاد بدهید. ترجیحاً چیزی که یک‌نفس تمام شود.", now - 5 * HOUR, 4, 0, None, None, None),
        ("sara", "خیابان‌های پاییزی بهترین مدل‌های دنیا هستند. #پاییز #عکاسی", now - 9 * HOUR, 22, 2, None, None, None),
        ("kaveh", "جمعه شب یک کنسرت کوچک خانگی دارم. ظرفیت محدود است، زود خبر بدهید! #موسیقی", now - 30 * HOUR, 18, 5, None, None, None),
        ("negar", "آدم‌ها را از سکوتشان بهتر می‌شود شناخت تا از حرف‌هایشان.", now - 30 * HOUR - 4 * HOUR, 12, 1, None, None, None),
    ]
    ids = []
    for author, text, ts, lb, rb, mkind, mpath, malt in posts:
        pid = uuid.uuid4().hex
        ids.append(pid)
        db.execute(
            "INSERT INTO posts (id, author, text, media_kind, media_path, media_alt, like_base, repost_base, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, author, text, mkind, mpath, malt, lb, rb, ts),
        )
    db.execute("INSERT INTO replies (id, post_id, author, text, created_at) VALUES (?,?,?,?,?)",
               (uuid.uuid4().hex, ids[0], "sara", "چه قابی! کجاست؟", now - 5 * HOUR))
    db.execute("INSERT INTO replies (id, post_id, author, text, created_at) VALUES (?,?,?,?,?)",
               (uuid.uuid4().hex, ids[1], "sara", "صدای تار عالی بود!", now - 80 * MIN))
    db.execute("INSERT INTO replies (id, post_id, author, text, created_at) VALUES (?,?,?,?,?)",
               (uuid.uuid4().hex, ids[1], "arash", "دست‌مریزاد. آن قسمت آخرش خیلی خوب بود.", now - 60 * MIN))


# ---------------------------------------------------------------- helpers
def now_ts():
    return time.time()


def current_user():
    return session.get("username")


def require_auth():
    u = current_user()
    if not u:
        abort(401, description="ابتدا وارد حساب کاربری خود شو.")
    return u


def user_row(db, username):
    return db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def user_public(db, username):
    row = user_row(db, username)
    if not row:
        return None
    return {"username": row["username"], "name": row["name"], "bio": row["bio"], "hue": row["hue"]}


def stats_for(db, username):
    following = db.execute("SELECT COUNT(*) c FROM follows WHERE follower=?", (username,)).fetchone()["c"]
    followers = db.execute("SELECT COUNT(*) c FROM follows WHERE followee=?", (username,)).fetchone()["c"]
    posts = db.execute("SELECT COUNT(*) c FROM posts WHERE author=?", (username,)).fetchone()["c"]
    return {"following": following, "followers": followers, "posts": posts}


def serialize_post(db, row, me):
    likes = db.execute("SELECT username FROM likes WHERE post_id=?", (row["id"],)).fetchall()
    reposts = db.execute("SELECT username FROM reposts WHERE post_id=?", (row["id"],)).fetchall()
    liked_usernames = [r["username"] for r in likes]
    reposted_usernames = [r["username"] for r in reposts]
    reply_count = db.execute("SELECT COUNT(*) c FROM replies WHERE post_id=?", (row["id"],)).fetchone()["c"]
    author = user_public(db, row["author"]) or {"username": row["author"], "name": row["author"], "bio": "", "hue": 200}
    media = None
    if row["media_kind"]:
        media = {"kind": row["media_kind"], "url": f"/uploads/{row['media_path']}" if row["media_path"] else None,
                 "alt": row["media_alt"]}
    return {
        "id": row["id"], "author": author, "text": row["text"], "createdAt": row["created_at"],
        "media": media, "likeCount": row["like_base"] + len(liked_usernames),
        "repostCount": row["repost_base"] + len(reposted_usernames),
        "replyCount": reply_count, "likedByMe": me in liked_usernames, "repostedByMe": me in reposted_usernames,
        "mine": row["author"] == me,
    }


def validate_username(u):
    return bool(USERNAME_RE.match(u or ""))


def save_media(file_storage):
    """Validate + persist an uploaded media file. Returns (kind, stored_name, alt) or raises ValueError."""
    filename = file_storage.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime = (file_storage.mimetype or "").lower()

    if mime.startswith("image/"):
        kind = "image"
    elif mime.startswith("video/"):
        kind = "video"
    elif mime.startswith("audio/"):
        kind = "audio"
    elif ext in IMAGE_EXTS:
        kind = "image"
    elif ext in AUDIO_EXTS:
        kind = "audio"
    elif ext in VIDEO_EXTS:
        kind = "video"
    else:
        raise ValueError("نوع فایل پشتیبانی نمی‌شود.")

    data = file_storage.read()
    limit = {"image": MAX_IMAGE, "video": MAX_VIDEO, "audio": MAX_AUDIO}[kind]
    if len(data) > limit:
        raise ValueError(f"حجم فایل بیش از حد مجاز است ({limit // (1024*1024)} مگابایت).")
    if not data:
        raise ValueError("فایل خالی است.")

    stored = f"{uuid.uuid4().hex}"
    if kind == "image" and HAS_PIL:
        try:
            im = Image.open(io.BytesIO(data))
            im = im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im
            im.thumbnail((1600, 1600))
            stored += ".jpg"
            im.save(os.path.join(UPLOAD_DIR, stored), "JPEG", quality=82)
        except Exception:
            stored += ("." + ext if ext else ".bin")
            with open(os.path.join(UPLOAD_DIR, stored), "wb") as f:
                f.write(data)
    else:
        stored += ("." + ext if ext else ".bin")
        with open(os.path.join(UPLOAD_DIR, stored), "wb") as f:
            f.write(data)
    return kind, stored


def convo_key(a, b):
    return "|".join(sorted([a, b]))


def schedule_bot_reply(bot, human):
    def _work():
        time.sleep(1.6)
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        pool = BOT_REPLIES.get(bot, ["سلام!"])
        i = _bot_reply_idx.get(bot, -1)
        i = (i + 1) % len(pool)
        _bot_reply_idx[bot] = i
        db.execute(
            "INSERT INTO messages (id, convo, sender, recipient, text, created_at) VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex, convo_key(bot, human), bot, human, pool[i], time.time()),
        )
        db.commit()
        db.close()
    threading.Thread(target=_work, daemon=True).start()


# ---------------------------------------------------------------- auth routes
@app.post("/api/auth/signup")
def signup():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    if len(name) < 2:
        return jsonify(error="نام را وارد کن (دست‌کم ۲ حرف)."), 400
    if not validate_username(username):
        return jsonify(error="نام کاربری فقط شامل حروف انگلیسی، عدد و _ باشد (۳ تا ۲۰ کاراکتر)."), 400
    if len(password) < 8:
        return jsonify(error="رمز عبور دست‌کم ۸ کاراکتر باشد."), 400
    db = get_db()
    if user_row(db, username):
        return jsonify(error="این نام کاربری قبلاً گرفته شده است."), 409
    db.execute(
        "INSERT INTO users (username, password_hash, name, bio, hue, created_at) VALUES (?,?,?,?,?,?)",
        (username, generate_password_hash(password), name, "", hue_for(username), now_ts()),
    )
    db.execute("INSERT INTO follows (follower, followee) VALUES (?,?)", (username, "sara"))
    db.commit()
    session["username"] = username
    return jsonify(user=user_public(db, username))


@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    db = get_db()
    row = user_row(db, username)
    if not row or username in DEMO_BOTS or not check_password_hash(row["password_hash"], password):
        return jsonify(error="نام کاربری یا رمز عبور درست نیست."), 401
    session["username"] = username
    return jsonify(user=user_public(db, username))


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify(ok=True)




# ---------------------------------------------------------- education SSO
def _decode_urlsafe(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@app.get("/sso/education")
def education_sso_login():
    """Consume a short-lived, single-use HMAC token issued by Goli-Abad Education."""
    secret = os.environ.get("NAVA_EDU_SSO_SECRET", "").strip()
    token = (request.args.get("token") or "").strip()
    if not secret or len(secret) < 32 or not token:
        abort(404)
    parts = token.split(".")
    if len(parts) != 2:
        abort(400, description="درخواست ورود یکپارچه معتبر نیست.")
    encoded, supplied_sig = parts
    expected_sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    if not hmac.compare_digest(supplied_sig, expected_sig):
        abort(401, description="امضای ورود یکپارچه معتبر نیست.")
    try:
        payload = json.loads(_decode_urlsafe(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        abort(400, description="توکن ورود قابل خواندن نیست.")
    now = time.time()
    username = str(payload.get("sub", "")).strip().lower()
    jti = str(payload.get("jti", "")).strip()
    try:
        expires = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        expires = 0
    if (payload.get("iss") != "goli-abad-education" or not validate_username(username)
            or not jti or len(jti) > 128 or expires < now or expires > now + 90):
        abort(401, description="توکن ورود منقضی یا نامعتبر است.")
    db = get_db()
    row = user_row(db, username)
    if not row or username in DEMO_BOTS:
        abort(403, description="ابتدا باید حساب واقعی و موجود Nava را به پرتال آموزشی متصل کنید.")
    try:
        db.execute("DELETE FROM education_sso_nonces WHERE expires_at < ?", (now,))
        db.execute(
            "INSERT INTO education_sso_nonces(jti,expires_at,consumed_at) VALUES(?,?,?)",
            (jti, expires, now),
        )
        db.commit()
    except sqlite3.IntegrityError:
        abort(409, description="این لینک ورود قبلاً استفاده شده است.")
    session.clear()
    session["username"] = username
    return redirect("/")


@app.get("/api/auth/me")
def me():
    u = current_user()
    if not u:
        return jsonify(user=None), 200
    db = get_db()
    row = user_public(db, u)
    if not row:
        session.clear()
        return jsonify(user=None)
    return jsonify(user=row)


@app.patch("/api/me")
def update_me():
    u = require_auth()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    bio = (data.get("bio") or "").strip()
    if len(name) < 2:
        return jsonify(error="نام دست‌کم ۲ حرف باشد."), 400
    db = get_db()
    db.execute("UPDATE users SET name=?, bio=? WHERE username=?", (name[:30], bio[:120], u))
    db.commit()
    return jsonify(user=user_public(db, u))


# ---------------------------------------------------------------- posts
@app.get("/api/posts")
def list_posts():
    u = require_auth()
    db = get_db()
    tab = request.args.get("tab", "foryou")
    tag = (request.args.get("tag") or "").strip().lower()
    rows = db.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()
    out = []
    following = None
    if tab == "following":
        following = {r["followee"] for r in db.execute("SELECT followee FROM follows WHERE follower=?", (u,))}
        following.add(u)
    for r in rows:
        if following is not None and r["author"] not in following:
            continue
        if tag and tag not in [t.lower() for t in TAG_RE.findall(r["text"])]:
            continue
        out.append(serialize_post(db, r, u))
    return jsonify(posts=out)


@app.post("/api/posts")
def create_post():
    u = require_auth()
    text = (request.form.get("text") or "").strip()[:300]
    file_storage = request.files.get("media")
    media_kind = media_path = media_alt = None
    if file_storage and file_storage.filename:
        try:
            media_kind, media_path = save_media(file_storage)
        except ValueError as e:
            return jsonify(error=str(e)), 400
    if not text and not media_kind:
        return jsonify(error="متن یا فایلی برای پست لازم است."), 400
    db = get_db()
    pid = uuid.uuid4().hex
    db.execute(
        "INSERT INTO posts (id, author, text, media_kind, media_path, media_alt, created_at) VALUES (?,?,?,?,?,?,?)",
        (pid, u, text, media_kind, media_path, media_alt, now_ts()),
    )
    db.commit()
    row = db.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    return jsonify(post=serialize_post(db, row, u)), 201


@app.delete("/api/posts/<pid>")
def delete_post(pid):
    u = require_auth()
    db = get_db()
    row = db.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify(error="پست پیدا نشد."), 404
    if row["author"] != u:
        return jsonify(error="اجازه‌ی حذف این پست را نداری."), 403
    if row["media_path"]:
        try:
            os.remove(os.path.join(UPLOAD_DIR, row["media_path"]))
        except OSError:
            pass
    db.execute("DELETE FROM posts WHERE id=?", (pid,))
    db.execute("DELETE FROM likes WHERE post_id=?", (pid,))
    db.execute("DELETE FROM reposts WHERE post_id=?", (pid,))
    db.execute("DELETE FROM replies WHERE post_id=?", (pid,))
    db.commit()
    return jsonify(ok=True)


@app.post("/api/posts/<pid>/like")
def like_post(pid):
    u = require_auth()
    db = get_db()
    row = db.execute("SELECT id FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify(error="پست پیدا نشد."), 404
    exists = db.execute("SELECT 1 FROM likes WHERE post_id=? AND username=?", (pid, u)).fetchone()
    if exists:
        db.execute("DELETE FROM likes WHERE post_id=? AND username=?", (pid, u))
    else:
        db.execute("INSERT INTO likes (post_id, username) VALUES (?,?)", (pid, u))
    db.commit()
    row = db.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    return jsonify(post=serialize_post(db, row, u))


@app.post("/api/posts/<pid>/repost")
def repost_post(pid):
    u = require_auth()
    db = get_db()
    row = db.execute("SELECT id FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify(error="پست پیدا نشد."), 404
    exists = db.execute("SELECT 1 FROM reposts WHERE post_id=? AND username=?", (pid, u)).fetchone()
    if exists:
        db.execute("DELETE FROM reposts WHERE post_id=? AND username=?", (pid, u))
    else:
        db.execute("INSERT INTO reposts (post_id, username) VALUES (?,?)", (pid, u))
    db.commit()
    row = db.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    return jsonify(post=serialize_post(db, row, u))


@app.get("/api/posts/<pid>/replies")
def get_replies(pid):
    require_auth()
    db = get_db()
    rows = db.execute("SELECT * FROM replies WHERE post_id=? ORDER BY created_at ASC", (pid,)).fetchall()
    out = []
    for r in rows:
        author = user_public(db, r["author"]) or {"username": r["author"], "name": r["author"], "hue": 200}
        out.append({"id": r["id"], "author": author, "text": r["text"], "createdAt": r["created_at"]})
    return jsonify(replies=out)


@app.post("/api/posts/<pid>/replies")
def add_reply(pid):
    u = require_auth()
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()[:300]
    if not text:
        return jsonify(error="متن پاسخ خالی است."), 400
    db = get_db()
    if not db.execute("SELECT 1 FROM posts WHERE id=?", (pid,)).fetchone():
        return jsonify(error="پست پیدا نشد."), 404
    rid = uuid.uuid4().hex
    db.execute("INSERT INTO replies (id, post_id, author, text, created_at) VALUES (?,?,?,?,?)",
               (rid, pid, u, text, now_ts()))
    db.commit()
    author = user_public(db, u)
    return jsonify(reply={"id": rid, "author": author, "text": text, "createdAt": now_ts()}), 201


# ---------------------------------------------------------------- users
@app.get("/api/users/<username>")
def get_user(username):
    u = require_auth()
    db = get_db()
    username = username.lower()
    row = user_public(db, username)
    if not row:
        return jsonify(error="این کاربر پیدا نشد."), 404
    row["stats"] = stats_for(db, username)
    row["followingByMe"] = bool(db.execute(
        "SELECT 1 FROM follows WHERE follower=? AND followee=?", (u, username)).fetchone())
    row["isMe"] = username == u
    return jsonify(user=row)


@app.get("/api/users/<username>/posts")
def get_user_posts(username):
    u = require_auth()
    db = get_db()
    rows = db.execute("SELECT * FROM posts WHERE author=? ORDER BY created_at DESC", (username.lower(),)).fetchall()
    return jsonify(posts=[serialize_post(db, r, u) for r in rows])


@app.post("/api/users/<username>/follow")
def follow_user(username):
    u = require_auth()
    username = username.lower()
    if username == u:
        return jsonify(error="نمی‌توانی خودت را دنبال کنی."), 400
    db = get_db()
    if not user_row(db, username):
        return jsonify(error="این کاربر پیدا نشد."), 404
    db.execute("INSERT OR IGNORE INTO follows (follower, followee) VALUES (?,?)", (u, username))
    db.commit()
    return jsonify(ok=True, following=True)


@app.delete("/api/users/<username>/follow")
def unfollow_user(username):
    u = require_auth()
    db = get_db()
    db.execute("DELETE FROM follows WHERE follower=? AND followee=?", (u, username.lower()))
    db.commit()
    return jsonify(ok=True, following=False)


@app.get("/api/trends")
def trends():
    require_auth()
    db = get_db()
    counts = {}
    for r in db.execute("SELECT text FROM posts"):
        for t in TAG_RE.findall(r["text"]):
            counts[t] = counts.get(t, 0) + 1
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return jsonify(trends=[{"tag": t, "count": c} for t, c in top])


@app.get("/api/suggestions")
def suggestions():
    u = require_auth()
    db = get_db()
    rows = db.execute(
        "SELECT username FROM users WHERE username != ? AND username NOT IN "
        "(SELECT followee FROM follows WHERE follower=?) LIMIT 3", (u, u)).fetchall()
    return jsonify(users=[user_public(db, r["username"]) for r in rows])


# ---------------------------------------------------------------- messages
@app.get("/api/conversations")
def list_conversations():
    u = require_auth()
    db = get_db()
    others = db.execute("SELECT username FROM users WHERE username != ?", (u,)).fetchall()
    out = []
    for r in others:
        other = r["username"]
        last = db.execute(
            "SELECT * FROM messages WHERE convo=? ORDER BY created_at DESC LIMIT 1",
            (convo_key(u, other),)).fetchone()
        out.append({
            "user": user_public(db, other),
            "lastMessage": {"text": last["text"], "fromMe": last["sender"] == u, "createdAt": last["created_at"]} if last else None,
        })
    out.sort(key=lambda c: c["lastMessage"]["createdAt"] if c["lastMessage"] else 0, reverse=True)
    return jsonify(conversations=out)


@app.get("/api/conversations/<username>")
def get_conversation(username):
    u = require_auth()
    db = get_db()
    username = username.lower()
    if not user_row(db, username):
        return jsonify(error="این کاربر پیدا نشد."), 404
    since = request.args.get("since", type=float, default=0)
    rows = db.execute(
        "SELECT * FROM messages WHERE convo=? AND created_at > ? ORDER BY created_at ASC",
        (convo_key(u, username), since)).fetchall()
    return jsonify(messages=[
        {"id": r["id"], "text": r["text"], "fromMe": r["sender"] == u, "createdAt": r["created_at"]} for r in rows
    ])


@app.post("/api/conversations/<username>")
def send_message(username):
    u = require_auth()
    username = username.lower()
    db = get_db()
    if not user_row(db, username):
        return jsonify(error="این کاربر پیدا نشد."), 404
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()[:500]
    if not text:
        return jsonify(error="متن پیام خالی است."), 400
    mid = uuid.uuid4().hex
    ts = now_ts()
    db.execute("INSERT INTO messages (id, convo, sender, recipient, text, created_at) VALUES (?,?,?,?,?,?)",
               (mid, convo_key(u, username), u, username, text, ts))
    db.commit()
    if username in DEMO_BOTS:
        schedule_bot_reply(username, u)
    return jsonify(message={"id": mid, "text": text, "fromMe": True, "createdAt": ts}), 201


# ---------------------------------------------------------------- static / frontend
@app.get("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.errorhandler(401)
def handle_401(e):
    return jsonify(error=str(e.description) if hasattr(e, "description") else "ابتدا وارد شو."), 401


@app.errorhandler(413)
def handle_413(e):
    return jsonify(error="حجم فایل ارسالی بیش از حد مجاز است."), 413


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
