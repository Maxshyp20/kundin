import asyncio
import csv
import io
import hashlib
import hmac
import html
import json
import urllib.parse
import urllib.request
import logging
import os
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template_string, request, jsonify, send_file, abort, send_from_directory
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, FSInputFile, WebAppInfo, BufferedInputFile
)
import sqlite3
# qw
load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ios-helper")

ROOT = Path(__file__).parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)
STATIC_DIR = ROOT / "static"
STATIC_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "bot.db"))
SCREENSHOTS_DIR = ROOT / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEB_URL = os.getenv("WEB_URL", "").strip()
INITIAL_ADMIN = os.getenv("INITIAL_ADMIN", "esli_pupsik").strip().lstrip("@").lower()

if not BOT_TOKEN:
    raise RuntimeError("Укажи BOT_TOKEN в файле .env")

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TEXT NOT NULL,
                email TEXT,
                password TEXT,
                last_seen TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS generations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                username TEXT,
                first_name TEXT,
                email TEXT NOT NULL,
                password TEXT NOT NULL,
                source TEXT,
                created_at TEXT NOT NULL,
                assigned_to_id INTEGER,
                assigned_to_username TEXT,
                assigned_to_name TEXT,
                assigned_to_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                username TEXT PRIMARY KEY,
                added_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                photo_filename TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                answer TEXT,
                status TEXT DEFAULT 'open',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                target TEXT PRIMARY KEY,
                banned_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR IGNORE INTO admins(username, added_at) VALUES (?, ?)",
            (INITIAL_ADMIN, datetime.now(timezone.utc).isoformat())
        )

def is_admin(username):
    if not username:
        return False
    with db() as conn:
        row = conn.execute("SELECT 1 FROM admins WHERE username = ?", (username.lstrip("@").lower(),)).fetchone()
    return row is not None

def is_user_banned(user_id, username):
    with db() as conn:
        targets = [str(user_id)]
        if username:
            targets.append(username.lower())
        placeholders = ','.join(['?'] * len(targets))
        row = conn.execute(f"SELECT 1 FROM banned_users WHERE target IN ({placeholders})", targets).fetchone()
        return row is not None

def get_admins():
    with db() as conn:
        rows = conn.execute("SELECT username FROM admins ORDER BY username").fetchall()
    return [r["username"] for r in rows]

def notify_admins_sync(text):
    with db() as conn:
        rows = conn.execute("SELECT u.telegram_id FROM admins a JOIN users u ON a.username = u.username").fetchall()
    for r in rows:
        if r["telegram_id"]:
            send_tg_message_sync(r["telegram_id"], text)

def send_tg_message_sync(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log.error(f"Ошибка отправки уведомления: {e}")

def assign_email_for_user(user_id, username, first_name, source):
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT email, password FROM generations WHERE assigned_to_id = ? LIMIT 1", (user_id,)
        ).fetchone()
        if existing:
            return existing["email"], existing["password"], "already_assigned"

        available = conn.execute(
            """SELECT id, email, password FROM generations 
               WHERE (source = 'вручную' OR source = 'добавлено вручную') AND (assigned_to_id IS NULL OR assigned_to_id = 0)
               ORDER BY id ASC LIMIT 1"""
        ).fetchone()

        if not available:
            return None, None, "no_available"

        conn.execute(
            """UPDATE generations 
               SET assigned_to_id = ?, assigned_to_username = ?, assigned_to_name = ?, assigned_to_at = ?
               WHERE id = ?""",
            (user_id, username, first_name, now, available["id"])
        )
        conn.execute(
            "UPDATE users SET email = ?, password = ? WHERE telegram_id = ?",
            (available["email"], available["password"], user_id)
        )
        return available["email"], available["password"], "success"

def make_user_token(user_id):
    sig = hmac.new(BOT_TOKEN.encode(), f"user:{user_id}".encode(), hashlib.sha256).hexdigest()[:20]
    return f"{user_id}.{sig}"

def read_user_token(token):
    try:
        uid, sig = (token or "").split(".", 1)
        good = hmac.new(BOT_TOKEN.encode(), f"user:{int(uid)}".encode(), hashlib.sha256).hexdigest()[:20]
        if hmac.compare_digest(sig, good):
            return int(uid)
    except Exception:
        pass
    return None

def site_url_for(user_id):
    return f"{WEB_URL.rstrip('/')}/?t={make_user_token(user_id)}"

def upsert_user(user_id, username, first_name):
    if not user_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            """INSERT INTO users(telegram_id, username, first_name, joined_at, last_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
               username=COALESCE(excluded.username, users.username),
               first_name=COALESCE(excluded.first_name, users.first_name),
               last_seen=excluded.last_seen""",
            (user_id, username, first_name, now, now)
        )

class TrackUsers(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if u and not u.is_bot:
            try:
                upsert_user(u.id, u.username, u.first_name)
            except Exception:
                pass
                
            if is_user_banned(u.id, u.username):
                if isinstance(event, Message):
                    await event.answer("🚫 Вы были заблокированы и не можете использовать этого бота.")
                return 
                
        return await handler(event, data)

dp = Dispatcher()
dp.message.outer_middleware(TrackUsers())
dp.callback_query.outer_middleware(TrackUsers())

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🌐 Открыт приложение")],
            [KeyboardButton(text="📜 Политика конфиденциальности")],
            [KeyboardButton(text="👤 Обратиться к администратору")],
        ], resize_keyboard=True
    )

@dp.message(CommandStart())
async def start(message: Message):
    caption = (
        "📱 <b>Проблема с установкой приложений на iPhone для работы и повседневной жизни</b>\n\n"
        "— Ничего страшного: бот бесплатно помогает устанавливать всё это!\n\n"
        "🎮 <b>Доступные приложения и игры:</b>\n"
        "✅ Toca Boca, Minecraft и ещё множество игр!\n"
        "И многое другое"
    )
    
    photo_path = STATIC_DIR / "start_photo.jpg"
    
    if photo_path.exists():
        photo = FSInputFile(str(photo_path))
        await message.answer_photo(photo=photo, caption=caption, reply_markup=main_keyboard(), parse_mode="HTML")
    else:
        await message.answer(caption, reply_markup=main_keyboard(), parse_mode="HTML")

@dp.message(F.text == "🌐 Открыт приложение")
async def open_site(message: Message):
    url = site_url_for(message.from_user.id)
    btn = InlineKeyboardButton(text="🌐 Открыть приложение", web_app=WebAppInfo(url=url))
    await message.answer("Нажмите кнопку ниже для запуска:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn]]))

@dp.message(F.text == "📜 Политика конфиденциальности")
async def privacy_policy(message: Message):
    url = f"{WEB_URL.rstrip('/')}/privacy"
    btn = InlineKeyboardButton(text="📖 Открыть соглашение", web_app=WebAppInfo(url=url))
    await message.answer("Нажмите кнопку ниже для просмотра пользовательского соглашения и политики конфиденциальности:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn]]))

@dp.message(F.text == "👤 Обратиться к администратору")
async def contact_admin(message: Message):
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Написать администратору", url="https://t.me/HellperIOS")]])
    await message.answer("Связь с администратором:", reply_markup=markup)

web = Flask(__name__)

def check_init_data(init_data):
    if not init_data: return None
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received): return None
        return json.loads(pairs.get("user", "null"))
    except Exception: return None

def identify_user():
    user = check_init_data(request.headers.get("X-Init-Data", ""))
    if user and user.get("id"): return user
    uid = read_user_token(request.headers.get("X-User-Token", ""))
    if uid:
        with db() as conn:
            row = conn.execute("SELECT username, first_name FROM users WHERE telegram_id = ?", (uid,)).fetchone()
        return {"id": uid, "username": row["username"] if row else None, "first_name": row["first_name"] if row else None}
    return None

PAGE = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>iOS Игры и Приложения</title>
<script async src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root {
  --bg: #ffffff;
  --text: #000000;
  --muted: #8e8e93;
  --line: #efeff4;
  --btn-bg: #e8f8f0;
  --btn-text: #00a859;
  --admin-bg: #ff9500;
}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding-bottom: 30px;}
.app{max-width:520px;margin:0 auto;padding:12px 16px; position:relative; min-height:100vh;}
h1{font-size:22px; margin: 10px 0 4px 0}
button{font:inherit;cursor:pointer;border:0;text-align:left}

