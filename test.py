import os
import random
import psycopg2
from cryptography.fernet import Fernet
from datetime import timedelta, time, datetime
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =============================
# CONFIG & ENV
# =============================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
REVELATION_KEY = os.getenv("REVELATION_KEY")

if not BOT_TOKEN or not DATABASE_URL or not REVELATION_KEY:
    raise RuntimeError("Missing required env vars: BOT_TOKEN, DATABASE_URL, REVELATION_KEY")

fernet = Fernet(REVELATION_KEY)
sg_timezone = pytz.timezone("Asia/Singapore")

REMINDER_MESSAGES = [
    "⏰ Gentle reminder: Have you done your QT?",
    "📖 Daily bread check-in — QT time?",
    "✨ QT reminder — take a quiet moment today.",
    "🙏 Hello! Just checking: QT done yet?",
    "🕊️ A nudge for QT — you got this!"
]

user_qt_done: dict[int, bool] = {}
user_jobs: dict[int, object] = {}
user_waiting_for_time: dict[int, bool] = {}  # track if user is setting reminder time

# =============================
# DATABASE
# =============================
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        name TEXT,
        current_streak INTEGER,
        longest_streak INTEGER,
        last_date TEXT,
        reminder_hour INTEGER DEFAULT 21,
        reminder_minute INTEGER DEFAULT 0
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS revelations (
        id SERIAL PRIMARY KEY,
        user_id TEXT,
        date TEXT,
        text TEXT
    )
    """)
    conn.commit()
    conn.close()

def ensure_user_record(user_id: int, name: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (user_id, name, current_streak, longest_streak, last_date)
        VALUES (%s, %s, 0, 0, NULL)
        ON CONFLICT (user_id) DO UPDATE SET
            name = EXCLUDED.name
    """, (str(user_id), name))
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT current_streak, longest_streak, last_date, name, reminder_hour, reminder_minute
        FROM users WHERE user_id=%s
    """, (str(user_id),))
    row = c.fetchone()
    conn.close()
    return row

def update_user(user_id: int, name: str, streak: int, longest: int, last_date: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (user_id, name, current_streak, longest_streak, last_date)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
          name = EXCLUDED.name,
          current_streak = EXCLUDED.current_streak,
          longest_streak = EXCLUDED.longest_streak,
          last_date = EXCLUDED.last_date
    """, (str(user_id), name, streak, longest, last_date))
    conn.commit()
    conn.close()

