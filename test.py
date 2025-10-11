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
SGT = pytz.timezone("Asia/Singapore")

REMINDER_MESSAGES = [
    "⏰ Gentle reminder: Have you done your QT?",
    "📖 Daily bread check-in — QT time?",
    "✨ QT reminder — take a quiet moment today.",
    "🙏 Hello! Just checking: QT done yet?",
    "🕊️ A nudge for QT — you got this!",
    "🔥 Keep the streak alive! QT time 🙏",
    "📿 Take a pause and connect with Him now ❤️"
]

# Runtime (not persisted)
user_qt_done: dict[int, bool] = {}
awaiting_reminder_input: set[int] = set()
daily_jobs: dict[int, object] = {}
followup_jobs: dict[int, object] = {}
user_cancelled_today: dict[int, bool] = {}

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
    try:
        c.execute("ALTER TABLE users ADD COLUMN reminder_hour INTEGER")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN reminder_minute INTEGER")
    except Exception:
        pass
    conn.commit()
    conn.close()

def ensure_user_record(user_id: int, name: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (user_id, name, current_streak, longest_streak, last_date, reminder_hour, reminder_minute)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name
    """, (str(user_id), name, 0, 0, None, None, None))
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
    out = []
    for date, enc in rows:
        try:
            out.append((date, fernet.decrypt(enc.encode()).decode()))
        except Exception:
            out.append((date, "⚠️ Unable to decrypt (corrupted entry)"))
    return out

def get_all_for_schedule():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, COALESCE(name,'friend'), reminder_hour, reminder_minute FROM users")
    rows = c.fetchall()
    conn.close()
    return [(int(uid), name, rh, rm) for uid, name, rh, rm in rows]

def get_all_streaks():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
      SELECT COALESCE(name,'Unknown'), current_streak, longest_streak
      FROM users
      ORDER BY current_streak DESC, longest_streak DESC, COALESCE(name,'') ASC
    """)
    rows = c.fetchall()
    conn.close()
    return rows

# =============================
# UI HELPERS
# =============================

def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Mark QT Done", callback_data="yes"),
            InlineKeyboardButton("🔕 Cancel Today’s Reminder", callback_data="cancel_today"),
        ],
        [
            InlineKeyboardButton("📖 View History", callback_data="history"),
            InlineKeyboardButton("⏰ Set Reminder", callback_data="setrem"),
        ],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")],
    ])

def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_to_menu")]])

def streak_visual(streak: int) -> str:
    total = 7
    r = streak % total
    if r == 0 and streak > 0:
        r = 7
    return "🔥" * r + "⚪" * (total - r)

def streak_message_block(current: int, longest: int, reminder_h: int | None, reminder_m: int | None) -> str:
    lines = [f"🙏 Welcome back!", f"{streak_visual(current)}", f"Current streak: {current} days", f"Longest streak: {longest} days"]
    if reminder_h is not None and reminder_m is not None:
        lines.insert(1, f"🔔 Daily reminder set for {reminder_h:02d}:{reminder_m:02d}")
    return "\n".join(lines)

def friendly_error_format() -> str:
    return "❌ Invalid format. Use HH:MM (e.g., 08:00 or 21:15)."

# =============================
# REMINDER SCHEDULING
# =============================

def cancel_user_jobs(user_id: int):
    job = daily_jobs.pop(user_id, None)
    if job:
        job.schedule_removal()
    job2 = followup_jobs.pop(user_id, None)
    if job2:
        job2.schedule_removal()

def compute_next_dt(hour: int, minute: int) -> datetime:
    now = datetime.now(SGT)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate

def schedule_user_reminder(app: Application, user_id: int, hour: int, minute: int):
    cancel_user_jobs(user_id)
    next_dt = compute_next_dt(hour, minute)
    delta = next_dt - datetime.now(SGT)
    job = app.job_queue.run_once(
        nudge_job_once,
        when=delta,
        chat_id=user_id,
        name=f"nudge_{user_id}",
        data={"user_id": user_id, "hour": hour, "minute": minute},
    )
    daily_jobs[user_id] = job