.admin-panel-btn{width:100%;padding:12px;border-radius:12px;background:var(--admin-bg);color:#fff;font-weight:bold;text-align:center;font-size:15px;margin-bottom:14px;box-shadow: 0 4px 10px rgba(255,149,0,0.3);}

.app-item{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--line);gap:12px;}
.app-left{display:flex;align-items:center;gap:12px; flex:1; min-width:0;}
.game-icon{width:56px;height:56px;border-radius:14px;object-fit:cover;background:#f2f2f7;flex-shrink:0;}
.game-emoji-icon{width:56px;height:56px;border-radius:14px;background:#f2f2f7;display:flex;align-items:center;justify-content:center;font-size:28px;flex-shrink:0;}
.app-info{display:flex;flex-direction:column;min-width:0;}
.app-title{font-size:16px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height: 1.2;}
.app-desc{font-size:13px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.app-rating{font-size:12px;color:var(--muted);margin-top:2px;}

.install-btn{background:var(--btn-bg);color:var(--btn-text);font-weight:600;font-size:14px;padding:7px 18px;border-radius:16px;border:none;flex-shrink:0;transition:transform 0.1s;}
.install-btn:active{transform:scale(0.95);}

.panel{background:#fff;border-radius:16px;padding:16px; margin-top:10px;}
.primary-btn{width:100%;padding:14px;border-radius:14px;background:var(--btn-text);color:#fff;font-weight:bold;text-align:center;font-size:16px; margin-top:10px;}
.primary-btn:disabled{opacity:0.6; cursor:not-allowed}

.video-grid{display:grid;grid-template-columns: 1fr 1fr;gap:10px;margin-bottom:16px;}
.video-card{background:#f8f8fa;border:1px solid var(--line);border-radius:12px;padding:8px;text-align:center}
.video-card b{font-size:13px; display:block; margin-bottom:4px; color:#000;}
.video-card video{width:100%;height:140px;object-fit:cover;border-radius:8px;background:#000;}
.btn-fullscreen {background:#e8f8f0; border:none; color:var(--btn-text); border-radius:6px; padding:4px 8px; cursor:pointer; font-size:11px; font-weight:bold; margin-top:4px; width:100%; text-align:center;}

.upload-box{border:2px dashed #c7c7cc;border-radius:16px;padding:16px;text-align:center;background:#fcfcfd}
.field{padding:12px;background:#f2f2f7;border-radius:12px;margin-top:10px;}
.field b{color:#000}
.hidden{display:none!important}
.back-btn {background:none; color:var(--btn-text); padding:0 0 10px 0; font-weight:bold; display:inline-block; font-size:15px;}
.field-row {display:flex; justify-content:space-between; align-items:center; flex-wrap: wrap; gap: 8px;}
.btn-copy {background:#e8f8f0; border:none; color:var(--btn-text); border-radius:8px; padding:6px 12px; cursor:pointer; font-size:13px; font-weight:bold;}
#timerDisplay {font-size:46px; font-weight:bold; color:var(--btn-text); margin:15px 0; display:block;}

.support-fab {
  position: fixed;
  bottom: 24px;
  right: 24px;
  width: 56px;
  height: 56px;
  background: #00a859;
  color: #fff;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 26px;
  font-weight: bold;
  box-shadow: 0 4px 12px rgba(0, 168, 89, 0.4);
  cursor: pointer;
  z-index: 100;
  transition: transform 0.1s;
}
.support-fab:active { transform: scale(0.92); }

.support-modal {
  position: fixed;
  top: 0; left: 0; width: 100%; height: 100%;
  background: rgba(0,0,0,0.4);
  display: flex;
  align-items: flex-end;
  z-index: 200;
}
.support-content {
  background: #fff;
  width: 100%;
  max-width: 520px;
  margin: 0 auto;
  border-top-left-radius: 20px;
  border-top-right-radius: 20px;
  padding: 20px 16px 40px 16px;
  max-height: 85vh;
  overflow-y: auto;
  box-shadow: 0 -4px 20px rgba(0,0,0,0.1);
}
.ticket-card {
  background: #f8f8fa;
  border-radius: 12px;
  padding: 12px;
  margin-top: 10px;
  border: 1px solid var(--line);
}
.ticket-q { font-weight: 600; color: #000; font-size: 14px; }
.ticket-a { margin-top: 8px; padding: 8px 10px; background: #e8f8f0; border-radius: 8px; font-size: 13px; color: #00a859; }
.ticket-status { font-size: 11px; color: var(--muted); margin-top: 6px; display: block; }
</style>
</head>
<body>
<main class="app">
  <button class="admin-panel-btn hidden" id="btnAdmin" onclick="location.href='/admin?t=' + getToken()">🛡️ Панель администратора</button>

  <div id="view-games" class="hidden">
    <h1>Приложения</h1>
    <p style="color:var(--muted); margin-top:0; font-size: 14px;">Выберите нужную игру или приложение для установки</p>
    
    <div class="app-item">
      <div class="app-left">
        <img src="/static/oboi.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🖼️'}))">
        <div class="app-info">
          <span class="app-title" style="white-space:normal;">Анимированые<br>обои</span>
          <span class="app-desc">Утилиты · Персонализация</span>
          <span class="app-rating">⭐ 4.9 · 18 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Анимированные обои')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/avito.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'📦'}))">
        <div class="app-info">
          <span class="app-title">Avito</span>
          <span class="app-desc">Сервисы · Объявления</span>
          <span class="app-rating">⭐ 4.8 · 70 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Avito')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/mac.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'💻'}))">
        <div class="app-info">
          <span class="app-title">Max</span>
          <span class="app-desc">Утилиты · Системные</span>
          <span class="app-rating">⭐ 4.9 · 10 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Max')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/capcut.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🎬'}))">
        <div class="app-info">
          <span class="app-title">CapCut Pro</span>
          <span class="app-desc">Утилиты · Видеоредактор</span>
          <span class="app-rating">⭐ 4.9 · 90 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('CapCut Pro')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/wb.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🛍️'}))">
        <div class="app-info">
          <span class="app-title">Wildberries</span>
          <span class="app-desc">Сервисы · Покупки</span>
          <span class="app-rating">⭐ 4.8 · 50 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Wildberries')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/youtube.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'▶️'}))">
        <div class="app-info">
          <span class="app-title">VK видео</span>
          <span class="app-desc">Сервисы · Видео</span>
          <span class="app-rating">⭐ 4.9 · 500 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('VK видео')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/yandex.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🟡'}))">
        <div class="app-info">
          <span class="app-title">Яндекс</span>
          <span class="app-desc">Сервисы · Поиск и ИИ</span>
          <span class="app-rating">⭐ 4.8 · 120 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Яндекс')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/vk.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🔵'}))">
        <div class="app-info">
          <span class="app-title">WK</span>
          <span class="app-desc">Сервисы · Соцсеть</span>
          <span class="app-rating">⭐ 4.7 · 100 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('WK')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/mailru.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'✉️'}))">
        <div class="app-info">
          <span class="app-title">Mail.ru</span>
          <span class="app-desc">Сервисы · Почта</span>
          <span class="app-rating">⭐ 4.6 · 40 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Mail.ru')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/blink.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'✨'}))">
        <div class="app-info">
          <span class="app-title">Blink</span>
          <span class="app-desc">Сервисы · Мессенджер</span>
          <span class="app-rating">⭐ 4.8 · 25 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Blink')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/darkgram.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'💬'}))">
        <div class="app-info">
          <span class="app-title">DarkGram</span>
          <span class="app-desc">Сервисы · Мессенджер</span>
          <span class="app-rating">⭐ 4.6 · 2 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('DarkGram')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/counter_flame.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🎯'}))">
        <div class="app-info">
          <span class="app-title">Counter Flame</span>
          <span class="app-desc">Игры · Шутер</span>
          <span class="app-rating">⭐ 4.7 · 15 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Counter Flame')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/beamng.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🚗'}))">
        <div class="app-info">
          <span class="app-title">BeamNG.drive Mobile</span>
          <span class="app-desc">Игры · Симуляторы</span>
          <span class="app-rating">⭐ 4.9 · 12 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('BeamNG.drive Mobile')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/assettocorsa.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🏎️'}))">
        <div class="app-info">
          <span class="app-title">Assetto Corsa Mobile</span>
          <span class="app-desc">Игры · Гонки</span>
          <span class="app-rating">⭐ 4.8 · 8 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Assetto Corsa Mobile')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/nullsbrawl.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'💥'}))">
        <div class="app-info">
          <span class="app-title">Nulls Brawl</span>
          <span class="app-desc">Игры · Экшен</span>
          <span class="app-rating">⭐ 4.9 · 45 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Nulls Brawl')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/gta.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🎮'}))">
        <div class="app-info">
          <span class="app-title">Grand Theft Auto V</span>
          <span class="app-desc">Игры · Экшен</span>
          <span class="app-rating">⭐ 4.7 · 20 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Grand Theft Auto V')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/gtasa_def.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🌆'}))">
        <div class="app-info">
          <span class="app-title">GTA: San Andreas Definitive</span>
          <span class="app-desc">Игры · Экшен</span>
          <span class="app-rating">⭐ 4.9 · 30 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('GTA: San Andreas Definitive')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/pubg.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🪖'}))">
        <div class="app-info">
          <span class="app-title">PUBG Mobile</span>
          <span class="app-desc">Игры · Королевская битва</span>
          <span class="app-rating">⭐ 4.8 · 100 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('PUBG Mobile')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/standoff2.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🎯'}))">
        <div class="app-info">
          <span class="app-title">Standoff 2</span>
          <span class="app-desc">Игры · Шутер</span>
          <span class="app-rating">⭐ 4.7 · 50 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Standoff 2')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/mortalkombat.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🥋'}))">
        <div class="app-info">
          <span class="app-title">Mortal Kombat Mobile</span>
          <span class="app-desc">Игры · Файтинг</span>
          <span class="app-rating">⭐ 4.8 · 40 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Mortal Kombat Mobile')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/geometrydash.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🟩'}))">
        <div class="app-info">
          <span class="app-title">Geometry Dash</span>
          <span class="app-desc">Игры · Аркады</span>
          <span class="app-rating">⭐ 4.9 · 60 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Geometry Dash')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/fnaf.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🐻'}))">
        <div class="app-info">
          <span class="app-title">FNAF (Freddy)</span>
          <span class="app-desc">Игры · Хоррор</span>
          <span class="app-rating">⭐ 4.8 · 15 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('FNAF (Freddy)')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/fnaf2.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🧸'}))">
        <div class="app-info">
          <span class="app-title">Five Nights at Freddys 2</span>
          <span class="app-desc">Игры · Хоррор</span>
          <span class="app-rating">⭐ 4.9 · 18 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Five Nights at Freddys 2')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/fnaf3.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🐰'}))">
        <div class="app-info">
          <span class="app-title">Five Nights at Freddys 3</span>
          <span class="app-desc">Игры · Хоррор</span>
          <span class="app-rating">⭐ 4.8 · 12 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Five Nights at Freddys 3')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/fnaf4.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🦊'}))">
        <div class="app-info">
          <span class="app-title">Five Nights at Freddys 4</span>
          <span class="app-desc">Игры · Хоррор</span>
          <span class="app-rating">⭐ 4.9 · 14 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Five Nights at Freddys 4')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/tocaboca.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🧁'}))">
        <div class="app-info">
          <span class="app-title">Toca Boca взлом</span>
          <span class="app-desc">Игры · Обучение и детские</span>
          <span class="app-rating">⭐ 4.9 · 80 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Toca Boca взлом')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/forza.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🏎️'}))">
        <div class="app-info">
          <span class="app-title">Forza Horizon 4 Mobile</span>
          <span class="app-desc">Игры · Гонки</span>
          <span class="app-rating">⭐ 4.8 · 10 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Forza Horizon 4 Mobile')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/roblox.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🧱'}))">
        <div class="app-info">
          <span class="app-title">Roblox взлом</span>
          <span class="app-desc">Игры · Приключения</span>
          <span class="app-rating">⭐ 4.6 · 200 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Roblox взлом')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/chatroulette.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🌐'}))">
        <div class="app-info">
          <span class="app-title">Чат рулетка</span>
          <span class="app-desc">Сервисы · Соцсети</span>
          <span class="app-rating">⭐ 4.5 · 5 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Чат рулетка')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/rust.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'⛺'}))">
        <div class="app-info">
          <span class="app-title">Rust Mobile</span>
          <span class="app-desc">Игры · Выживание</span>
          <span class="app-rating">⭐ 4.7 · 9 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Rust Mobile')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/ayugram.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'💬'}))">
        <div class="app-info">
          <span class="app-title">AyuGram</span>
          <span class="app-desc">Сервисы · Мессенджер</span>
          <span class="app-rating">⭐ 4.9 · 3 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('AyuGram')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <div class="game-emoji-icon">🏨</div>
        <div class="app-info">
          <span class="app-title">Островок ios</span>
          <span class="app-desc">Путешествия · Бронирование</span>
          <span class="app-rating">⭐ 4.8 · 7 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Островок ios')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/bannertt.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'🎬'}))">
        <div class="app-info">
          <span class="app-title">Баннер ТТ</span>
          <span class="app-desc">Утилиты · Медиа</span>
          <span class="app-rating">⭐ 4.6 · 4 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Баннер ТТ')">Установить</button>
    </div>

    <div class="app-item">
      <div class="app-left">
        <img src="/static/minecraft.jpg" class="game-icon" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'game-emoji-icon',innerText:'⛏️'}))">
        <div class="app-info">
          <span class="app-title">Minecraft</span>
          <span class="app-desc">Игры · Песочница</span>
          <span class="app-rating">⭐ 4.9 · 150 млн+</span>
        </div>
      </div>
      <button class="install-btn" onclick="selectGame('Minecraft')">Установить</button>
    </div>
  </div>

  <div id="view-instructions" class="panel hidden">
    <button class="back-btn" onclick="showGames()">🔙 Назад к списку</button>
    <h2 style="margin-top:0; color:#000;">Инструкция</h2>
    <p style="color:var(--muted);font-size:13px; margin-bottom:10px;">Посмотри видео, как выйти из своего iCloud и как войти в наш.</p>
    
    <div class="video-grid">
      <div class="video-card">
        <b>1. Как выйти</b>
        <video id="vidIcloud" playsinline src="/tutorial_media/icloud"></video>
        <button class="btn-fullscreen" onclick="openFullscreen('vidIcloud')">Развернуть</button>
      </div>
      <div class="video-card">
        <b>2. Как войти</b>
        <video id="vidLogin" playsinline src="/tutorial_media/login"></video>
        <button class="btn-fullscreen" onclick="openFullscreen('vidLogin')">Развернуть</button>
      </div>
    </div>

    <div class="upload-box">
      <h3 style="margin-top:0; color:#000;">Подтверждение</h3>
      <p style="font-size:12px;color:var(--muted); margin-top:5px;">Загрузи скриншот того, что ты <b>вышел</b> из своего iCloud в Настройках.</p>
      <input type="file" id="screenshot" accept="image/*" style="margin-bottom:10px;width:100%; font-size:12px;">
      <button class="primary-btn" id="submitBtn" onclick="submitRequest()">Отправить скриншот</button>
    </div>
  </div>

  <div id="view-pending" class="panel hidden" style="text-align:center; padding: 20px;">
    <button class="back-btn" onclick="showGames()" style="display:block; text-align:left; width:100%;">🔙 Назад к списку</button>
    <h2 style="margin-top:20px; color:#000;">⏳ Заявка на проверке</h2>
    <p style="color:var(--muted); margin-bottom: 5px;">Администратор проверяет твой скриншот.</p>
    <p style="color:var(--muted); font-size: 13px; margin-top: 0;">Пожалуйста, подожди...</p>
    
    <div id="timerDisplay">15</div>
    
    <button class="primary-btn hidden" id="refreshBtn" onclick="loadStatus()" style="margin-top:10px;background:#f2f2f7;color:#000">Обновить статус</button>
  </div>

  <div id="view-approved" class="panel hidden" style="text-align:center; padding: 20px;">
    <button class="back-btn" onclick="showGames()" style="display:block; text-align:left; width:100%;">🔙 Назад к списку</button>
    <h2 style="color:var(--btn-text); margin-top:20px;">✅ Одобрено!</h2>
    <p style="color:#000;">Твоя заявка проверена. Нажми кнопку ниже, чтобы получить данные от Apple ID.</p>
    <button class="primary-btn" id="getCredsBtn" onclick="getCredentials()">Получить почту и пароль</button>
  </div>

  <div id="view-fulfilled" class="panel hidden">
    <button class="back-btn" onclick="showGames()">🔙 Назад к списку</button>
    <h2 style="margin-top:0; color:#000;">Твои данные Apple ID</h2>
    <p style="color:var(--muted);font-size:14px">Используй эти данные для входа в App Store.</p>
    
    <div class="field field-row">
      <div><b>Почта:</b> <br><code id="lblEmail" style="font-size:16px; color:#000;"></code></div>
      <button class="btn-copy" onclick="copyText('lblEmail', this)">Копировать</button>
    </div>
    
    <div class="field field-row">
      <div><b>Пароль:</b> <br><code id="lblPass" style="font-size:16px; color:#000;"></code></div>
      <button class="btn-copy" onclick="copyText('lblPass', this)">Копировать</button>
    </div>
  </div>

  <div id="view-banned" class="panel hidden" style="text-align:center; padding: 40px 20px;">
    <h2 style="color:#ff3b30; margin-top:20px;">🚫 Доступ закрыт</h2>
    <p style="color:var(--muted)">Вы были заблокированы администратором и больше не можете пользоваться этим приложением.</p>
  </div>

  <div class="support-fab" onclick="openSupportModal()">?</div>

  <div id="supportModal" class="support-modal hidden">
    <div class="support-content">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
        <h2 style="margin:0; font-size:20px; color:#000;">Поддержка</h2>
        <button onclick="closeSupportModal()" style="background:none; font-size:22px; font-weight:bold; color:var(--muted);">✕</button>
      </div>
      <p style="color:var(--muted); font-size:13px; margin-top:0; margin-bottom:16px;">
        Задайте вопрос — он появится в вашем диалоге. Владелец увидит обращение и сможет ответить.
      </p>

      <div style="display:flex; gap:8px; margin-bottom:20px;">
        <input type="text" id="supportInput" placeholder="Напишите вопрос..." style="flex:1; padding:12px; background:#f2f2f7; border:1px solid var(--line); border-radius:12px; font-size:14px; outline:none;">
        <button class="primary-btn" style="width:auto; margin-top:0; padding:12px 18px; font-size:14px;" onclick="sendSupportQuestion()">Отправить</button>
      </div>

      <h3 style="font-size:15px; color:#000; margin-bottom:8px;">МОИ ОБРАЩЕНИЯ</h3>
      <div id="supportTicketsList">
        <p style="color:var(--muted); font-size:13px; text-align:center; padding:20px 0;">Загрузка...</p>
      </div>
    </div>
  </div>
