import os
import random
import psycopg2
from cryptography.fernet import Fernet
from datetime import timedelta, time, datetime
import pytz
from calendar import month_name
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
    "🙏 Hello! Just checking: have you done your QT yet?",
    "🕊️ A nudge for QT — you got this!",
    "🔥 Keep the streak alive! QT time 🙏",
    "📿 Take a pause and connect with Him now ❤️"
]

# Runtime memory
user_qt_done: dict[int, bool] = {}
awaiting_reminder_input: set[int] = set()
awaiting_revelation: set[int] = set()
daily_jobs: dict[int, object] = {}
followup_jobs: dict[int, object] = {}

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
    # ✅ fix: ensure column exists for old users table
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS cancelled_date TEXT;")
    conn.commit()
    conn.close()

def ensure_user_record(user_id: int, name: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (user_id, name, current_streak, longest_streak, last_date, reminder_hour, reminder_minute, cancelled_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO NOTHING
    """, (str(user_id), name, 0, 0, None, 8, 0, None))
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT current_streak, longest_streak, last_date, name, reminder_hour, reminder_minute, cancelled_date
        FROM users WHERE user_id=%s
    """, (str(user_id),))
    row = c.fetchone()
    conn.close()
    return row

def update_user(user_id: int, name: str, streak: int, longest: int, last_date: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        UPDATE users SET
          name=%s,
          current_streak=%s,
          longest_streak=%s,
          last_date=%s
        WHERE user_id=%s
    """, (name, streak, longest, last_date, str(user_id)))
    conn.commit()
    conn.close()

def update_user_reminder(user_id: int, hour: int, minute: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET reminder_hour=%s, reminder_minute=%s WHERE user_id=%s",
              (hour, minute, str(user_id)))
    conn.commit()
    conn.close()

def set_user_cancelled_today(user_id: int, date_str: str | None):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET cancelled_date=%s WHERE user_id=%s", (date_str, str(user_id)))
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

# 🆕 Monthly Revelation Retrieval + Pagination
def get_revelations_by_month(user_id: int, year: int, month: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT date, text FROM revelations WHERE user_id=%s ORDER BY id ASC", (str(user_id),))
    rows = c.fetchall()
    conn.close()

    result = []
    for date, enc in rows:
        try:
            dec = fernet.decrypt(enc.encode()).decode()
        except Exception:
            dec = "⚠️ Unable to decrypt (corrupted entry)"
        try:
            d = datetime.strptime(date, "%d/%m/%y")
            if d.year == year and d.month == month:
                result.append((date, dec))
        except Exception:
            continue
    return result

def month_history_keyboard(user_id: int, year: int, month: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT DISTINCT date FROM revelations WHERE user_id=%s", (str(user_id),))
    all_dates = c.fetchall()
    conn.close()

    months = []
    for (date_str,) in all_dates:
        try:
            d = datetime.strptime(date_str, "%d/%m/%y")
            ym = (d.year, d.month)
            if ym not in months:
                months.append(ym)
        except Exception:
            continue
    months.sort()

    has_prev = any((y, m) < (year, month) for (y, m) in months)
    has_next = any((y, m) > (year, month) for (y, m) in months)

    buttons = []
    if has_prev:
        buttons.append(InlineKeyboardButton("◀️", callback_data=f"history_prev_{year}_{month}"))
    if has_next:
        buttons.append(InlineKeyboardButton("▶️", callback_data=f"history_next_{year}_{month}"))

    return InlineKeyboardMarkup([buttons] + [[InlineKeyboardButton("↩️ Back", callback_data="back_to_menu")]]) if buttons else InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_to_menu")]])

def get_all_for_schedule():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, COALESCE(name,'friend'), reminder_hour, reminder_minute FROM users")
    rows = c.fetchall()
    conn.close()
    return [(int(uid), name, rh, rm) for uid, name, rh, rm in rows if rh is not None and rm is not None]

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

def menu_keyboard():
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

def reminder_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes", callback_data="reminder_yes"),
            InlineKeyboardButton("❌ No", callback_data="reminder_no")
        ]
    ])

def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back_to_menu")]])

def streak_visual(streak: int) -> str:
    total = 7
    r = streak % total or 7 if streak > 0 else 0
    return "🔥" * r + "⚪" * (total - r)

def streak_message_block(current: int, longest: int, rh: int | None, rm: int | None) -> str:
    lines = [
        "🙏 Welcome back!",
        f"{streak_visual(current)}",
        f"Current streak: {current} days",
        f"Longest streak: {longest} days"
    ]
    if rh is not None and rm is not None:
        lines.insert(1, f"🔔 Daily reminder set for {rh:02d}:{rm:02d}")
    if current in [5, 7, 30, 100, 365]:
        msg = {5:"🌟 Congrats on 5 days!",7:"💪 One full week!",30:"🎉 A whole month!",100:"👑 Incredible! 100 days!",365:"🏆 WOW! A full year!"}[current]
        lines.append(msg)
    return "\n".join(lines)

# =============================
# REMINDERS
# =============================

def safe_cancel(job):
    try:
        if job:
            job.schedule_removal()
    except Exception:
        pass

def cancel_user_jobs(uid):
    safe_cancel(daily_jobs.pop(uid, None))
    safe_cancel(followup_jobs.pop(uid, None))

def compute_next_dt(h: int, m: int) -> datetime:
    now = datetime.now(SGT)
    candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate

def schedule_user_reminder(app, uid: int, h: int, m: int):
    cancel_user_jobs(uid)
    delta = compute_next_dt(h, m) - datetime.now(SGT)
    job = app.job_queue.run_once(nudge_job_once, when=delta, chat_id=uid,
                                 name=f"nudge_{uid}", data={"hour": h, "minute": m})
    daily_jobs[uid] = job

async def nudge_job_once(context: ContextTypes.DEFAULT_TYPE):
    uid = context.job.chat_id
    row = get_user(uid)
    if not row:
        return
    cancelled_date = row[6]
    today = datetime.now(SGT).strftime("%d/%m/%y")

    if cancelled_date == today:
        data = getattr(context.job, "data", {}) or {}
        if data.get("hour") is not None:
            schedule_user_reminder(context.application, uid, data["hour"], data["minute"])
        return

    msg = random.choice(REMINDER_MESSAGES)
    try:
        await context.bot.send_message(chat_id=uid, text=msg, reply_markup=reminder_keyboard())
    except Exception:
        pass

    data = getattr(context.job, "data", {}) or {}
    if data.get("hour") is not None:
        schedule_user_reminder(context.application, uid, data["hour"], data["minute"])

async def reminder_followup(context: ContextTypes.DEFAULT_TYPE):
    uid = context.job.chat_id
    if not user_qt_done.get(uid, False):
        try:
            await context.bot.send_message(chat_id=uid, text="👋 Hello! Have you done your QT 🤨?", reply_markup=menu_keyboard())
        except Exception:
            pass
    followup_jobs.pop(uid, None)

# =============================
# NIGHTLY RESET
# =============================

async def nightly_reset_job(context: ContextTypes.DEFAULT_TYPE):
    awaiting_revelation.clear()
    today = datetime.now(SGT).strftime("%d/%m/%y")
    yesterday = (datetime.now(SGT) - timedelta(days=1)).strftime("%d/%m/%y")
    for uid, _, rh, rm in get_all_for_schedule():
        user_qt_done[uid] = False
        row = get_user(uid)
        if not row:
            continue
        current, longest, last_date, name, _, _, cancelled_date = row
        if last_date != yesterday and current > 0:
            update_user(uid, name or "friend", 0, longest, last_date)
            try:
                await context.bot.send_message(chat_id=uid, text="🌅 New day, new start! Your streak reset overnight. You got this! 💪")
            except Exception:
                pass
        if cancelled_date == today:
            set_user_cancelled_today(uid, None)

# =============================
# COMMANDS & BUTTONS
# =============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "friend"
    ensure_user_record(uid, name)
    row = get_user(uid)
    current, longest, _, _, rh, rm, _ = row if row else (0, 0, None, None, 8, 0, None)
    schedule_user_reminder(context.application, uid, rh or 8, rm or 0)
    await update.message.reply_text(
        f"Hello {name}! 🙌\nI’m ZN3 PrayerBot.\nLet’s grow together in faith 🙏",
    )
    await update.message.reply_text(streak_message_block(current, longest, rh, rm), reply_markup=menu_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid, data = q.from_user.id, q.data
    name = q.from_user.first_name or "friend"
    ensure_user_record(uid, name)

    if data in ("reminder_yes", "yes"):
        awaiting_revelation.add(uid)
        await q.edit_message_text("Awesome 🙌 Please type your revelation for today:", reply_markup=back_keyboard())
        return

    if data == "reminder_no":
        safe_cancel(followup_jobs.get(uid))
        job = context.job_queue.run_once(reminder_followup, when=timedelta(hours=1), chat_id=uid)
        followup_jobs[uid] = job
        await q.edit_message_text("Got it! I’ll remind you again in an hour ⏰", reply_markup=back_keyboard())
        return

    if data == "cancel_today":
        today = datetime.now(SGT).strftime("%d/%m/%y")
        set_user_cancelled_today(uid, today)
        await q.edit_message_text("🔕 You’ve cancelled reminders for today. See you tomorrow!", reply_markup=back_keyboard())
        return

    # 🆕 Month-based history view
    if data == "history":
        now = datetime.now(SGT)
        year, month = now.year, now.month
        rows = get_revelations_by_month(uid, year, month)
        title = f"📖 {month_name[month]} {year}"
        text = f"{title}\n\n" + ("\n\n".join([f"📝 {d}: {t}" for d, t in rows]) if rows else "📭 No entries this month.")
        await q.edit_message_text(text, reply_markup=month_history_keyboard(uid, year, month))
        return

    if data.startswith("history_prev_") or data.startswith("history_next_"):
        parts = data.split("_")
        direction, year, month = parts[1], int(parts[2]), int(parts[3])
        if direction == "prev":
            month -= 1
            if month == 0:
                month = 12
                year -= 1
        else:
            month += 1
            if month == 13:
                month = 1
                year += 1
        rows = get_revelations_by_month(uid, year, month)
        title = f"📖 {month_name[month]} {year}"
        text = f"{title}\n\n" + ("\n\n".join([f"📝 {d}: {t}" for d, t in rows]) if rows else "📭 No entries this month.")
        await q.edit_message_text(text, reply_markup=month_history_keyboard(uid, year, month))
        return

    if data == "setrem":
        awaiting_reminder_input.add(uid)
        await q.edit_message_text("🕰️ Send reminder time (HH:MM, 24hr, before 23:30).", reply_markup=back_keyboard())
        return

    if data == "leaderboard":
        rows = get_all_streaks()
        text = "📊 Leaderboard:\n\n" + "\n".join([f"{i+1}. {n} — 🔥 {s} (Longest: {l})" for i, (n, s, l) in enumerate(rows)]) if rows else "📭 No data yet."
        await q.edit_message_text(text, reply_markup=back_keyboard())
        return

    if data == "back_to_menu":
        awaiting_revelation.discard(uid)
        awaiting_reminder_input.discard(uid)
        row = get_user(uid)
        current, longest, _, _, rh, rm, _ = row if row else (0, 0, None, None, 8, 0, None)
        await q.edit_message_text(streak_message_block(current, longest, rh, rm), reply_markup=menu_keyboard())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "Unknown"
    ensure_user_record(uid, name)
    text = (update.message.text or "").strip()

    if uid in awaiting_reminder_input:
        parts = text.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            await update.message.reply_text("❌ Invalid format. Use HH:MM (e.g. 08:00).")
            return
        h, m = map(int, parts)
        if not (0 <= h <= 23 and 0 <= m <= 59) or (h == 23 and m >= 30):
            await update.message.reply_text("⚠️ Please choose a time before 23:30.")
            return
        update_user_reminder(uid, h, m)
        schedule_user_reminder(context.application, uid, h, m)
        awaiting_reminder_input.discard(uid)
        await update.message.reply_text(f"✅ Reminder set for {h:02d}:{m:02d}.", reply_markup=back_keyboard())
        return

    if uid in awaiting_revelation:
        today = datetime.now(SGT).strftime("%d/%m/%y")
        row = get_user(uid)
        current, longest, last_date, _, _, _, _ = row if row else (0, 0, None, None, None, None, None)
        if last_date == today:
            pass
        elif last_date == (datetime.now(SGT) - timedelta(days=1)).strftime("%d/%m/%y"):
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        update_user(uid, name, current, longest, today)
        add_revelation(uid, today, text)
        awaiting_revelation.discard(uid)
        user_qt_done[uid] = True

        safe_cancel(followup_jobs.get(uid))
        followup_jobs.pop(uid, None)

        row = get_user(uid)
        msg = streak_message_block(row[0], row[1], row[4], row[5])
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
    app.job_queue.run_daily(nightly_reset_job, time=time(hour=0, minute=5, tzinfo=SGT))
    for uid, _, rh, rm in get_all_for_schedule():
        schedule_user_reminder(app, uid, rh, rm)
    print("🤖 ZN3 PrayerBot running (stable, with monthly history + fixed cancel-today + back + follow-up + persist)…")
    app.run_polling()

if __name__ == "__main__":
    main()