async def nudge_job_once(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.chat_id
    if user_cancelled_today.get(user_id, False):
        return
    msg = random.choice(REMINDER_MESSAGES)
    try:
        await context.bot.send_message(chat_id=user_id, text=msg, reply_markup=menu_keyboard())
    except Exception:
        pass
    if not user_qt_done.get(user_id, False):
        fj = context.job_queue.run_once(reminder_followup, when=timedelta(hours=1), chat_id=user_id, name=f"followup_{user_id}")
        followup_jobs[user_id] = fj
    data = getattr(context.job, "data", {}) or {}
    hour, minute = data.get("hour"), data.get("minute")
    if hour is not None and minute is not None:
        schedule_user_reminder(context.application, user_id, hour, minute)

async def reminder_followup(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.chat_id
    if not user_qt_done.get(user_id, False) and not user_cancelled_today.get(user_id, False):
        try:
            await context.bot.send_message(chat_id=user_id, text="👋 Hello! Just checking: QT done yet?", reply_markup=menu_keyboard())
        except Exception:
            pass
    followup_jobs.pop(user_id, None)

# =============================
# NIGHTLY RESET
# =============================

async def nightly_reset_job(context: ContextTypes.DEFAULT_TYPE):
    user_cancelled_today.clear()
    today = datetime.now(SGT).strftime("%d/%m/%y")
    yesterday = (datetime.now(SGT) - timedelta(days=1)).strftime("%d/%m/%y")
    for uid, _, rh, rm in get_all_for_schedule():
        user_qt_done[uid] = False
        row = get_user(uid)
        if not row:
            continue
        current, longest, last_date, name, _, _ = row
        if last_date != yesterday and current > 0:
            update_user(uid, name or "friend", 0, longest, last_date)
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=("New day, new start 🌅 Your streak reset overnight, but it’s never too late to build it back up. You got this! 💯🔥")
                )
            except Exception:
                pass

# =============================
# CALLBACKS & COMMANDS
# =============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "friend"
    ensure_user_record(uid, name)
    user_qt_done[uid] = False
    row = get_user(uid)
    if row:
        current, longest, _, _, rh, rm = row
    else:
        current, longest, rh, rm = 0, 0, None, None
    text = streak_message_block(current, longest, rh, rm)
    await update.message.reply_text(text, reply_markup=menu_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    name = q.from_user.first_name or "Unknown"
    ensure_user_record(uid, name)
    data = q.data

    if data == "cancel_today":
        cancel_user_jobs(uid)
        user_cancelled_today[uid] = True
        await q.edit_message_text("🔕 You’ve cancelled reminders for today. See you tomorrow!", reply_markup=back_keyboard())
        return

    if data == "yes":
        today = datetime.now(SGT).strftime("%d/%m/%y")
        row = get_user(uid)
        if row:
            current, longest, last_date, _, _, _ = row
            if last_date == today:
                pass
            elif last_date == (datetime.now(SGT) - timedelta(days=1)).strftime("%d/%m/%y"):
                current += 1
            else:
                current = 1
            longest = max(longest, current)
        else:
            current, longest = 1, 1
        update_user(uid, name, current, longest, today)
        user_qt_done[uid] = True
        await q.edit_message_text("Awesome 🙌 Please type your revelation for today:", reply_markup=back_keyboard())
        return

    if data == "history":
        rows = get_revelations(uid)
        text = "📭 You have no saved revelations yet." if not rows else "📖 Your past revelations:\n\n" + "\n\n".join([f"📝 {d}: {t}" for d, t in rows])
        await q.edit_message_text(text, reply_markup=back_keyboard())
        return

    if data == "setrem":
        awaiting_reminder_input.add(uid)
        await q.edit_message_text("🕰️ Please send your preferred reminder time (HH:MM, 24hr). Must be before 23:30.", reply_markup=back_keyboard())
        return

    if data == "leaderboard":
        rows = get_all_streaks()
        text = "📭 No streaks recorded yet." if not rows else "📊 Streak Leaderboard:\n\n" + "\n".join([
            f"{i+1}. {name} — 🔥 {streak} (Longest: {longest})"
            for i, (name, streak, longest) in enumerate(rows)
        ])
        await q.edit_message_text(text, reply_markup=back_keyboard())
        return

    if data == "back_to_menu":
        row = get_user(uid)
        if row:
            current, longest, _, _, rh, rm = row
        else:
            current, longest, rh, rm = 0, 0, None, None
        text = streak_message_block(current, longest, rh, rm)
        await q.edit_message_text(text, reply_markup=menu_keyboard())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "Unknown"
    ensure_user_record(uid, name)
    text = (update.message.text or "").strip()

    # Reminder time input
    if uid in awaiting_reminder_input:
        parts = text.split(":")
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            await update.message.reply_text(friendly_error_format())
            return
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59) or (h == 23 and m >= 30):
            await update.message.reply_text("⚠️ Please choose a time before 23:30.")
            return
        update_user_reminder(uid, h, m)
        schedule_user_reminder(context.application, uid, h, m)
        awaiting_reminder_input.discard(uid)
        await update.message.reply_text(f"✅ Reminder updated!\n🔔 Daily reminder set for {h:02d}:{m:02d}.", reply_markup=back_keyboard())
        return

    # Revelation
    if user_qt_done.get(uid, False):
        today = datetime.now(SGT).strftime("%d/%m/%y")
        add_revelation(uid, today, text)
        row = get_user(uid)
        current, longest, _, _, rh, rm = row if row else (0, 0, None, None, None, None)
        msg = streak_message_block(current, longest, rh, rm)
        await update.message.reply_text(f"🙏 Revelation saved!\n{msg}", reply_markup=menu_keyboard())
        return

    await update.message.reply_text("Please choose an option below:", reply_markup=menu_keyboard())

# =============================
# MAIN
# =============================

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.job_queue.run_daily(nightly_reset_job, time=time(hour=0, minute=0, tzinfo=SGT))
    for uid, _, rh, rm in get_all_for_schedule():
        if rh is not None and rm is not None:
            schedule_user_reminder(app, uid, rh, rm)
    print("🤖 ZN3 PrayerBot running with Cancel Today’s Reminder feature…")
    app.run_polling()

if __name__ == "__main__":
    main()
