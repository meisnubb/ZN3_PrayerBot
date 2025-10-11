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
user_waiting_for_time: dict[int, bool] = {}  # track who is setting a time

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
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name
    """, (str(user_id), name, 0, 0, None))
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT current_streak, longest_streak, last_date, name, reminder_hour, reminder_minute FROM users WHERE user_id=%s", (str(user_id),))
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

def update_user_reminder_time(user_id: int, hour: int, minute: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET reminder_hour=%s, reminder_minute=%s WHERE user_id=%s",
              (hour, minute, str(user_id)))
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
    c.execute("SELECT user_id, COALESCE(name, 'friend'), reminder_hour, reminder_minute FROM users")
    rows = c.fetchall()
    conn.close()
    return rows

# =============================
# HELPERS
# =============================

def yes_no_keyboard():
    keyboard = [[
        InlineKeyboardButton("✅ Yes", callback_data="yes"),
        InlineKeyboardButton("❌ No", callback_data="no")
    ]]
    return InlineKeyboardMarkup(keyboard)

def main_menu_keyboard():
    keyboard = [[
        InlineKeyboardButton("✅ Mark QT Done", callback_data="yes"),
        InlineKeyboardButton("❌ Not Yet", callback_data="no"),
    ],[
        InlineKeyboardButton("📖 View History", callback_data="history"),
        InlineKeyboardButton("🔕 Cancel Reminder", callback_data="cancelrem"),
    ],[
        InlineKeyboardButton("🕒 Set Reminder Time", callback_data="settime"),
        InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"),
    ]]
    return InlineKeyboardMarkup(keyboard)

def _schedule_personal_reminder(user_id: int, hour: int, minute: int, context: ContextTypes.DEFAULT_TYPE):
    """Schedules daily reminder for specific user."""
    _cancel_user_job(user_id)
    singapore_tz = pytz.timezone("Asia/Singapore")
    job_time = time(hour=hour, minute=minute, tzinfo=singapore_tz)
    job = context.job_queue.run_daily(
        send_daily_check,
        time=job_time,
        chat_id=user_id,
        name=f"user_daily_reminder_{user_id}",
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
    return "🔥" * remainder + "⚪" * (total - remainder)

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

    await update.message.reply_text(
        f"Hello {user_name}! 🙌\nI’m **ZN3 PrayerBot**.\nLet’s grow together in faith 🙏",
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        f"Have you done your QT today?",
        reply_markup=yes_no_keyboard()
    )

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

    if data == "settime":
        user_waiting_for_time[user_id] = True
        await query.edit_message_text(
            "🕒 Please send your preferred reminder time in *24-hour format* (e.g., 08:00 or 21:15).\n"
            "⚠️ Must be before 23:30.",
            parse_mode="Markdown"
        )
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
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_to_menu")]])
        )
        return

    if data == "yes":
        user_qt_done[user_id] = True
        today = datetime.now(sg_timezone).strftime("%d/%m/%y")
        user = get_user(user_id)
        if user:
            current, longest, last_date, _, _, _ = user
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

    if data == "back_to_menu":
        await query.edit_message_text("🙏 Welcome back!", reply_markup=main_menu_keyboard())
        return

# =============================
# MESSAGE HANDLER
# =============================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    if user_waiting_for_time.get(user_id):
        try:
            hour, minute = map(int, text.split(":"))
            if hour > 23 or minute > 59:
                raise ValueError
            if (hour, minute) > (23, 30):
                await update.message.reply_text("⚠️ Time must be before 23:30. Try again.")
                return
            update_user_reminder_time(user_id, hour, minute)
            _schedule_personal_reminder(user_id, hour, minute, context)
            user_waiting_for_time[user_id] = False
            await update.message.reply_text(f"✅ Reminder time set to {hour:02d}:{minute:02d} daily!", reply_markup=main_menu_keyboard())
        except Exception:
            await update.message.reply_text("❌ Invalid time format. Use HH:MM (e.g., 08:00 or 21:15).")
        return

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
# JOBS
# =============================

async def send_daily_check(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.chat_id
    name = context.bot_data.get(user_id, "friend")
    user_qt_done[user_id] = False
    await context.bot.send_message(
        chat_id=user_id,
        text=f"🌙 Hello {name}, have you done your QT today?",
        reply_markup=yes_no_keyboard()
    )

async def nightly_reset_job(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(sg_timezone).strftime("%d/%m/%y")
    yesterday = (datetime.now(sg_timezone) - timedelta(days=1)).strftime("%d/%m/%y")
    users = get_all_user_ids_and_names()
    for user_id_str, name, _, _ in users:
        uid = int(user_id_str)
        row = get_user(uid)
        if not row:
            continue
        current, longest, last_date, *_ = row
        if last_date != yesterday and current > 0:
            update_user(uid, name, 0, longest, last_date)
            await context.bot.send_message(
                chat_id=uid,
                text="New day, new start 🌅 Your streak reset overnight, but it’s never too late to build it back up. You got this! 💯🔥"
            )

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

    # Midnight streak reset
    app.job_queue.run_daily(
        nightly_reset_job,
        time=time(hour=0, minute=0, tzinfo=singapore_tz),
        name="nightly_reset_job"
    )

    print("🤖 ZN3 PrayerBot running with custom reminder times + encrypted revelations + streak logic intact…")
    app.run_polling()

if __name__ == "__main__":
    main()