</main>

<script>
let currentGame = "";
let pendingInterval = null;

function getToken() {
  const q = new URLSearchParams(location.search).get("t");
  if (q) sessionStorage.setItem("ut", q);
  return sessionStorage.getItem("ut") || "";
}
function hdrs() { return {"X-User-Token": getToken(), "Content-Type": "application/json"}; }
const $ = id => document.getElementById(id);
const hideAll = () => ["view-games", "view-instructions", "view-pending", "view-approved", "view-fulfilled", "view-banned"].forEach(id => $(id).classList.add("hidden"));

function showView(viewId) {
    hideAll();
    $(viewId).classList.remove("hidden");
    
    if (viewId !== "view-pending") {
        clearInterval(pendingInterval);
    }
    
    if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.BackButton) {
        if (viewId === "view-games" || viewId === "view-banned") {
            window.Telegram.WebApp.BackButton.hide();
        } else {
            window.Telegram.WebApp.BackButton.show();
        }
    }
}

function showGames() {
    showView("view-games");
}

function openFullscreen(videoId) {
    const video = $(videoId);
    if (video.requestFullscreen) {
        video.requestFullscreen();
    } else if (video.webkitRequestFullscreen) {
        video.webkitRequestFullscreen();
    } else if (video.msRequestFullscreen) {
        video.msRequestFullscreen();
    }
    video.controls = true;
    video.play();
}

