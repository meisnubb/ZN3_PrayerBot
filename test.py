import os
import random
import psycopg2
from datetime import timedelta, time, datetime
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
BOT_TOKEN = os.getenv("8332102513:AAFLXK6fdJkJyrbdTwi_lFOFk3qDaK0nL9A")
DATABASE_URL = os.getenv("postgresql://postgres:lsnvRsPDSoqqJbFEBHpSUptIEIlwLcLA@turntable.proxy.rlwy.net:28453/railway")  # Railway will inject this

REMINDER_MESSAGES = [
    "⏰ Gentle reminder: Have you done your QT?",
    "📖 Daily bread check-in — QT time?",
    "✨ QT reminder — take a quiet moment today.",
    "🙏 Hello! Just checking: QT done yet?",
    "🕊️ A nudge for QT — you got this!"
]

sg_timezone = pytz.timezone("Asia/Singapore")

# Track running state only (not streaks, those live in Postgres)
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

def get_user(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT current_streak, longest_streak, last_date FROM users WHERE user_id=%s", (str(user_id),))
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
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO revelations (user_id, date, text) VALUES (%s, %s, %s)", (str(user_id), date, text))
    conn.commit()
    conn.close()

def get_revelations(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT date, text FROM revelations WHERE user_id=%s ORDER BY id ASC", (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return rows

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
    keyboard = [[InlineKeyboardButton("↩️ Back", callback_data="back_to_menu")]]
    return InlineKeyboardMarkup(keyboard)

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
    total = 5
    fire = "🔥" * min(streak, total)
    white = "⚪" * (total - min(streak, total))
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
    user_qt_done[user_id] = user_qt_done.get(user_id, False)
    context.bot_data[user_id] = user_name

    if update.message:
        await update.message.reply_text(
            f"Hello {user_name}! 🙌\nI’m **ZN3 PrayerBot**.\nLet’s grow together in our commitment and faith 🙏👋",
            parse_mode="Markdown"
        )
        await update.message.reply_text(
            f"Hello {user_name}! 👋\nHave you done your QT today?",
            reply_markup=yes_no_keyboard()
        )
    else:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Hello {user_name}! 🙌\nI’m **ZN3 PrayerBot**.\nLet’s grow together in our commitment and faith 🙏👋",
            parse_mode="Markdown"
        )
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Hello {user_name}! 👋\nHave you done your QT today?",
            reply_markup=yes_no_keyboard()
        )

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_revelations(user_id)

    if not rows:
        text = "📭 You have no saved revelations yet."
    else:
        text = "\n\n".join([f"📝 {date}: {msg}" for date, msg in rows])
        text = f"📖 Your past revelations:\n\n{text}"

    keyboard = [[InlineKeyboardButton("↩️ Back", callback_data="back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup)

async def allstreaks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_all_streaks()
    if not rows:
        await update.message.reply_text("📭 No streaks recorded yet.")
        return
    leaderboard = "\n".join([
        f"{i+1}. {name or 'Unknown'} — 🔥 {streak} (Longest: {longest})"
        for i, (name, streak, longest) in enumerate(rows)
    ])
    await update.message.reply_text(f"📊 Streak Leaderboard:\n\n{leaderboard}")

# =============================
# CALLBACK HANDLER
# =============================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_name = query.from_user.first_name or "Unknown"
    data = query.data

    if data == "yes":
        user_qt_done[user_id] = True
        _cancel_user_job(user_id)

        today = datetime.now(sg_timezone).strftime("%d/%m/%y")
        user = get_user(user_id)
        if user:
            current, longest, last_date = user
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
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back")]])
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

    if data == "back":
        await query.edit_message_text("Have you done your QT today?", reply_markup=yes_no_keyboard())
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

    if data == "back_to_menu":
        user = get_user(user_id)
        if user:
            current, longest, last_date = user
            msg = streak_message(current, longest)
            await query.edit_message_text(
                f"🙏 Welcome back!\n{msg}",
                reply_markup=main_menu_keyboard()
            )
        else:
            await query.edit_message_text(
                "🙏 Welcome back!",
                reply_markup=main_menu_keyboard()
            )
        return

    if data == "cancelrem":
        if _cancel_user_job(user_id):
            await query.edit_message_text("🔕 Reminder cancelled.", reply_markup=main_menu_keyboard())
        else:
            await query.edit_message_text("ℹ️ No reminder was set.", reply_markup=main_menu_keyboard())
        return

# =============================
# MESSAGE HANDLER
# =============================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Unknown"
    text = (update.message.text or "").strip()

    if user_qt_done.get(user_id, False):
        today = datetime.now(sg_timezone).strftime("%d/%m/%y")
        add_revelation(user_id, today, text)
        user = get_user(user_id)
        if user:
            current, longest, last_date = user
            msg = streak_message(current, longest)
            await update.message.reply_text(f"🙏 Revelation saved!\n{msg}", reply_markup=main_menu_keyboard())
        else:
            await update.message.reply_text("🙏 Revelation saved!", reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text("Please choose an option below:", reply_markup=yes_no_keyboard())

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

async def daily_qt_check(context: ContextTypes.DEFAULT_TYPE):
    for user_id in list(user_qt_done.keys()):
        user_qt_done[user_id] = False
        user_name = context.bot_data.get(user_id, "friend")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🌙 Hello {user_name}, have you done your QT today?",
            reply_markup=yes_no_keyboard()
        )
        _schedule_forced_reminder(user_id, context, delay_hours=1)

# =============================
# MAIN
# =============================

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("allstreaks", allstreaks_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    singapore_tz = pytz.timezone("Asia/Singapore")
    app.job_queue.run_daily(
        daily_qt_check,
        time=time(hour=21, minute=0, tzinfo=singapore_tz),
        name="daily_qt_check"
    )

    print("🤖 ZN3 PrayerBot running on Railway (with Postgres)…")
    app.run_polling()

if __name__ == "__main__":
    main()
