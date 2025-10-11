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
# CONFIG
# =============================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
REVELATION_KEY = os.getenv("REVELATION_KEY")

if not BOT_TOKEN or not DATABASE_URL or not REVELATION_KEY:
    raise RuntimeError("Missing required environment variables.")

fernet = Fernet(REVELATION_KEY)
sg_timezone = pytz.timezone("Asia/Singapore")

REMINDER_MESSAGES = [
    "🕊️ A nudge for QT — you got this!",
    "✨ QT reminder — take a quiet moment today.",
    "📖 Daily bread check-in — QT time?",
    "⏰ Gentle reminder: Have you done your QT?",
]

# in-memory tracking
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
    # ensure users table has reminder columns
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        name TEXT,
        current_streak INTEGER,
        longest_streak INTEGER,
        last_date TEXT,
        reminder_hour INTEGER,
        reminder_minute INTEGER
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
        INSERT INTO users (user_id, name, current_streak, longest_streak, last_date, reminder_hour, reminder_minute)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name
    """, (str(user_id), name, 0, 0, None, 21, 0))
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
    UPDATE users SET
      name = %s,
      current_streak = %s,
      longest_streak = %s,
      last_date = %s
    WHERE user_id = %s
    """, (name, streak, longest, last_date, str(user_id)))
    conn.commit()
    conn.close()