function copyText(elementId, btn) {
    const textToCopy = $(elementId).innerText;
    const originalText = btn.innerText;
    
    const successFeedback = () => {
        btn.innerText = "Скопировано!";
        setTimeout(() => { 
            btn.innerText = originalText;
        }, 2000);
    };

    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(textToCopy).then(successFeedback);
    } else {
        const textArea = document.createElement("textarea");
        textArea.value = textToCopy;
        textArea.style.position = "fixed";
        textArea.style.left = "-999999px";
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        try {
            document.execCommand('copy');
            successFeedback();
        } catch (err) {
            console.error('Не удалось скопировать', err);
        }
        document.body.removeChild(textArea);
    }
}

function startPendingTimer() {
    showView("view-pending");
    let timeLeft = 15;
    $("timerDisplay").innerText = timeLeft;
    $("timerDisplay").classList.remove("hidden");
    $("refreshBtn").classList.add("hidden");
    
    clearInterval(pendingInterval);
    pendingInterval = setInterval(() => {
        timeLeft--;
        if (timeLeft > 0) {
            $("timerDisplay").innerText = timeLeft;
        } else {
            clearInterval(pendingInterval);
            $("timerDisplay").innerText = "⏳";
            loadStatus();
        }
    }, 1000);
}

async function loadStatus() {
  try {
    const r = await fetch("/api/my_status", {headers: hdrs()});
    const data = await r.json();
    
    if (data.is_admin) $("btnAdmin").classList.remove("hidden");

    if (data.status === 'banned') {
        showView("view-banned");
    } else if (data.status === 'fulfilled' || data.creds) {
        $("lblEmail").innerText = data.creds.email;
        $("lblPass").innerText = data.creds.password;
        showView("view-fulfilled");
    } else if (data.status === 'approved') {
        showView("view-approved");
    } else if (data.status === 'pending') {
        startPendingTimer();
    } else {
        showView("view-games");
    }
  } catch (e) { showView("view-games"); }
}