def update_user_reminder(user_id: int, hour: int, minute: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        UPDATE users SET reminder_hour=%s, reminder_minute=%s WHERE user_id=%s
    """, (hour, minute, str(user_id)))
    conn.commit()
    conn.close()

def get_all_user_ids_and_names():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, COALESCE(name, 'friend'), reminder_hour, reminder_minute FROM users")
    rows = c.fetchall()
    conn.close()
    return rows

def add_revelation(user_id: int, date: str, text: str):
    encrypted_text = fernet.encrypt(text.encode()).decode()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO revelations (user_id, date, text) VALUES (%s, %s, %s)",
              (str(user_id), date, encrypted_text))
    conn.commit()
    conn.close()

def get_revelations(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT date, text FROM revelations WHERE user_id=%s ORDER BY id ASC", (str(user_id),))
    rows = c.fetchall()
    conn.close()

    decrypted = []
    for date, encrypted_text in rows:
        try:
            text = fernet.decrypt(encrypted_text.encode()).decode()
        except Exception:
            text = "⚠️ Unable to decrypt (corrupted entry)"
        decrypted.append((date, text))
    return decrypted

def get_all_streaks():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, current_streak, longest_streak FROM users ORDER BY current_streak DESC, longest_streak DESC")
    rows = c.fetchall()
    conn.close()
    return rows

# =============================
# HELPERS
# =============================
def yes_no_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes", callback_data="yes"), InlineKeyboardButton("❌ No", callback_data="no")]
    ])

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Mark QT Done", callback_data="yes"),
         InlineKeyboardButton("❌ Not Yet", callback_data="no")],
        [InlineKeyboardButton("📖 View History", callback_data="history"),
         InlineKeyboardButton("⏰ Set Reminder", callback_data="set_reminder")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")]
    ])

def streak_visual(streak: int) -> str:
    remainder = streak % 7 if streak else 0
    if remainder == 0 and streak > 0:
        remainder = 7
    return "🔥" * remainder + "⚪" * (7 - remainder)

def streak_message(current: int, longest: int) -> str:
    msg = f"{streak_visual(current)}\nCurrent streak: {current} days\nLongest streak: {longest} days"
    return msg

# =============================
# COMMANDS
# =============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "there"
    ensure_user_record(user_id, user_name)

    context.bot_data[user_id] = user_name
    user_qt_done[user_id] = user_qt_done.get(user_id, False)

    intro = f"Hello {user_name}! 🙌\nI’m **ZN3 PrayerBot**.\nLet’s grow together in our commitment and faith 🙏👋"
    await update.message.reply_text(intro, parse_mode="Markdown")
    await update.message.reply_text(f"Have you done your QT today?", reply_markup=yes_no_keyboard())

# =============================
# CUSTOM REMINDER HANDLER
# =============================
async def prompt_reminder_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    user_waiting_for_time[user_id] = True
    await update.callback_query.edit_message_text(
        "🕓 Please send your preferred reminder time in 24-hour format.\n"
        "Example: 08:00 or 21:15\n⚠️ Must be before 23:30."
    )

def smart_parse_time(text: str):
    text = text.strip().replace(".", ":")
    if ":" not in text:
        if len(text) <= 2:
            return int(text), 0
        elif len(text) == 3:
            return int(text[0]), int(text[1:])
        elif len(text) == 4:
            return int(text[:2]), int(text[2:])
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError
    hour = int(parts[0])
    minute = int(parts[1])
    return hour, minute

# =============================
# MESSAGE HANDLER
# =============================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    # handle reminder time input
    if user_waiting_for_time.get(user_id, False):
        try:
            hour, minute = smart_parse_time(text)
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                raise ValueError
            if hour == 23 and minute > 30:
                await update.message.reply_text("⚠️ Time must be before 23:30.")
                return
            update_user_reminder(user_id, hour, minute)
            user_waiting_for_time[user_id] = False
            await update.message.reply_text(f"✅ Reminder set for {hour:02d}:{minute:02d} daily.")
        except Exception:
            await update.message.reply_text("❌ Invalid time format. Try again (e.g., 08:00 or 21:15).")
        return

    # handle revelation input
    user_name = update.effective_user.first_name or "Unknown"
    ensure_user_record(user_id, user_name)

    if user_qt_done.get(user_id, False):
        today = datetime.now(sg_timezone).strftime("%d/%m/%y")
        add_revelation(user_id, today, text)
        user = get_user(user_id)
        current, longest, _, _, _, _ = user
        msg = streak_message(current, longest)
        await update.message.reply_text(f"🙏 Revelation saved!\n{msg}", reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text("Please choose an option below:", reply_markup=yes_no_keyboard())

# =============================
# CALLBACK HANDLER
# =============================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    user_name = q.from_user.first_name or "Unknown"
    data = q.data
    ensure_user_record(user_id, user_name)

    if data == "set_reminder":
        await prompt_reminder_time(update, context)
        return

    if data == "yes":
        user_qt_done[user_id] = True
        today = datetime.now(sg_timezone).strftime("%d/%m/%y")
        user = get_user(user_id)
        if user:
            current, longest, last_date, _, _, _ = user
            if last_date != today:
                if last_date == (datetime.now(sg_timezone) - timedelta(days=1)).strftime("%d/%m/%y"):
                    current += 1
                else:
                    current = 1
                longest = max(longest, current)
            update_user(user_id, user_name, current, longest, today)
        else:
            update_user(user_id, user_name, 1, 1, today)
        await q.edit_message_text("Awesome 🙌 Please type your revelation for today:")
        return

    if data == "no":
        user_qt_done[user_id] = False
        await q.edit_message_text("⏳ No worries — take your time. I’ll check in later.", reply_markup=main_menu_keyboard())
        return

    if data == "leaderboard":
        rows = get_all_streaks()
        text = "\n".join([f"{i+1}. {n or 'Unknown'} — 🔥 {s} (Longest: {l})" for i,(n,s,l) in enumerate(rows)])
        await q.edit_message_text(f"📊 Leaderboard:\n\n{text}", reply_markup=main_menu_keyboard())
        return

# =============================
# JOBS
# =============================
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.chat_id
    msg = random.choice(REMINDER_MESSAGES)
    await context.bot.send_message(chat_id=user_id, text=msg, reply_markup=main_menu_keyboard())

async def nightly_reset_job(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(sg_timezone).strftime("%d/%m/%y")
    yesterday = (datetime.now(sg_timezone) - timedelta(days=1)).strftime("%d/%m/%y")

    for user_id_str, name, _, _ in get_all_user_ids_and_names():
        uid = int(user_id_str)
        user = get_user(uid)
        if not user: continue
        current, longest, last_date, *_ = user
        if last_date != yesterday and current > 0:
            update_user(uid, name, 0, longest, last_date)
            await context.bot.send_message(uid, "New day, new start 🌅 Your streak reset overnight. You got this! 💯🔥")

# =============================
# MAIN
# =============================
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    singapore_tz = pytz.timezone("Asia/Singapore")

    # midnight reset
    app.job_queue.run_daily(nightly_reset_job, time=time(hour=0, minute=0, tzinfo=singapore_tz))

    # reschedule per-user reminders from DB
    for uid, _, hour, minute in get_all_user_ids_and_names():
        app.job_queue.run_daily(reminder_job, time=time(hour=hour, minute=minute, tzinfo=singapore_tz), chat_id=int(uid))

    print("🤖 ZN3 PrayerBot running on Railway (custom reminders + midnight reset + encryption + smart time parser)…")
    app.run_polling()

if __name__ == "__main__":
    main()
