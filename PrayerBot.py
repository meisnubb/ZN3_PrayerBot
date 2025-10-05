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
BOT_TOKEN = os.getenv("BOT_TOKEN")        # from Railway Variables
DATABASE_URL = os.getenv("DATABASE_URL")  # from Railway Variables

REMINDER_MESSAGES = [
    "⏰ Gentle reminder: Have you done your QT?",
    "📖 Daily bread check-in — QT time?",
    "✨ QT reminder — take a quiet moment today.",
    "🙏 Hello! Just checking: QT done yet?",
    "🕊️ A nudge for QT — you got this!"
]

sg_timezone = pytz.timezone("Asia/Singapore")

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
    # ✅ Fix leaderboard ordering
    c.execute("SELECT name, current_streak, longest_streak FROM users ORDER BY current_streak DESC, longest_streak DESC")
    rows = c.fetchall()
    conn.close()
    return rows

# =============================
# HELPERS
# =============================

def yes_no_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data="yes"),
        InlineKeyboardButton("❌ No", callback_data="no")
    ]])

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Mark QT Done", callback_data="yes"),
            InlineKeyboardButton("❌ Not Yet", callback_data="no"),
        ],
        [
            InlineKeyboardButton("📖 View History", callback_data="history"),
            InlineKeyboardButton("🔕 Cancel Reminder", callback_data="cancelrem"),
        ],
        [
            InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"),
        ]
    ])

def leaderboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_to_menu")]])

def streak_visual(streak: int) -> str:
    total = 7  # 7-day cycle
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
    user_qt_done[user_id] = user_qt_done.get(user_id, False)
    context.bot_data[user_id] = user_name

    await update.message.reply_text(
        f"Hello {user_name}! 🙌\nI’m **ZN3 PrayerBot**.\nLet’s grow together in our commitment and faith 🙏👋",
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        f"Hello {user_name}! 👋\nHave you done your QT today?",
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

    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_to_menu")]])

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

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
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_to_menu")]])
        )
        return

    if data == "history":
        await history_cmd(update, context)
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
            current, longest, _ = user
            msg = streak_message(current, longest)
            await query.edit_message_text(f"🙏 Welcome back!\n{msg}", reply_markup=main_menu_keyboard())
        else:
            await query.edit_message_text("🙏 Welcome back!", reply_markup=main_menu_keyboard())
        return

# =============================
# JOBS
# =============================

async def streak_break_check(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(sg_timezone).strftime("%d/%m/%y")
    yesterday = (datetime.now(sg_timezone) - timedelta(days=1)).strftime("%d/%m/%y")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, name, last_date FROM users")
    rows = c.fetchall()

    for user_id, name, last_date in rows:
        if last_date != yesterday:  # missed yesterday
            c.execute("UPDATE users SET current_streak = 0 WHERE user_id = %s", (user_id,))
            await context.bot.send_message(
                chat_id=int(user_id),
                text="💔 Oh no, your streak was broken!\nDon’t be discouraged — tomorrow is a fresh start. 🌅 Keep going!"
            )

    conn.commit()
    conn.close()

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

    # 21:00 – reminder
    app.job_queue.run_daily(
        lambda context: None,
        time=time(hour=21, minute=0, tzinfo=singapore_tz),
        name="daily_reminder"
    )

    # 00:00 – streak break check
    app.job_queue.run_daily(
        streak_break_check,
        time=time(hour=0, minute=0, tzinfo=singapore_tz),
        name="streak_break_check"
    )

    print("🤖 ZN3 PrayerBot running on Railway (with Postgres + streak break check)…")
    app.run_polling()

if __name__ == "__main__":
    main()
