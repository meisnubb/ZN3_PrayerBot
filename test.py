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

# Track running state (not persisted)
user_qt_done: dict[int, bool] = {}
user_jobs: dict[int, object] = {}

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
        last_date TEXT
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
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name
    """, (str(user_id), name, 0, 0, None))
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT current_streak, longest_streak, last_date, name FROM users WHERE user_id=%s", (str(user_id),))
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

    decrypted_rows = []
    for date, encrypted_text in rows:
        try:
            decrypted_text = fernet.decrypt(encrypted_text.encode()).decode()
        except Exception:
            decrypted_text = "⚠️ Unable to decrypt (corrupted entry)"
        decrypted_rows.append((date, decrypted_text))
    return decrypted_rows

def get_all_streaks():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT name, current_streak, longest_streak
        FROM users
        ORDER BY current_streak DESC, longest_streak DESC, COALESCE(name, '') ASC
    """)
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_user_ids_and_names():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, COALESCE(name, 'friend') FROM users")
    rows = c.fetchall()
    conn.close()
    return rows

# =============================
# HELPERS
# =============================

def yes_no_keyboard() -> InlineKeyboardMarkup:
    keyboard = [[
        InlineKeyboardButton("✅ Yes", callback_data="yes"),
        InlineKeyboardButton("❌ No", callback_data="no")
    ]]
    return InlineKeyboardMarkup(keyboard)

def main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [[
        InlineKeyboardButton("✅ Mark QT Done", callback_data="yes"),
        InlineKeyboardButton("❌ Not Yet", callback_data="no"),
    ],[
        InlineKeyboardButton("📖 View History", callback_data="history"),
        InlineKeyboardButton("🔕 Cancel Reminder", callback_data="cancelrem"),
    ],[
        InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"),
    ]]
    return InlineKeyboardMarkup(keyboard)

def leaderboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_to_menu")]])

def _schedule_forced_reminder(user_id: int, context: ContextTypes.DEFAULT_TYPE, delay_hours: int = 1):
    _cancel_user_job(user_id)
    job = context.job_queue.run_once(
        reminder_job_once,
        when=timedelta(hours=delay_hours),
        chat_id=user_id,
        name=f"forced_reminder_{user_id}",
        data={"user_id": user_id}
    )
    user_jobs[user_id] = job

def _cancel_user_job(user_id: int) -> bool:
    job = user_jobs.pop(user_id, None)
    if job:
        job.schedule_removal()
        return True
    return False

def streak_visual(streak: int) -> str:
    total = 7
    remainder = streak % total
    if remainder == 0 and streak > 0:
        remainder = 7
    fire = "🔥" * remainder
    white = "⚪" * (total - remainder)
    return fire + white

def streak_message(current: int, longest: int) -> str:
    msg = f"{streak_visual(current)}\nCurrent streak: {current} days\nLongest streak: {longest} days"
    if current == 5:
        msg += "\n🌟 Congrats on 5 days!"
    elif current == 7:
        msg += "\n💪 One full week!"
    elif current == 30:
        msg += "\n🏆 A whole month!"
    elif current == 100:
        msg += "\n👑 Incredible! 100 days!"
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

    intro = (
        f"Hello {user_name}! 🙌\n"
        f"I’m **ZN3 PrayerBot**.\n"
        f"Let’s grow together in our commitment and faith 🙏👋"
    )
    question = f"Hello {user_name}! 👋\nHave you done your QT today?"

    if update.message:
        await update.message.reply_text(intro, parse_mode="Markdown")
        await update.message.reply_text(question, reply_markup=yes_no_keyboard())
    else:
        await context.bot.send_message(chat_id=user_id, text=intro, parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text=question, reply_markup=yes_no_keyboard())

