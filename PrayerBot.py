import os
import random
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
BOT_TOKEN = os.getenv("BOT_TOKEN", "8332102513:AAFLXK6fdJkJyrbdTwi_lFOFk3qDaK0nL9A")

# Track per-user state
user_qt_done: dict[int, bool] = {}            # user_id -> done today?
user_revelations: dict[int, list[dict]] = {}  # user_id -> list of {date, text}
user_jobs: dict[int, object] = {}             # user_id -> scheduled Job (forced reminder)

REMINDER_MESSAGES = [
    "⏰ Gentle reminder: Have you done your QT?",
    "📖 Daily bread check-in — QT time?",
    "✨ QT reminder — take a quiet moment today.",
    "🙏 Hello! Just checking: QT done yet?",
    "🕊️ A nudge for QT — you got this!"
]

# Singapore timezone
sg_timezone = pytz.timezone("Asia/Singapore")

# =============================
# HELPERS (UI)
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
    ]]
    return InlineKeyboardMarkup(keyboard)

def _schedule_forced_reminder(user_id: int, context: ContextTypes.DEFAULT_TYPE, delay_hours: int = 1):
    """Schedule a single forced reminder after `delay_hours`. Replaces any existing one."""
    _cancel_user_job(user_id)
    job = context.job_queue.run_once(
        reminder_job_once,
        when=timedelta(hours=delay_hours),
        chat_id=user_id,
        name=f"forced_reminder_{user_id}",
        data={"user_id": user_id}
    )
    user_jobs[user_id] = job

# =============================
# COMMANDS
# =============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "there"
    # Keep the user's current state if exists, else default to False (not done yet today)
    user_qt_done[user_id] = user_qt_done.get(user_id, False)
    context.bot_data[user_id] = user_name   # save for daily reminders

    # Intro bubble
    if update.message:
        await update.message.reply_text(
            f"Hello {user_name}! 🙌\nI’m **ZN3 PrayerBot**.\nLet’s grow together in our commitment and faith 🙏👋",
            parse_mode="Markdown"
        )
        # Question bubble
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
    await _send_history(update.effective_user.id, context)

async def _send_history(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    revelations = user_revelations.get(user_id, [])
    if not revelations:
        await context.bot.send_message(chat_id=user_id, text="📭 You have no saved revelations yet.")
        return

    text = "\n\n".join([f"📝 {r['date']}: {r['text']}" for r in revelations])

    keyboard = [[InlineKeyboardButton("↩️ Back", callback_data="back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=user_id,
        text=f"📖 Your past revelations:\n\n{text}",
        reply_markup=reply_markup
    )

# =============================
# CALLBACKS (BUTTONS)
# =============================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "yes":
        # Mark done, cancel any pending forced reminder
        user_qt_done[user_id] = True
        _cancel_user_job(user_id)
        await query.edit_message_text(
            "Awesome 🙌 Please type your revelation for today:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="back")]])
        )
        return

    if data == "no":
        # Not done: encourage and FORCE a reminder in 1 hour
        user_qt_done[user_id] = False
        _schedule_forced_reminder(user_id, context, delay_hours=1)
        await query.edit_message_text(
            "⏳ Not yet? No worries — I’ll remind you in 1 hour.",
            reply_markup=main_menu_keyboard()
        )
        return

    if data == "back":
        await query.edit_message_text(
            "Have you done your QT today?",
            reply_markup=yes_no_keyboard()
        )
        return

    if data == "history":
        await query.edit_message_reply_markup(None)
        await _send_history(user_id, context)
        return

    if data == "cancelrem":
        if _cancel_user_job(user_id):
            await query.edit_message_text("🔕 Reminder cancelled.", reply_markup=main_menu_keyboard())
        else:
            await query.edit_message_text("ℹ️ No reminder was set.", reply_markup=main_menu_keyboard())
        return

# =============================
# MESSAGE HANDLER (TEXT)
# =============================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    if user_qt_done.get(user_id, False):
        today = datetime.now(sg_timezone).strftime("%d/%m/%y")
        user_revelations.setdefault(user_id, []).append({"date": today, "text": text})
        _cancel_user_job(user_id)
        await update.message.reply_text(
            "🙏 Revelation saved privately!\nUse /history to view past notes.",
            reply_markup=main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "Please choose an option below:",
            reply_markup=yes_no_keyboard()
        )

# =============================
# JOBS
# =============================

async def reminder_job_once(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.chat_id

    # If already marked done, skip
    if user_qt_done.get(user_id, False):
        user_jobs.pop(user_id, None)
        return

    message = random.choice(REMINDER_MESSAGES)
    try:
        await context.bot.send_message(chat_id=user_id, text=message, reply_markup=main_menu_keyboard())
    except Exception as e:
        print(f"Could not send reminder to {user_id}: {e}")

    # One-time reminder fired; clear handle
    user_jobs.pop(user_id, None)

async def daily_qt_check(context: ContextTypes.DEFAULT_TYPE):
    """At 9 PM SGT, reset 'done' for all users, ask the QT question, and FORCE schedule a reminder in 1 hour."""
    for user_id in list(user_qt_done.keys()):
        # Reset today's status at 9 PM
        user_qt_done[user_id] = False

        user_name = context.bot_data.get(user_id, "friend")
        try:
            # Ask the nightly question
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🌙 Hello {user_name}, have you done your QT today?",
                reply_markup=yes_no_keyboard()
            )
            # Force a 1-hour reminder if they still haven't marked 'Yes' by then
            _schedule_forced_reminder(user_id, context, delay_hours=1)
        except Exception as e:
            print(f"Could not send daily QT check to {user_id}: {e}")

def _cancel_user_job(user_id: int) -> bool:
    job = user_jobs.pop(user_id, None)
    if job:
        job.schedule_removal()
        return True
    return False

# =============================
# MAIN
# =============================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Daily 9 PM Singapore Time reminder + forced follow-up in 1 hour
    singapore_tz = pytz.timezone("Asia/Singapore")
    app.job_queue.run_daily(
        daily_qt_check,
        time=time(hour=21, minute=0, tzinfo=singapore_tz),
        name="daily_qt_check"
    )

    print("🤖 ZN3 PrayerBot is running (9PM SGT check + forced 1-hour reminder)…")
    app.run_polling()

if __name__ == "__main__":
    main()