function selectGame(game) {
  currentGame = game;
  showView("view-instructions");
}

async function submitRequest() {
  const file = $("screenshot").files[0];
  if (!file) return alert("Пожалуйста, прикрепите скриншот!");
  
  if (!file.type.startsWith('image/')) {
      alert("Пожалуйста, загрузите изображение (скриншот)!");
      return;
  }
  
  $("submitBtn").disabled = true;
  $("submitBtn").innerText = "Отправка...";
  
  const fd = new FormData();
  fd.append("photo", file);
  fd.append("game", currentGame);
  
  const r = await fetch("/api/request", { method: "POST", headers: hdrs(), body: fd });
  if (r.ok) {
     $("submitBtn").innerText = "Отправить скриншот";
     $("submitBtn").disabled = false;
     startPendingTimer();
  } else {
     alert("Ошибка при загрузке. Убедитесь, что отправляете картинку.");
     $("submitBtn").disabled = false;
     $("submitBtn").innerText = "Отправить скриншот";
  }
}

async function getCredentials() {
  $("getCredsBtn").disabled = true;
  const r = await fetch("/api/credentials", { method: "POST", headers: hdrs() });
  const data = await r.json();
  if (r.ok) { loadStatus(); } 
  else { alert(data.error); $("getCredsBtn").disabled = false; }
}

function openSupportModal() {
    $("supportModal").classList.remove("hidden");
    loadSupportTickets();
}

function closeSupportModal() {
    $("supportModal").classList.add("hidden");
}

async function sendSupportQuestion() {
    const q = $("supportInput").value.trim();
    if (!q) return;
    
    const r = await fetch("/api/support/send", {
        method: "POST",
        headers: hdrs(),
        body: JSON.stringify({ question: q })
    });
    
    if (r.ok) {
        $("supportInput").value = "";
        loadSupportTickets();
    } else {
        alert("Ошибка отправки вопроса.");
    }
}

async function loadSupportTickets() {
    try {
        const r = await fetch("/api/support/list", { headers: hdrs() });
        const data = await r.json();
        const box = $("supportTicketsList");
        
        if (!data.tickets || data.tickets.length === 0) {
            box.innerHTML = '<p style="color:var(--muted); font-size:13px; text-align:center; padding:20px 0;">Здесь появятся ваши обращения в поддержку.</p>';
            return;
        }
        
        let html = "";
        data.tickets.forEach(t => {
            let answerHtml = t.answer ? `<div class="ticket-a"><b>Ответ администратора:</b><br>${esc(t.answer)}</div>` : `<div class="ticket-status">Ожидает ответа...</div>`;
            html += `<div class="ticket-card">
                <div class="ticket-q">❓ ${esc(t.question)}</div>
                ${answerHtml}
            </div>`;
        });
        box.innerHTML = html;
    } catch (e) {
        $("supportTicketsList").innerHTML = '<p style="color:red; font-size:13px; text-align:center;">Ошибка загрузки</p>';
    }
}

const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

window.onload = () => {
    if(window.Telegram && window.Telegram.WebApp) {
        window.Telegram.WebApp.expand();
        if (window.Telegram.WebApp.BackButton) {
            window.Telegram.WebApp.BackButton.onClick(showGames);
        }
    }
    loadStatus();
};
</script>
</body></html>
"""

PRIVACY_PAGE = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Пользовательское соглашение</title>
<style>
body{margin:0;background:#ffffff;color:#000000;font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;padding:20px;}
.container{max-width:600px;margin:0 auto;line-height:1.5;}
h1{font-size:22px;margin-bottom:4px;}
.date{color:#8e8e93;font-size:13px;margin-bottom:20px;}
h3{font-size:16px;margin-top:20px;margin-bottom:6px;color:#000;}
p, li{font-size:14px;color:#3a3a3c;margin-bottom:10px;}
ul{padding-left:20px;}
</style>
</head>
<body>
<div class="container">
  <h1>ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ IOS HELPER</h1>
  <div class="date">25 августа в 15:00<br>Редакция от 25 августа 2026 г.</div>

  <h3>1. ОБЩИЕ ПОЛОЖЕНИЯ</h3>
  <p>1.1. Настоящее Пользовательское соглашение (далее — «Соглашение») регулирует отношения между Сервисом и Пользователем.</p>
  <p>1.2. iOS Helper — программный сервис, доступный через Telegram Mini App и иные официальные ресурсы Оператора (далее — «Сервис»). Настоящее Соглашение определяет условия доступа и использования Сервиса.</p>
  <p>1.3. Используя Сервис, проходя регистрацию, авторизацию или продолжая работу с iOS Helper, Пользователь подтверждает, что ознакомился с условиями Соглашения, понимает их и принимает в полном объёме.</p>
  <p>1.4. Использование Сервиса также регулируется Политикой конфиденциальности iOS Helper, а также иными правилами, которые Оператор публикует в интерфейсе Сервиса.</p>

  <h3>2. ОСНОВНЫЕ ТЕРМИНЫ</h3>
  <p>2.1. «iOS Helper» или «Сервис» — бот, мини-приложение и связанные с ними программные и технические средства.</p>
  <p>2.2. «Пользователь» — дееспособное физическое лицо, которое получает доступ к Сервису с помощью устройства.</p>
  <p>2.3. «Устройство» — телефон, планшет или иное техническое устройство, с помощью которого Пользователь использует Сервис.</p>
  <p>2.4. «Аккаунт» — учётная запись или сессия Пользователя в iOS Helper.</p>

  <h3>3. ДОСТУП К СЕРВИСУ И УСЛОВИЯ</h3>
  <p>3.1. Оператор предоставляет Пользователю ограниченный и личный доступ к функциональности iOS Helper на условиях настоящего Соглашения.</p>
  <p>3.2. Пользователь обязуется соблюдать правила безопасности, не передавать поддельные скриншоты и корректно выполнять инструкции сервиса.</p>

  <h3>4. ПРАВА И ОБЯЗАННОСТИ ПОЛЬЗОВАТЕЛЯ</h3>
  <p>4.1. Пользователь вправе использовать iOS Helper исключительно в личных целях.</p>
  <p>4.2. Пользователю запрещается нарушать работоспособность Сервиса, использовать уязвимости, обходить защитные механизмы или передавать вредоносный контент.</p>

  <h3>5. ПЕРСОНАЛЬНЫЕ ДАННЫЕ</h3>
  <p>5.1. Обработка персональных данных Пользователя осуществляется в соответствии с Политикой конфиденциальности Сервиса и применимым законодательством.</p>
  <p>5.2. Данные используются исключительно для функционирования бота, проверки заявок и предоставления доступа к аккаунтам приложений.</p>

  <h3>6. ОГРАНИЧЕНИЕ ОТВЕТСТВЕННОСТИ</h3>
  <p>6.1. Сервис предоставляется на условиях «как есть». Оператор не гарантирует абсолютную бесперебойность работы сторонних платформ (Apple App Store, Telegram).</p>
  <p>6.2. В случае нарушения правил Соглашения или мошеннических действий Оператор вправе заблокировать доступ Пользователя к Сервису.</p>

  <h3>7. ЗАКЛЮЧИТЕЛЬНЫЕ ПОЛОЖЕНИЯ</h3>
  <p>7.1. Оператор вправе вносить изменения в настоящее Соглашение. Новая редакция вступает в силу с момента её публикации.</p>
</div>
</body>
</html>
"""