# =============================
# CALLBACK HANDLER
# =============================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_name = query.from_user.first_name or "Unknown"
    data = query.data
    ensure_user_record(user_id, user_name)

    if data == "yes":
        user_qt_done[user_id] = True
        _cancel_user_job(user_id)

        today = datetime.now(sg_timezone).strftime("%d/%m/%y")
        user = get_user(user_id)
        if user:
            current, longest, last_date, _ = user
            if last_date == today:
                pass
            elif last_date == (datetime.now(sg_timezone) - timedelta(days=1)).strftime("%d/%m/%y"):
                current += 1
            else:
                current = 1
            longest = max(longest, current)
        else:
            current, longest = 1, 1
        update_user(user_id, user_name, current, longest, today)

        await query.edit_message_text(
            "Awesome 🙌 Please type your revelation for today:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_to_menu")]])
        )
        return

    if data == "no":
        user_qt_done[user_id] = False
        _schedule_forced_reminder(user_id, context, delay_hours=1)
        await query.edit_message_text(
            "⏳ Not yet? No worries — I’ll remind you in 1 hour.",
            reply_markup=main_menu_keyboard()
        )
        return

    if data == "history":
        rows = get_revelations(user_id)
        if not rows:
            text = "📭 You have no saved revelations yet."
        else:
            text = "\n\n".join([f"📝 {date}: {msg}" for date, msg in rows])
            text = f"📖 Your past revelations:\n\n{text}"
        await query.edit_message_text(text, reply_markup=leaderboard_keyboard())
        return

    if data == "leaderboard":
        rows = get_all_streaks()
        if not rows:
            await query.edit_message_text("📭 No streaks recorded yet.", reply_markup=main_menu_keyboard())
            return
        leaderboard = "\n".join([
            f"{i+1}. {name or 'Unknown'} — 🔥 {streak} (Longest: {longest})"
            for i, (name, streak, longest) in enumerate(rows)
        ])
        await query.edit_message_text(
            f"📊 Streak Leaderboard:\n\n{leaderboard}",
            reply_markup=leaderboard_keyboard()
        )
        return

    if data == "cancelrem":
        if _cancel_user_job(user_id):
            msg = "🔕 Reminder cancelled."
        else:
            msg = "ℹ️ No reminder was set."
        await query.edit_message_text(msg, reply_markup=main_menu_keyboard())
        return

    if data == "back_to_menu":
        user = get_user(user_id)
        if user:
            current, longest, last_date, _ = user
            msg = streak_message(current, longest)
            await query.edit_message_text(f"🙏 Welcome back!\n{msg}", reply_markup=main_menu_keyboard())
        else:
            await query.edit_message_text("🙏 Welcome back!", reply_markup=main_menu_keyboard())

# =============================
# MESSAGE HANDLER
# =============================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Unknown"
    ensure_user_record(user_id, user_name)

    text = (update.message.text or "").strip()
    today = datetime.now(sg_timezone).strftime("%d/%m/%y")

    user = get_user(user_id)
    if user:
        current, longest, last_date, _ = user

        # If streak reset overnight, restart it automatically when user sends revelation
        if current == 0 and last_date != today:
            current = 1
            longest = max(longest, current)
            update_user(user_id, user_name, current, longest, today)

    add_revelation(user_id, today, text)
    user = get_user(user_id)
    if user:
        current, longest, last_date, _ = user
        msg = streak_message(current, longest)
        await update.message.reply_text(f"🙏 Revelation saved!\n{msg}", reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text("🙏 Revelation saved!", reply_markup=main_menu_keyboard())

# =============================
# JOBS
# =============================

async def reminder_job_once(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.chat_id
    if user_qt_done.get(user_id, False):
        user_jobs.pop(user_id, None)
        return
    message = random.choice(REMINDER_MESSAGES)
    await context.bot.send_message(chat_id=user_id, text=message, reply_markup=main_menu_keyboard())
    user_jobs.pop(user_id, None)

async def nightly_reset_job(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(sg_timezone).strftime("%d/%m/%y")
    yesterday = (datetime.now(sg_timezone) - timedelta(days=1)).strftime("%d/%m/%y")
    users = get_all_user_ids_and_names()

    for user_id_str, name in users:
        uid = int(user_id_str)
        row = get_user(uid)
        if not row:
            continue
        current, longest, last_date, _ = row

        if last_date != yesterday:
            if current > 0:
                update_user(uid, name, 0, longest, last_date)
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text="New day, new start 🌅 Your streak reset overnight, but it’s never too late to build it back up. You got this! 💯🔥"
                    )
                except Exception:
                    pass

async def nightly_21_check(context: ContextTypes.DEFAULT_TYPE):
    users = get_all_user_ids_and_names()
    for user_id_str, name in users:
        uid = int(user_id_str)
        user_qt_done[uid] = False
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"🌙 Hello {name}, have you done your QT today?",
                reply_markup=yes_no_keyboard()
            )
        except Exception:
            pass

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

    # Midnight streak check
    app.job_queue.run_daily(
        nightly_reset_job,
        time=time(hour=0, minute=0, tzinfo=singapore_tz),
        name="nightly_reset_job"
    )
    # Daily reminder
    app.job_queue.run_daily(
        nightly_21_check,
        time=time(hour=21, minute=0, tzinfo=singapore_tz),
        name="nightly_21_check"
    )

    print("🤖 ZN3 PrayerBot running on Railway (Postgres + Encrypted Revelations + streak restore + midnight reset + 21:00 reminder)…")
    app.run_polling()

if __name__ == "__main__":
    main()