def update_user_reminder(user_id: int, hour: int, minute: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET reminder_hour=%s, reminder_minute=%s WHERE user_id=%s", (hour, minute, str(user_id)))
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

    result = []
    for date, enc in rows:
        try:
            result.append((date, fernet.decrypt(enc.encode()).decode()))
        except:
            result.append((date, "⚠️ Unable to decrypt."))
    return result

def get_all_user_ids_and_names():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, name, reminder_hour, reminder_minute FROM users")
    rows = c.fetchall()
    conn.close()
    return rows

# =============================
# HELPERS
# =============================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("✅ Mark QT Done", callback_data="yes")],
        [InlineKeyboardButton("📖 View History", callback_data="history"),
         InlineKeyboardButton("⏰ Set Reminder", callback_data="setrem")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")]
    ]
    return InlineKeyboardMarkup(keyboard)

def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_to_menu")]])

def streak_visual(streak: int) -> str:
    total = 7
    rem = streak % total or (7 if streak > 0 else 0)
    return "🔥" * rem + "⚪" * (total - rem)

def streak_message(current: int, longest: int, hour=None, minute=None) -> str:
    msg = f"{streak_visual(current)}\nCurrent streak: {current} days\nLongest streak: {longest} days"
    if hour is not None:
        msg += f"\n\n🔔 Daily reminder set for {hour:02d}:{minute:02d}"
    return msg

def cancel_jobs(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    for n in [f"reminder_{user_id}", f"followup_{user_id}"]:
        for j in context.job_queue.get_jobs_by_name(n):
            j.schedule_removal()

def schedule_user_reminder(app, user_id: int, hour: int, minute: int):
    now = datetime.now(sg_timezone)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    next_fire = target if target > now else target + timedelta(days=1)
    for j in app.job_queue.get_jobs_by_name(f"reminder_{user_id}"):
        j.schedule_removal()
    app.job_queue.run_once(reminder_job_once, when=(next_fire - now),
                           chat_id=user_id, name=f"reminder_{user_id}")

# =============================
# COMMANDS
# =============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user_record(user.id, user.first_name)
    row = get_user(user.id)
    current, longest, last_date, _, hour, minute = row
    msg = streak_message(current, longest, hour, minute)
    await update.message.reply_text(
        f"Hello {user.first_name}! 🙌\nI’m **ZN3 PrayerBot**.\nLet’s grow together in our commitment and faith 🙏👋",
        parse_mode="Markdown",
    )
    await update.message.reply_text(f"🙏 Welcome back!\n{msg}", reply_markup=main_menu_keyboard())

# =============================
# BUTTON HANDLER
# =============================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_name = query.from_user.first_name
    data = query.data

    ensure_user_record(user_id, user_name)

    if data == "yes":
        user_qt_done[user_id] = True
        cancel_jobs(context, user_id)

        today = datetime.now(sg_timezone).strftime("%d/%m/%y")
        user = get_user(user_id)
        current, longest, last_date, _, hour, minute = user
        if last_date == today:
            pass
        elif last_date == (datetime.now(sg_timezone) - timedelta(days=1)).strftime("%d/%m/%y"):
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        update_user(user_id, user_name, current, longest, today)

        await query.edit_message_text("Awesome 🙌 Please type your revelation for today:", reply_markup=back_keyboard())
        return

    if data == "history":
        rows = get_revelations(user_id)
        if not rows:
            text = "📭 You have no saved revelations yet."
        else:
            text = "\n\n".join([f"📝 {d}: {t}" for d, t in rows])
            text = f"📖 Your past revelations:\n\n{text}"
        await query.edit_message_text(text, reply_markup=back_keyboard())
        return

    if data == "setrem":
        await query.edit_message_text(
            "🕰 Please send your preferred reminder time in 24-hour format.\nExample: 08:00 or 21:15.\n⚠️ Must be before 23:30.",
            reply_markup=back_keyboard()
        )
        context.user_data["awaiting_reminder"] = True
        return

    if data == "leaderboard":
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name, current_streak, longest_streak FROM users ORDER BY current_streak DESC, longest_streak DESC")
        rows = c.fetchall()
        conn.close()
        if not rows:
            text = "📭 No streaks recorded yet."
        else:
            leaderboard = "\n".join([f"{i+1}. {n or 'Unknown'} — 🔥 {s} (Longest: {l})" for i, (n, s, l) in enumerate(rows)])
            text = f"🏆 Leaderboard:\n\n{leaderboard}"
        await query.edit_message_text(text, reply_markup=back_keyboard())
        return

    if data == "back_to_menu":
        row = get_user(user_id)
        if row:
            current, longest, last_date, _, hour, minute = row
            msg = streak_message(current, longest, hour, minute)
            await query.edit_message_text(f"🙏 Welcome back!\n{msg}", reply_markup=main_menu_keyboard())
        return

# =============================
# MESSAGE HANDLER
# =============================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()

    if context.user_data.get("awaiting_reminder"):
        context.user_data["awaiting_reminder"] = False
        text = text.replace(" ", "").replace(".", ":")
        if ":" not in text:
            await update.message.reply_text("❌ Invalid format. Use HH:MM (e.g., 08:00 or 21:15).")
            return
        try:
            hour, minute = map(int, text.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
            if hour == 23 and minute > 30:
                await update.message.reply_text("⚠️ Reminder must be before 23:30.")
                return
            update_user_reminder(user_id, hour, minute)
            schedule_user_reminder(update.get_bot(), user_id, hour, minute)
            await update.message.reply_text(f"✅ Reminder set for {hour:02d}:{minute:02d} daily.", reply_markup=back_keyboard())
        except:
            await update.message.reply_text("❌ Invalid format. Use HH:MM (e.g., 08:00 or 21:15).")
        return

    if user_qt_done.get(user_id, False):
        today = datetime.now(sg_timezone).strftime("%d/%m/%y")
        add_revelation(user_id, today, text)
        user = get_user(user_id)
        current, longest, last_date, _, hour, minute = user
        msg = streak_message(current, longest, hour, minute)
        await update.message.reply_text(f"🙏 Revelation saved!\n{msg}", reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text("Please choose an option below:", reply_markup=main_menu_keyboard())

# =============================
# JOBS (2-step reminder system)
# =============================

async def reminder_job_once(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.chat_id
    if user_qt_done.get(user_id, False):
        return
    message = random.choice(REMINDER_MESSAGES)
    await context.bot.send_message(chat_id=user_id, text=message, reply_markup=main_menu_keyboard())
    # schedule follow-up 1 hour later
    for j in context.job_queue.get_jobs_by_name(f"followup_{user_id}"):
        j.schedule_removal()
    context.job_queue.run_once(reminder_followup, when=timedelta(hours=1),
                               chat_id=user_id, name=f"followup_{user_id}")

async def reminder_followup(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.chat_id
    if not user_qt_done.get(user_id, False):
        await context.bot.send_message(chat_id=user_id,
                                       text="🙏 Hello! Just checking: QT done yet?",
                                       reply_markup=main_menu_keyboard())

async def nightly_reset_job(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(sg_timezone).strftime("%d/%m/%y")
    yesterday = (datetime.now(sg_timezone) - timedelta(days=1)).strftime("%d/%m/%y")
    for uid, _, hour, minute in get_all_user_ids_and_names():
        row = get_user(int(uid))
        if not row:
            continue
        current, longest, last_date, name, _, _ = row
        if last_date != yesterday:
            if current > 0:
                update_user(uid, name, 0, longest, last_date)
                try:
                    await context.bot.send_message(chat_id=int(uid),
                                                   text="New day 🌅 Your streak reset, but it’s never too late to build it back! 💪🔥")
                except:
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
    app.job_queue.run_daily(nightly_reset_job, time=time(0, 0, tzinfo=singapore_tz))

    # Re-schedule reminders for all users
    for uid, _, hour, minute in get_all_user_ids_and_names():
        schedule_user_reminder(app, int(uid), hour, minute)

    print("🤖 ZN3 PrayerBot running (Postgres + 2-step reminders + daily reset)...")
    app.run_polling()

if __name__ == "__main__":
    main()