ADMIN_PAGE = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Админ-панель</title>
<style>
:root{ --bg:#f8f8fa; --surface:#ffffff; --surface-2:#f2f2f7; --line:#efeff4; --accent:#00a859; --accent-2:#007aff; --text:#000000; --danger:#ff3b30;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system, sans-serif; padding-bottom: 80px;}
.app{max-width:600px;margin:0 auto;padding:20px 16px}
h1{margin-top:0}
.tabs{display:flex;gap:8px;overflow-x:auto;padding-bottom:10px; margin-bottom:16px; border-bottom:1px solid var(--line)}
.tab{background:var(--surface-2);border:none;color:var(--text);padding:10px 16px;border-radius:12px;font-weight:bold;white-space:nowrap;cursor:pointer}
.tab.active{background:var(--accent);color:#fff}
.tab-content{display:none}
.tab-content.active{display:block}
.card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:16px; margin-bottom:12px; box-shadow: 0 2px 8px rgba(0,0,0,0.03)}
.btn{padding:12px; border-radius:10px; font-weight:bold; border:none; cursor:pointer; width:100%; margin-top:8px}
.btn-green{background:var(--accent); color:#fff}
.btn-red{background:var(--danger); color:#fff}
.btn-gold{background:#ff9500; color:#fff}
input.field{width:100%; background:var(--surface-2); border:1px solid var(--line); color:black; padding:12px; border-radius:10px; margin-bottom:10px;}
.req-img{width:100%; max-height:250px; object-fit:contain; background:#000; border-radius:8px; margin:10px 0; cursor:pointer;}
</style>
</head>
<body>
<main class="app">
  <h1>🛡️ Админ-панель</h1>
  
  <div class="tabs">
    <button class="tab active" onclick="openTab('requests')">📋 Заявки</button>
    <button class="tab" onclick="openTab('support')">💬 Поддержка</button>
    <button class="tab" onclick="openTab('accounts')">📦 Аккаунты</button>
    <button class="tab" onclick="openTab('admins')">👑 Админы</button>
    <button class="tab" onclick="openTab('bans')">🚫 Бан-лист</button>
  </div>

  <div id="requests" class="tab-content active">
    <h3>Ожидают проверки</h3>
    <div id="requestsBox">Загрузка...</div>
  </div>

  <div id="support" class="tab-content">
    <h3>Обращения в поддержку</h3>
    <div id="supportBox">Загрузка...</div>
  </div>

  <div id="accounts" class="tab-content">
    <div class="card">
        <h3>➕ Добавить Apple ID</h3>
        <input type="text" id="newEmail" class="field" placeholder="Email">
        <input type="text" id="newPass" class="field" placeholder="Пароль">
        <button class="btn btn-gold" onclick="addAccount()">Сохранить в базу</button>
    </div>
    <h3>Список всех аккаунтов</h3>
    <div id="accountsBox"></div>
  </div>

  <div id="admins" class="tab-content">
    <div class="card">
        <h3>➕ Добавить Админа</h3>
        <input type="text" id="newAdmin" class="field" placeholder="Юзернейм (без @)">
        <button class="btn btn-gold" onclick="addAdmin()">Сделать админом</button>
    </div>
    <h3>Список администраторов</h3>
    <div id="adminsBox"></div>
  </div>

  <div id="bans" class="tab-content">
    <div class="card">
        <h3>🚫 Заблокировать пользователя</h3>
        <input type="text" id="newBan" class="field" placeholder="Юзернейм (без @) или ID">
        <button class="btn btn-red" onclick="addBan()">Заблокировать</button>
    </div>
    <h3>Заблокированные пользователи</h3>
    <div id="bansBox"></div>
  </div>

  <button class="btn" style="background:var(--surface-2);color:black; margin-top:30px" onclick="location.href='/?t=' + getToken()">🔙 На главную</button>
</main>

<script>
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function getToken() { return new URLSearchParams(location.search).get("t") || sessionStorage.getItem("ut") || ""; }
function hdrs() { return {"X-User-Token": getToken(), "Content-Type": "application/json"}; }

function openTab(tabId) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById(tabId).classList.add('active');
    loadData();
}

async function loadData() {
    const r = await fetch("/api/admin/data", {headers: hdrs()});
    if(!r.ok) return document.body.innerHTML = "<h2>Доступ запрещен</h2>";
    const d = await r.json();
    
    let reqHtml = "";
    if (d.requests.length === 0) reqHtml = "<p style='color:gray'>Новых заявок нет.</p>";
    d.requests.forEach(req => {
        const user = req.username ? '@'+req.username : 'ID: '+req.telegram_id;
        reqHtml += `<div class="card">
            <b>Игра/Приложение:</b> ${esc(req.game)}<br>
            <b>Юзер:</b> ${esc(user)}<br>
            <span style="font-size:12px;color:gray">${req.created_at.slice(0,16).replace('T',' ')}</span>
            <br><small style="color:var(--accent)">Нажмите на картинку, чтобы открыть в полном размере:</small>
            <a href="/screenshots/${req.photo_filename}" onclick="window.Telegram.WebApp.openLink('/screenshots/' + '${req.photo_filename}'); return false;">
                <img src="/screenshots/${req.photo_filename}" class="req-img" title="Нажмите, чтобы открыть">
            </a>
            <div style="display:flex;gap:10px">
                <button class="btn btn-green" onclick="resolveReq(${req.id}, 'approved')">Одобрить</button>
                <button class="btn btn-red" onclick="resolveReq(${req.id}, 'rejected')">Отклонить</button>
            </div>
        </div>`;
    });
    document.getElementById("requestsBox").innerHTML = reqHtml;

    let supHtml = "";
    if (!d.tickets || d.tickets.length === 0) {
        supHtml = "<p style='color:gray'>Обращений в поддержку нет.</p>";
    } else {
        d.tickets.forEach(t => {
            const userStr = t.username ? '@'+t.username : 'ID: '+t.telegram_id;
            supHtml += `<div class="card">
                <b>Пользователь:</b> ${esc(userStr)}<br>
                <b>Вопрос:</b> ${esc(t.question)}<br>
                <span style="font-size:12px;color:gray">${t.created_at.slice(0,16).replace('T',' ')}</span>
                <div style="margin-top:10px;">
                    <input type="text" id="answer_${t.id}" class="field" placeholder="Введите ответ..." value="${esc(t.answer || '')}">
                    <button class="btn btn-green" style="margin-top:0;" onclick="sendSupportAnswer(${t.id})">Отправить ответ</button>
                </div>
            </div>`;
        });
    }
    document.getElementById("supportBox").innerHTML = supHtml;

    let accHtml = "";
    d.accounts.forEach(g => {
        const isAssigned = g.assigned_to_id && g.assigned_to_id > 0;
        const assignedInfo = isAssigned ? `<br><span style="color:var(--accent)">Выдан пользователю: @${esc(g.assigned_to_username || g.assigned_to_name || g.assigned_to_id)}</span>` : `<br><span style="color:gray">Свободен</span>`;
        
        accHtml += `<div class="card" style="padding:12px">
            <b>${esc(g.email)}</b><br><code style="color:gray">${esc(g.password)}</code>
            ${assignedInfo}
            <div style="display:flex; gap:8px; margin-top:8px;">
                ${isAssigned ? `<button class="btn btn-gold" style="padding:6px; margin-top:0;" onclick="freeAccount(${g.id})">Освободить</button>` : ''}
                <button class="btn btn-red" style="padding:6px; margin-top:0;" onclick="deleteAccount(${g.id})">Удалить</button>
            </div>
        </div>`;
    });
    document.getElementById("accountsBox").innerHTML = accHtml || "<p>Пусто</p>";

    let admHtml = "";
    d.admins.forEach(a => {
        admHtml += `<div class="card" style="padding:12px; display:flex; justify-content:space-between; align-items:center">
            <b>@${esc(a.username)}</b>
            <button class="btn btn-red" style="width:auto; margin:0; padding:6px 12px" onclick="removeAdmin('${esc(a.username)}')">Удалить</button>
        </div>`;
    });
    document.getElementById("adminsBox").innerHTML = admHtml;

    let bansHtml = "";
    d.bans.forEach(b => {
        bansHtml += `<div class="card" style="padding:12px; display:flex; justify-content:space-between; align-items:center">
            <b>${esc(b.target)}</b>
            <button class="btn btn-green" style="width:auto; margin:0; padding:6px 12px" onclick="removeBan('${esc(b.target)}')">Разбанить</button>
        </div>`;
    });
    document.getElementById("bansBox").innerHTML = bansHtml || "<p style='color:gray'>Бан-лист пуст.</p>";
}

async function resolveReq(id, action) {
    await fetch("/api/admin/requests/resolve", { method: "POST", headers: hdrs(), body: JSON.stringify({id, action}) });
    loadData();
}

async function sendSupportAnswer(ticketId) {
    const answer = document.getElementById("answer_" + ticketId).value.trim();
    if (!answer) return alert("Введите текст ответа!");
    await fetch("/api/admin/support/answer", {
        method: "POST",
        headers: hdrs(),
        body: JSON.stringify({ id: ticketId, answer })
    });
    alert("Ответ отправлен пользователю!");
    loadData();
}

async function addAccount() {
    const email = document.getElementById("newEmail").value.trim();
    const pass = document.getElementById("newPass").value.trim();
    if(!email || !pass) return;
    await fetch("/api/admin/add_account", { method: "POST", headers: hdrs(), body: JSON.stringify({email, password: pass}) });
    document.getElementById("newEmail").value = "";
    document.getElementById("newPass").value = "";
    loadData();
}

async function deleteAccount(id) {
    if(confirm("Удалить аккаунт из базы?")) {
        await fetch("/api/admin/delete_account", { method: "POST", headers: hdrs(), body: JSON.stringify({id}) });
        loadData();
    }
}

async function freeAccount(id) {
    if(confirm("Освободить этот аккаунт?")) {
        await fetch("/api/admin/free_account", { method: "POST", headers: hdrs(), body: JSON.stringify({id}) });
        loadData();
    }
}

async function addAdmin() {
    const username = document.getElementById("newAdmin").value.trim().replace('@', '');
    if(!username) return;
    await fetch("/api/admin/admins/add", { method: "POST", headers: hdrs(), body: JSON.stringify({username}) });
    document.getElementById("newAdmin").value = "";
    loadData();
}

async function removeAdmin(username) {
    if(confirm("Лишить прав администратора @"+username+"?")) {
        await fetch("/api/admin/admins/remove", { method: "POST", headers: hdrs(), body: JSON.stringify({username}) });
        loadData();
    }
}

async function addBan() {
    const target = document.getElementById("newBan").value.trim().replace('@', '').toLowerCase();
    if(!target) return;
    await fetch("/api/admin/bans/add", { method: "POST", headers: hdrs(), body: JSON.stringify({target}) });
    document.getElementById("newBan").value = "";
    loadData();
}

async function removeBan(target) {
    if(confirm("Разблокировать пользователя " + target + "?")) {
        await fetch("/api/admin/bans/remove", { method: "POST", headers: hdrs(), body: JSON.stringify({target}) });
        loadData();
    }
}

window.onload = loadData;
</script>
</body></html>
"""
@web.get("/health")
def health():
    return "ok", 200

@web.get("/")
def home():
    return render_template_string(PAGE)

@web.get("/privacy")
def privacy_page():
    return render_template_string(PRIVACY_PAGE)

@web.get("/admin")
def admin_page():
    return render_template_string(ADMIN_PAGE)

@web.get("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory(STATIC_DIR, filename)

@web.get("/api/my_status")
def api_my_status():
    user = identify_user()
    if not user: return jsonify(status="none")
    
    if is_user_banned(user["id"], user.get("username")):
        return jsonify(status="banned")
        
    with db() as conn:
        creds = conn.execute("SELECT email, password FROM generations WHERE assigned_to_id = ? LIMIT 1", (user["id"],)).fetchone()
        if creds:
            return jsonify(status="fulfilled", creds={"email": creds["email"], "password": creds["password"]}, is_admin=is_admin(user.get("username")))
            
        req = conn.execute("SELECT status, game FROM requests WHERE telegram_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
        status = req["status"] if req else "none"
        
    return jsonify(status=status, is_admin=is_admin(user.get("username")))

@web.post("/api/support/send")
def api_support_send():
    user = identify_user()
    if not user: return abort(403)
    if is_user_banned(user["id"], user.get("username")): return abort(403)
    
    data = request.json
    question = (data.get("question") or "").strip()
    if not question: return abort(400)
    
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute("INSERT INTO support_tickets(telegram_id, question, created_at) VALUES (?, ?, ?)", (user["id"], question, now))
        
    username_str = f"@{user['username']}" if user.get("username") else f"ID {user['id']}"
    threading.Thread(target=notify_admins_sync, args=(f"💬 <b>Новый вопрос в поддержку!</b>\nОт: {username_str}\nВопрос: {question}\n\nЗайдите в админ-панель для ответа.",), daemon=True).start()
    return jsonify(ok=True)

@web.get("/api/support/list")
def api_support_list():
    user = identify_user()
    if not user: return abort(403)
    
    with db() as conn:
        tickets = [dict(r) for r in conn.execute("SELECT id, question, answer, status, created_at FROM support_tickets WHERE telegram_id = ? ORDER BY id DESC", (user["id"],)).fetchall()]
    return jsonify(tickets=tickets)

@web.post("/api/request")
def api_upload_request():
    user = identify_user()
    if not user: return abort(403)
    
    if is_user_banned(user["id"], user.get("username")):
        return abort(403)
        
    file = request.files.get("photo")
    game = request.form.get("game", "Unknown")
    if not file: return abort(400)
    
    allowed_extensions = {'.jpg', '.jpeg', '.png', '.webp', '.heic'}
    file_ext = os.path.splitext(file.filename)[1].lower()
    
    if file_ext not in allowed_extensions or not file.content_type.startswith('image/'):
        return jsonify(error="Разрешено загружать только изображения (скриншоты)."), 400
    
    filename = f"req_{int(time.time())}_{user['id']}{file_ext}"
    filepath = SCREENSHOTS_DIR / filename
    file.save(filepath)
    
    with db() as conn:
        conn.execute("INSERT INTO requests(telegram_id, game, photo_filename, created_at) VALUES (?, ?, ?, ?)",
                     (user["id"], game, filename, datetime.now(timezone.utc).isoformat()))
        
    username_str = f"@{user['username']}" if user.get("username") else f"ID {user['id']}"
    threading.Thread(target=notify_admins_sync, args=(f"🆕 <b>Новая заявка!</b>\nПользователь: {username_str}\nВыбор: {game}\n\nЗайдите в админ-панель для проверки.",), daemon=True).start()
    return jsonify(ok=True)

@web.post("/api/credentials")
def api_credentials():
    user = identify_user()
    if not user: return abort(403)
    
    if is_user_banned(user["id"], user.get("username")):
        return abort(403)
        
    with db() as conn:
        req = conn.execute("SELECT status FROM requests WHERE telegram_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
        if not req or req["status"] != "approved":
            return jsonify(error="Заявка еще не одобрена админом."), 400
            
    email, password, status = assign_email_for_user(user["id"], user.get("username"), user.get("first_name"), "site")
    
    if status == "no_available":
        return jsonify(error="К сожалению, сейчас нет свободных аккаунтов. Админ скоро их добавит."), 400
        
    with db() as conn:
        conn.execute("UPDATE requests SET status = 'fulfilled' WHERE telegram_id = ? AND status = 'approved'", (user["id"],))
        
    if status == "success":
        username_str = f"@{user['username']}" if user.get("username") else f"ID {user['id']}"
        msg = f"📤 <b>Аккаунт выдан!</b>\nПользователь: {username_str}\nПочта: <code>{email}</code>\nПароль: <code>{password}</code>"
        threading.Thread(target=notify_admins_sync, args=(msg,), daemon=True).start()
        
    return jsonify(email=email, password=password, saved=True)

@web.get("/screenshots/<filename>")
def serve_screenshot(filename):
    safe_name = os.path.basename(filename)
    path = SCREENSHOTS_DIR / safe_name
    if path.exists():
        return send_file(path)
    abort(404)

TUTORIALS = {
    "icloud": "video_1.mp4",
    "login": "video_2.mp4"
}

@web.get("/tutorial_media/<key>")
def tutorial_media(key):
    possible_names = {
        "icloud": ["video_1.mp4", "video_1.MP4", "IMG_1448.mp4", "IMG_1448.MP4"],
        "login": ["video_2.mp4", "video_2.MP4", "IMG_3283.mp4", "IMG_3283.MP4"]
    }
    
    filenames = possible_names.get(key, [])
    target_path = None
    
    for name in filenames:
        p1 = ASSETS / name
        p2 = ROOT / name
        
        if p1.exists():
            target_path = p1
            break
        elif p2.exists():
            target_path = p2
            break
            
    if not target_path:
        log.error(f"❌ ОШИБКА: Видео для ключа '{key}' не найдено ни в assets, ни в корне!")
        abort(404)
        
    return send_file(target_path, mimetype="video/mp4", conditional=True)

@web.get("/api/admin/data")
def api_admin_data():
    user = identify_user()
    if not user or not is_admin(user.get("username")): abort(403)
    
    with db() as conn:
        reqs = [dict(r) for r in conn.execute("""
            SELECT r.*, u.username FROM requests r 
            LEFT JOIN users u ON r.telegram_id = u.telegram_id 
            WHERE r.status = 'pending' ORDER BY r.id ASC
        """).fetchall()]
        tickets = [dict(r) for r in conn.execute("""
            SELECT t.*, u.username FROM support_tickets t
            LEFT JOIN users u ON t.telegram_id = u.telegram_id
            WHERE t.status = 'open' ORDER BY t.id ASC
        """).fetchall()]
        all_accs = [dict(r) for r in conn.execute("SELECT * FROM generations ORDER BY id DESC").fetchall()]
        admins = [dict(r) for r in conn.execute("SELECT * FROM admins").fetchall()]
        bans = [dict(r) for r in conn.execute("SELECT * FROM banned_users").fetchall()]
        
    return jsonify(requests=reqs, tickets=tickets, accounts=all_accs, admins=admins, bans=bans)

@web.post("/api/admin/requests/resolve")
def api_resolve_req():
    user = identify_user()
    if not user or not is_admin(user.get("username")): abort(403)
    
    data = request.json
    req_id = data.get("id")
    action = data.get("action")
    
    with db() as conn:
        req = conn.execute("SELECT telegram_id FROM requests WHERE id = ?", (req_id,)).fetchone()
        if req:
            conn.execute("UPDATE requests SET status = ? WHERE id = ?", (action, req_id))
            text = "✅ <b>Твоя заявка одобрена!</b> Зайди в приложение, чтобы забрать почту и пароль." if action == 'approved' else "❌ <b>Твоя заявка отклонена.</b> Скриншот не подошел, попробуй еще раз в приложении."
            threading.Thread(target=send_tg_message_sync, args=(req["telegram_id"], text), daemon=True).start()
            
    return jsonify(ok=True)

@web.post("/api/admin/support/answer")
def api_admin_support_answer():
    user = identify_user()
    if not user or not is_admin(user.get("username")): abort(403)
    
    data = request.json
    ticket_id = data.get("id")
    answer = (data.get("answer") or "").strip()
    if not ticket_id or not answer: return abort(400)
    
    with db() as conn:
        ticket = conn.execute("SELECT telegram_id, question FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
        if ticket:
            conn.execute("UPDATE support_tickets SET answer = ?, status = 'answered' WHERE id = ?", (answer, ticket_id))
            text = f"💬 <b>Ответ поддержки на ваш вопрос:</b>\n<i>«{ticket['question']}»</i>\n\n<b>Ответ:</b> {answer}"
            threading.Thread(target=send_tg_message_sync, args=(ticket["telegram_id"], text), daemon=True).start()
            
    return jsonify(ok=True)

@web.post("/api/admin/add_account")
def api_admin_add_acc():
    user = identify_user()
    if not user or not is_admin(user.get("username")): abort(403)
    data = request.json
    if data.get("email") and data.get("password"):
        with db() as conn:
            conn.execute("INSERT INTO generations(email, password, source, created_at) VALUES (?, ?, 'вручную', ?)",
                         (data["email"], data["password"], datetime.now(timezone.utc).isoformat()))
    return jsonify(ok=True)

@web.post("/api/admin/delete_account")
def api_admin_del_acc():
    user = identify_user()
    if not user or not is_admin(user.get("username")): abort(403)
    with db() as conn:
        conn.execute("DELETE FROM generations WHERE id = ?", (request.json.get("id"),))
    return jsonify(ok=True)

@web.post("/api/admin/free_account")
def api_admin_free_acc():
    user, data = identify_user(), request.json
    if not user or not is_admin(user.get("username")): abort(403)
    acc_id = data.get("id")
    with db() as conn:
        conn.execute("""
            UPDATE generations 
            SET assigned_to_id = NULL, assigned_to_username = NULL, assigned_to_name = NULL, assigned_to_at = NULL 
            WHERE id = ?
        """, (acc_id,))
    return jsonify(ok=True)

@web.post("/api/admin/admins/add")
def api_admin_add():
    user = identify_user()
    if not user or not is_admin(user.get("username")): abort(403)
    username = request.json.get("username", "").strip().lower()
    if username:
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO admins(username, added_at) VALUES (?, ?)", (username, datetime.now(timezone.utc).isoformat()))
    return jsonify(ok=True)

@web.post("/api/admin/admins/remove")
def api_admin_remove():
    user = identify_user()
    if not user or not is_admin(user.get("username")): abort(403)
    username = request.json.get("username", "").strip().lower()
    with db() as conn:
        conn.execute("DELETE FROM admins WHERE username = ?", (username,))
    return jsonify(ok=True)

@web.post("/api/admin/bans/add")
def api_admin_ban_add():
    user = identify_user()
    if not user or not is_admin(user.get("username")): abort(403)
    target = request.json.get("target", "").strip().lower()
    if target:
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO banned_users(target, banned_at) VALUES (?, ?)", (target, datetime.now(timezone.utc).isoformat()))
    return jsonify(ok=True)

@web.post("/api/admin/bans/remove")
def api_admin_ban_remove():
    user = identify_user()
    if not user or not is_admin(user.get("username")): abort(403)
    target = request.json.get("target", "").strip().lower()
    with db() as conn:
        conn.execute("DELETE FROM banned_users WHERE target = ?", (target,))
    return jsonify(ok=True)

def run_web():
    port = int(os.getenv("PORT", "5000"))
    web.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

async def main():
    init_db()
    log.info("✅ База данных успешно инициализирована")
    
    threading.Thread(target=run_web, daemon=True).start()
    log.info("✅ Веб-сервер запущен (порт 5000)")
    
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    log.info("🤖 Бот запущен и готов к работе!")
    
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
