import os
import asyncio
import logging
from pathlib import Path
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from database import (
    init_db, get_balances, get_credit_analysis, confirm_session, get_confirmation,
    get_confirmation_for_occurrence,
    cancel_scheduled_occurrence, get_makeup_occurrence, get_makeup_obligation,
    list_makeup_obligations, set_makeup_status,
    get_available_makeup_slots, schedule_makeup_class,
    reset_forbidden_makeup_reminders,
)
from timetable import (
    load_timetable, get_regular_sessions, find_conflicts,
    most_recent_occurrence_date, confirmation_matches_timetable_date,
)
from makeup import (
    deliver_due_makeup_reminders, reminder_scheduler, notify_cancelled_groups,
    broadcast_scheduled_makeup,
)
from lesson_tracker import (
    handle_callback as handle_lesson_callback,
    handle_document_upload as handle_syllabus_document,
    handle_text_input as handle_lesson_text,
    post_confirmation_markup,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN")
TEACHER_TELEGRAM_ID = os.getenv("TEACHER_TELEGRAM_ID")
SECTION_GROUP_IDS = {
    "A": os.getenv("TELEGRAM_GROUP_ID_A") or os.getenv("TELEGRAM_GROUP_ID"),
    "B": os.getenv("TELEGRAM_GROUP_ID_B"),
}
TIMEZONE = os.getenv("TIMEZONE", "Africa/Addis_Ababa")
STUDENT_GROUP_CHAT_ID = os.getenv("STUDENT_GROUP_CHAT_ID")
try:
    STUDENT_GROUP_CHAT_ID = int(STUDENT_GROUP_CHAT_ID) if STUDENT_GROUP_CHAT_ID else None
except ValueError as exc:
    raise RuntimeError("STUDENT_GROUP_CHAT_ID must be a numeric Telegram chat ID.") from exc

try:
    ZoneInfo(TIMEZONE)
except Exception as exc:
    raise RuntimeError(f"TIMEZONE is not a valid IANA time zone: {TIMEZONE}") from exc

if TEACHER_TELEGRAM_ID:
    try:
        TEACHER_TELEGRAM_ID = int(TEACHER_TELEGRAM_ID)
    except ValueError as exc:
        raise RuntimeError("TEACHER_TELEGRAM_ID must be a Telegram numeric user ID.") from exc
else:
    raise RuntimeError("TEACHER_TELEGRAM_ID is missing. Set the teacher's personal Telegram user ID in .env.")

for section, group_id in SECTION_GROUP_IDS.items():
    try:
        SECTION_GROUP_IDS[section] = int(group_id) if group_id else None
    except ValueError as exc:
        raise RuntimeError(f"TELEGRAM_GROUP_ID_{section} must be a numeric Telegram group ID.") from exc

if not BOT_TOKEN or BOT_TOKEN == "put_your_new_bot_token_here":
    raise RuntimeError("BOT_TOKEN is missing. Copy .env.example to .env and set it.")

SCHEDULE_PATH = BASE_DIR / "data" / "timetable.json"
SCHEDULE = load_timetable(SCHEDULE_PATH)
if not SCHEDULE:
    raise RuntimeError(f"{SCHEDULE_PATH} has no schedule entries. Add the verified timetable first.")

try:
    SYLLABUS_MAX_UPLOAD_MB = int(os.getenv("SYLLABUS_MAX_UPLOAD_MB", "10"))
except ValueError as exc:
    raise RuntimeError("SYLLABUS_MAX_UPLOAD_MB must be an integer between 1 and 19.") from exc
if not 1 <= SYLLABUS_MAX_UPLOAD_MB <= 19:
    raise RuntimeError("SYLLABUS_MAX_UPLOAD_MB must be an integer between 1 and 19.")
SYLLABUS_MAX_UPLOAD_BYTES = SYLLABUS_MAX_UPLOAD_MB * 1024 * 1024

def is_teacher(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == TEACHER_TELEGRAM_ID)

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Regular Class", callback_data="confirm:start")],
        [InlineKeyboardButton("❌ Cancel Regular Class", callback_data="makeup:cancel:list")],
        [InlineKeyboardButton("🗓️ Makeup Obligations", callback_data="makeup:list")],
        [InlineKeyboardButton("📊 Credit Check", callback_data="credit:start")],
        [InlineKeyboardButton("📖 Lesson Tracker", callback_data="lesson:classes:0")],
        [InlineKeyboardButton("📚 Syllabus Library", callback_data="lesson:library:0")],
    ])

async def edit_message_if_changed(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_teacher(update):
        await update.message.reply_text("🔒 Sorry, this bot is for the authorized teacher only.")
        return
    await update.message.reply_text(
        "🎓 ClassFlow\nChoose an action:",
        reply_markup=main_keyboard()
    )
    if update.effective_chat and update.effective_chat.type != "private":
        await update.message.reply_text(
            "💬 Open this bot in a private chat and send /start to receive private makeup reminders."
        )
    elif update.effective_chat and update.effective_chat.type == "private":
        # A Forbidden response before /start means Telegram had not opened the
        # teacher's private chat. Retry those known-undelivered reminders now.
        reset_forbidden_makeup_reminders()
        await deliver_due_makeup_reminders(context.application, TEACHER_TELEGRAM_ID, TIMEZONE)

async def show_cancellation_choices(query):
    now = datetime.now(ZoneInfo(TIMEZONE))
    today = now.date()
    occurrences = []
    for offset in range(15):
        occurrence_date = today + timedelta(days=offset)
        day = occurrence_date.strftime("%A")
        for session in get_regular_sessions(SCHEDULE):
            if session["day"] != day or get_makeup_occurrence(session["id"], occurrence_date.isoformat()):
                continue
            end_at = datetime.combine(occurrence_date, datetime.strptime(session["end"], "%H:%M").time(), tzinfo=now.tzinfo)
            if end_at <= now:
                continue
            item = dict(session)
            item["class_date"] = occurrence_date.isoformat()
            occurrences.append(item)
    if not occurrences:
        await edit_message_if_changed(
            query,
            "✅ There are no upcoming System Analysis and Design occurrences available to cancel.",
            reply_markup=main_keyboard()
        )
        return
    buttons = []
    for item in occurrences:
        buttons.append([InlineKeyboardButton(
            f"📚 {datetime.strptime(item['class_date'], '%Y-%m-%d').strftime('%a %b %d')} · Sec {item['section']} · {item['start']}–{item['end']}",
            callback_data=f"makeup:cancel:pick:{item['id']}:{item['class_date']}"
        )])
    buttons.append([InlineKeyboardButton("⬅️ Back to menu", callback_data="makeup:home")])
    await edit_message_if_changed(query, "❌ Choose a scheduled occurrence to cancel:", reply_markup=InlineKeyboardMarkup(buttons))

async def show_makeup_obligations(query):
    obligations = list_makeup_obligations()
    if not obligations:
        await query.edit_message_text("✅ There are no pending makeup obligations.", reply_markup=main_keyboard())
        return
    buttons = []
    for item in obligations:
        sections = ",".join(item["sections"].split(","))
        buttons.append([InlineKeyboardButton(
            f"🗓️ {item['original_date']} · Sec {sections} · {item['status']} · {item['course'][:28]}",
            callback_data=f"makeup:view:{item['id']}"
        )])
    buttons.append([InlineKeyboardButton("⬅️ Back to menu", callback_data="makeup:home")])
    await query.edit_message_text(
        "🗓️ Pending makeup obligations (including planned classes):",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def makeups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_teacher(update):
        await update.effective_message.reply_text("🔒 Sorry, this bot is for the authorized teacher only.")
        return
    obligations = list_makeup_obligations()
    if not obligations:
        await update.effective_message.reply_text("✅ There are no pending makeup obligations.")
        return
    await update.effective_message.reply_text(
        f"🗓️ Pending makeup obligations: {len(obligations)}. Select one to review or update:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📌 #{item['id']} · {item['original_date']} · Section {item['sections']} · {item['status']}",
                callback_data=f"makeup:view:{item['id']}"
            )]
            for item in obligations
        ])
    )

async def show_makeup_detail(query, obligation_id):
    item = get_makeup_obligation(obligation_id)
    if not item:
        await query.edit_message_text("⚠️ That makeup obligation was not found.", reply_markup=main_keyboard())
        return
    sections = ", ".join(item["sections"].split(","))
    duration_hours = item["duration_minutes"] / 60
    plan = item["planned_datetime"] or "Not recorded"
    plan_duration = (f"{item['planned_duration_minutes'] / 60:g} hour(s)"
                     if item.get("planned_duration_minutes") else "Not recorded")
    text = (
        f"📘 {item['course']}\n👥 Sections: {sections}\n🗓️ Original date: {item['original_date']}\n"
        f"🕒 Original time: {item['start']}–{item['end']}\n⏱️ Duration: {duration_hours:g} hour(s)\n"
        f"📍 Status: {item['status']}\n📝 Planned makeup: {plan}\n⏳ Makeup duration: {plan_duration}"
    )
    if item["reminder_error"]:
        text += "\n⚠️ Private reminder could not be delivered. Open a private chat with the bot using /start."
    rows = []
    if item["status"] in {"Pending", "Planned"}:
        rows.extend([
            [InlineKeyboardButton("📝 Plan a Makeup", callback_data=f"makeup:status:{obligation_id}:planned")],
            [InlineKeyboardButton("✅ Completed", callback_data=f"makeup:status:{obligation_id}:completed")],
            [InlineKeyboardButton("🚫 No Makeup Required", callback_data=f"makeup:status:{obligation_id}:none")],
        ])
    rows.append([InlineKeyboardButton("⬅️ Back to obligations", callback_data="makeup:list")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))

async def show_makeup_days(query, obligation_id):
    item = get_makeup_obligation(obligation_id)
    if not item or item["status"] not in {"Pending", "Planned"} or item["planned_datetime"]:
        await query.edit_message_text("ℹ️ This obligation is already scheduled or closed.", reply_markup=main_keyboard())
        return
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    buttons = []
    for offset in range(1, 5):
        day = today + timedelta(days=offset)
        buttons.append([InlineKeyboardButton(
            f"📅 {day.strftime('%A %b %d')}", callback_data=f"makeup:plan:day:{obligation_id}:{day.isoformat()}"
        )])
    buttons.append([InlineKeyboardButton("⬅️ Back to obligation", callback_data=f"makeup:view:{obligation_id}")])
    await query.edit_message_text(
        f"📝 Plan a makeup for {item['course']} · Section(s) {item['sections']}.\n📅 Choose a day:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def show_makeup_durations(query, obligation_id, class_date):
    item = get_makeup_obligation(obligation_id)
    if not item:
        await query.edit_message_text("⚠️ That makeup obligation was not found.", reply_markup=main_keyboard())
        return
    buttons = [[InlineKeyboardButton(
        f"⏱️ {minutes / 60:.1f} hours", callback_data=f"makeup:plan:duration:{obligation_id}:{class_date}:{minutes}"
    )] for minutes in range(60, 181, 30)]
    buttons.append([InlineKeyboardButton("📅 Change Day", callback_data=f"makeup:status:{obligation_id}:planned")])
    await query.edit_message_text(
        f"📘 {item['course']} · Section(s) {item['sections']}\n📅 {class_date}\n⏱️ Choose makeup duration:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def show_makeup_slots(query, obligation_id, class_date, duration_minutes):
    item = get_makeup_obligation(obligation_id)
    if not item:
        await query.edit_message_text("⚠️ That makeup obligation was not found.", reply_markup=main_keyboard())
        return
    day = date.fromisoformat(class_date).strftime("%A")
    slots = get_available_makeup_slots(
        SCHEDULE, item["sections"].split(","), day, class_date, duration_minutes,
        now=datetime.now(ZoneInfo(TIMEZONE)),
    )
    if not slots:
        duration_hours = duration_minutes / 60
        await query.edit_message_text(
            f"⚠️ No continuous {duration_hours:g}-hour free slot on this day for Section(s) {item['sections']}. "
            "The timetable or lunch window blocks the requested duration.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Pick Another Duration", callback_data=f"makeup:plan:durations:{obligation_id}:{class_date}")],
                [InlineKeyboardButton("📅 Change Day", callback_data=f"makeup:status:{obligation_id}:planned")],
            ])
        )
        return
    buttons = [[InlineKeyboardButton(
        f"{slot['start']}–{slot['end']}",
        callback_data=f"makeup:plan:slot:{obligation_id}:{class_date}:{duration_minutes}:{slot['start']}"
    )] for slot in slots]
    buttons.append([InlineKeyboardButton("🔄 Pick Another Duration", callback_data=f"makeup:plan:durations:{obligation_id}:{class_date}")])
    buttons.append([InlineKeyboardButton("📅 Change Day", callback_data=f"makeup:status:{obligation_id}:planned")])
    await query.edit_message_text(
        f"🟢 Available {duration_minutes / 60:g}-hour slots on {day} {class_date} · Section(s) {item['sections']}:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def show_credit_analysis(query, section):
    analysis = get_credit_analysis(section)
    bar_width = 20
    filled = round(analysis["percent_used"] / 100 * bar_width)
    progress = "🟩" * filled + "⬜" * (bar_width - filled)
    text = (
        f"📊 Credit Analysis · Section {section}\n"
        f"🎯 Baseline pool: {analysis['baseline_hours']:.1f} hours\n"
        f"📉 Total hours used: {analysis['used_hours']:.1f} hours\n"
        f"💳 Remaining hours: {analysis['remaining_hours']:.1f} hours\n"
        f"🔴 Percentage used: {analysis['percent_used']:.1f}%\n"
        f"🟢 Percentage remaining: {analysis['percent_remaining']:.1f}%\n"
        f"{progress}\n"
        f"📚 Active sessions logged: {analysis['active_sessions']}"
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Check Another Section", callback_data="credit:start")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
    ]))

async def plan_makeup_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await handle_lesson_text(update, context, TEACHER_TELEGRAM_ID, TIMEZONE):
        return
    obligation_id = context.user_data.get("pending_makeup_plan")
    if not obligation_id or not update.effective_message:
        return
    if not is_teacher(update):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await update.effective_message.reply_text("💬 Enter the makeup date and time in a private chat with this bot.")
        return
    try:
        local_plan = datetime.strptime(update.effective_message.text.strip(), "%Y-%m-%d %H:%M")
        local_plan = local_plan.replace(tzinfo=ZoneInfo(TIMEZONE)).isoformat(timespec="minutes")
        item = set_makeup_status(obligation_id, "Planned", local_plan)
    except ValueError:
        await update.effective_message.reply_text("🕒 Use YYYY-MM-DD HH:MM in local time, for example 2026-11-02 09:30.")
        return
    context.user_data.pop("pending_makeup_plan", None)
    await update.effective_message.reply_text(
        f"✅ Makeup recorded as Planned for {item['planned_datetime']} ({TIMEZONE})."
    )

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the caller's sender ID only in a private chat for setup diagnostics."""
    if not update.effective_message:
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await update.effective_message.reply_text("🔒 For privacy, use /myid in a private chat with this bot.")
        return
    user = update.effective_user
    if not user:
        await update.effective_message.reply_text("⚠️ Telegram did not provide a sender ID for this message.")
        return
    await update.effective_message.reply_text(f"🆔 Your Telegram user ID is: {user.id}")

async def group_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report the current group ID so it can be configured as notification target."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if chat.type == "private":
        await message.reply_text("👥 Run /groupid inside the group that should receive class notifications.")
        return
    await message.reply_text(f"🆔 This group's Telegram chat ID is: {chat.id}")

async def begin_confirmation(query, context):
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    available_days = sorted({s["day"] for s in get_regular_sessions(SCHEDULE)}, key=lambda d: days.index(d) if d in days else 99)
    keyboard = [[InlineKeyboardButton(f"📅 {day}", callback_data=f"confirm:day:{day}")] for day in available_days]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="confirm:cancel")])
    await query.edit_message_text("✅ Choose the day of the regular System Analysis and Design class:", reply_markup=InlineKeyboardMarkup(keyboard))

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_teacher(update):
        await query.edit_message_text("🔒 Sorry, this bot is for the authorized teacher only.")
        return

    if await handle_lesson_callback(query, context, TEACHER_TELEGRAM_ID, TIMEZONE):
        return

    data = query.data or ""
    if data == "credit:start":
        await query.edit_message_text(
            "📊 Choose a section for credit analysis:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🅰️ Section A", callback_data="credit:section:A")],
                [InlineKeyboardButton("🅱️ Section B", callback_data="credit:section:B")],
                [InlineKeyboardButton("⬅️ Back", callback_data="makeup:home")],
            ])
        )
        return
    if data in {"credit:section:A", "credit:section:B"}:
        await show_credit_analysis(query, data.rsplit(":", 1)[1])
        return
    if data == "makeup:home":
        context.user_data.pop("pending_makeup_plan", None)
        await query.edit_message_text("🎓 ClassFlow — choose an action:", reply_markup=main_keyboard())
        return
    if data == "makeup:cancel:list":
        await show_cancellation_choices(query)
        return
    if data.startswith("makeup:cancel:pick:"):
        session_id, class_date = data[len("makeup:cancel:pick:"):].rsplit(":", 1)
        item = next((s for s in get_regular_sessions(SCHEDULE) if s["id"] == session_id), None)
        if not item or item["day"] != date.fromisoformat(class_date).strftime("%A"):
            await query.edit_message_text("⚠️ That timetable occurrence was not found.", reply_markup=main_keyboard())
            return
        section = item["section"]
        await query.edit_message_text(
            f"❓ Cancel this scheduled class?\n📘 {item['course']}\n👥 Section: {section}\n"
            f"🗓️ Date: {class_date} · {item['start']}–{item['end']}\n"
            "📝 This creates a makeup obligation and does not change credit-hour balances.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Cancellation", callback_data=f"makeup:cancel:confirm:{session_id}:{class_date}")],
                [InlineKeyboardButton("⬅️ Back", callback_data="makeup:cancel:list")],
            ])
        )
        return
    if data.startswith("makeup:cancel:confirm:"):
        session_id, class_date = data[len("makeup:cancel:confirm:"):].rsplit(":", 1)
        item = next((s for s in get_regular_sessions(SCHEDULE) if s["id"] == session_id), None)
        if not item or item["day"] != date.fromisoformat(class_date).strftime("%A"):
            await query.edit_message_text("That timetable occurrence was not found.", reply_markup=main_keyboard())
            return
        original_date = date.fromisoformat(class_date)
        now = datetime.now(ZoneInfo(TIMEZONE))
        obligation, created = cancel_scheduled_occurrence(
            session_id=session_id,
            original_date=class_date,
            day=item["day"],
            course=item["course"],
            sections=[item["section"]],
            start=item["start"],
            end=item["end"],
            duration_minutes=item["duration_minutes"],
            reminder_date=(original_date + timedelta(days=1)).isoformat(),
            created_at=now.isoformat(timespec="seconds"),
        )
        if not created:
            await query.edit_message_text(
                f"ℹ️ This occurrence was already cancelled and is tracked as makeup obligation #{obligation['id']}.",
                reply_markup=main_keyboard()
            )
            return
        failures = await notify_cancelled_groups(context.bot, obligation, SECTION_GROUP_IDS, TIMEZONE)
        sections = ", ".join(obligation["sections"].split(","))
        text = (
            f"✅ Cancelled and tracked as makeup obligation #{obligation['id']}.\n"
            f"📘 Course: {obligation['course']}\n👥 Sections: {sections}\n"
            f"🗓️ Original date: {obligation['original_date']} · {obligation['start']}–{obligation['end']}\n"
            f"⏱️ Duration: {obligation['duration_minutes'] / 60:g} hour(s). Credit balances were not changed."
        )
        if failures:
            text += f"\n⚠️ Group notification failed for Section {', '.join(failures)}; the cancellation record is saved."
        else:
            text += "\n📣 Affected section group(s) notified."
        await query.edit_message_text(text, reply_markup=main_keyboard())
        return
    if data == "makeup:list":
        await show_makeup_obligations(query)
        return
    if data.startswith("makeup:view:"):
        await show_makeup_detail(query, int(data.rsplit(":", 1)[1]))
        return
    if data.startswith("makeup:plan:day:"):
        _, _, _, obligation_id, class_date = data.split(":", 4)
        await show_makeup_durations(query, int(obligation_id), class_date)
        return
    if data.startswith("makeup:plan:durations:"):
        obligation_id, class_date = data[len("makeup:plan:durations:"):].split(":", 1)
        await show_makeup_durations(query, int(obligation_id), class_date)
        return
    if data.startswith("makeup:plan:duration:"):
        _, _, _, obligation_id, class_date, duration_minutes = data.split(":", 5)
        await show_makeup_slots(query, int(obligation_id), class_date, int(duration_minutes))
        return
    if data.startswith("makeup:plan:slot:"):
        _, _, _, obligation_id_text, class_date, duration_text, start_time = data.split(":", 6)
        obligation_id = int(obligation_id_text)
        item = get_makeup_obligation(obligation_id)
        if not item:
            await query.edit_message_text("⚠️ That makeup obligation was not found.", reply_markup=main_keyboard())
            return
        duration_minutes = int(duration_text)
        planned_local = datetime.fromisoformat(f"{class_date}T{start_time}").replace(tzinfo=ZoneInfo(TIMEZONE))
        try:
            # Recheck availability on the callback to guard stale menus and duplicate clicks.
            available = get_available_makeup_slots(
                SCHEDULE, item["sections"].split(","), planned_local.strftime("%A"), class_date,
                duration_minutes, now=datetime.now(ZoneInfo(TIMEZONE)),
            )
            if not any(slot["start"] == start_time for slot in available):
                raise ValueError("That time is no longer available. Choose another open slot.")
            saved = schedule_makeup_class(
                obligation_id, planned_local.isoformat(timespec="minutes"), duration_minutes, SCHEDULE
            )
        except ValueError as exc:
            await query.edit_message_text(f"⚠️ {exc}", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🕒 Choose Another Time", callback_data=f"makeup:plan:duration:{obligation_id}:{class_date}:{duration_minutes}")],
                [InlineKeyboardButton("🗓️ Pending Obligations", callback_data="makeup:list")],
            ]))
            return
        notice, failures = await broadcast_scheduled_makeup(
            context.bot, TEACHER_TELEGRAM_ID, SECTION_GROUP_IDS,
            STUDENT_GROUP_CHAT_ID, saved, TIMEZONE,
        )
        await query.edit_message_text(notice, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗓️ Pending Obligations", callback_data="makeup:list")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
        ]))
        if failures:
            logging.getLogger(__name__).warning(
                "Makeup class notification delivery failed for %s recipient(s).", len(failures)
            )
        return
    if data.startswith("makeup:status:"):
        _, _, obligation_id_text, action = data.split(":", 3)
        obligation_id = int(obligation_id_text)
        if action == "planned":
            await show_makeup_days(query, obligation_id)
            return
        status = "Completed" if action == "completed" else "No Makeup Required"
        context.user_data.pop("pending_makeup_plan", None)
        try:
            item = set_makeup_status(obligation_id, status)
        except ValueError as exc:
            await query.edit_message_text(f"⚠️ {exc}", reply_markup=main_keyboard())
            return
        await query.edit_message_text(
            f"✅ Makeup obligation #{item['id']} marked {item['status']}. Credit-hour balances were not changed.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗓️ Pending Obligations", callback_data="makeup:list")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
            ])
        )
        return

    if data == "confirm:start":
        await begin_confirmation(query, context)
        return
    if data == "confirm:cancel":
        context.user_data.pop("pending_confirm", None)
        await query.edit_message_text("✅ Cancelled.", reply_markup=main_keyboard())
        return

    if data.startswith("confirm:day:"):
        day = data.split(":", 2)[2]
        sessions = [s for s in get_regular_sessions(SCHEDULE) if s["day"] == day]
        buttons = [
            [InlineKeyboardButton(
                f"📚 Section {s['section']} · {s['start']}–{s['end']}",
                callback_data=f"confirm:session:{s['id']}"
            )]
            for s in sessions
        ]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="confirm:start")])
        await query.edit_message_text(f"📅 Choose the regular class on {day}:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("confirm:session:"):
        session_id = data.split(":", 2)[2]
        session = next((s for s in get_regular_sessions(SCHEDULE) if s["id"] == session_id), None)
        if not session:
            await query.edit_message_text("⚠️ That timetable entry could not be found. Reload and try again.")
            return
        today = datetime.now(ZoneInfo(TIMEZONE)).date()
        class_date = most_recent_occurrence_date(session["day"], today).isoformat()
        existing = get_confirmation_for_occurrence(session["id"], class_date, session["day"])
        if existing:
            await query.edit_message_text(
                f"ℹ️ This regular session is already confirmed for {class_date} ({existing['sections']}), "
                f"{session['start']}–{session['end']}.",
                reply_markup=post_confirmation_markup(existing["id"])
            )
            return
        context.user_data["pending_confirm"] = {"session": session, "class_date": class_date}
        keyboard = [
            [InlineKeyboardButton(f"👥 Section {session['section']} Only", callback_data="confirm:mode:single")],
            [InlineKeyboardButton("👥 Combine Sections A + B", callback_data="confirm:mode:combined")],
            [InlineKeyboardButton("❌ Cancel", callback_data="confirm:cancel")]
        ]
        await query.edit_message_text(
            f"✅ Regular class selected:\n📘 {session['course']} — Section {session['section']}\n"
            f"🕒 {session['day']} {session['start']}–{session['end']}\n"
            f"🗓️ Class date: {class_date}\n\n"
            "Teach this section only, or combine both sections?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data.startswith("confirm:mode:"):
        pending = context.user_data.get("pending_confirm")
        if not pending:
            await query.edit_message_text("⏳ Your selection expired. Start again.", reply_markup=main_keyboard())
            return
        session = pending["session"]
        class_date = pending["class_date"]
        mode = data.split(":", 2)[2]
        sections = [session["section"]] if mode == "single" else ["A", "B"]
        duration = session["duration_minutes"]

        if mode == "combined":
            other_section = "B" if session["section"] == "A" else "A"
            conflicts = find_conflicts(
                SCHEDULE, section=other_section, day=session["day"],
                start=session["start"], end=session["end"],
                exclude_session_id=session["id"]
            )
            if conflicts:
                details = "\n".join(
                    f"• {c['course']} (Section {c['section']}), {c['start']}–{c['end']}"
                    for c in conflicts
                )
                await query.edit_message_text(
                    f"🚫 Cannot combine Sections A + B.\nSection {other_section} has a timetable conflict:\n{details}\n\n"
                    "ℹ️ No confirmation or credit deduction was made.",
                    reply_markup=main_keyboard()
                )
                context.user_data.pop("pending_confirm", None)
                return

        target_groups = {section: SECTION_GROUP_IDS.get(section) for section in sections}
        missing_sections = [section for section, chat_id in target_groups.items() if chat_id is None]
        if missing_sections:
            await query.edit_message_text(
                f"⚠️ Cannot confirm: notification group is not configured for Section {', '.join(missing_sections)}. "
                "No credits were deducted.",
                reply_markup=main_keyboard()
            )
            context.user_data.pop("pending_confirm", None)
            return
        try:
            for chat_id in set(target_groups.values()):
                group_member = await context.bot.get_chat_member(chat_id, context.bot.id)
                if group_member.status in {"left", "kicked"} or (
                    group_member.status == "restricted" and not getattr(group_member, "can_send_messages", False)
                ):
                    raise TelegramError("The bot cannot post in a configured group.")
        except TelegramError:
            await query.edit_message_text(
                "⚠️ Cannot reach one of the configured notification groups or the bot cannot post there. "
                "No confirmation or credit deduction was made. Add the bot to each section's group and verify IDs with /groupid.",
                reply_markup=main_keyboard()
            )
            context.user_data.pop("pending_confirm", None)
            return

        balances = get_balances()
        if any(balances[section] < duration / 60 for section in sections):
            insufficient = [f"Section {s}: {balances[s]:g} hours remaining" for s in sections if balances[s] < duration / 60]
            await query.edit_message_text(
                "⚠️ Cannot confirm because there are not enough credit hours:\n" + "\n".join(insufficient) +
                "\nNo credits were deducted.",
                reply_markup=main_keyboard()
            )
            context.user_data.pop("pending_confirm", None)
            return

        now = datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds")
        try:
            confirm_session(
                session_id=session["id"], day=session["day"], class_date=class_date, sections=sections,
                duration_hours=duration / 60, confirmed_at=now,
                course=session["course"], start=session["start"], end=session["end"]
            )
        except ValueError as exc:
            await query.edit_message_text(str(exc), reply_markup=main_keyboard())
            context.user_data.pop("pending_confirm", None)
            return

        new_balances = get_balances()
        notification_failures = []
        # Send once per distinct group. If both sections share a group, the combined
        # confirmation appears there only once.
        for chat_id in dict.fromkeys(target_groups.values()):
            destination_sections = [section for section in sections if target_groups[section] == chat_id]
            group_message = (
                f"✅ Class confirmed: {session['course']}\n"
                f"🕒 Time: {session['day']} {session['start']}–{session['end']}\n"
                f"👥 Sections: {', '.join(destination_sections)}\n"
                f"⏱️ Duration: {duration / 60:g} hour(s) per section"
            )
            try:
                await context.bot.send_message(chat_id=chat_id, text=group_message)
            except TelegramError:
                notification_failures.extend(destination_sections)
        notification_status = (
            "\nGroup notification sent to all selected section groups."
            if not notification_failures else
            f"\nConfirmation was saved, but notification failed for Section {', '.join(notification_failures)}."
        )
        summary = [
            f"✅ Confirmed: {session['course']}",
            f"🕒 Time: {session['day']} {session['start']}–{session['end']}",
            f"👥 Sections: {', '.join(sections)}",
            f"⏱️ Duration: {duration / 60:g} credit hour(s) per section",
            "💳 Remaining credit hours:",
        ]
        summary.extend([f"  ▫️ Section {s}: {new_balances[s]:g}" for s in sections])
        summary.append(notification_status)
        context.user_data.pop("pending_confirm", None)
        confirmation = get_confirmation(session["id"], class_date)
        await query.edit_message_text(
            "\n".join(summary),
            reply_markup=post_confirmation_markup(confirmation["id"]) if confirmation else main_keyboard(),
        )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_teacher(update):
        await update.message.reply_text("🔒 Sorry, this bot is for the authorized teacher only.")
        return
    context.user_data.pop("pending_confirm", None)
    await update.message.reply_text("✅ Current action cancelled.", reply_markup=main_keyboard())

async def start_makeup_reminder_task(application: Application):
    application.bot_data["section_group_ids"] = SECTION_GROUP_IDS
    application.bot_data["makeup_reminder_task"] = asyncio.create_task(
        reminder_scheduler(application, TEACHER_TELEGRAM_ID, TIMEZONE),
        name="classflow-makeup-reminders",
    )

async def stop_makeup_reminder_task(application: Application):
    task = application.bot_data.pop("makeup_reminder_task", None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

async def syllabus_document_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_syllabus_document(
        update, context, TEACHER_TELEGRAM_ID, TIMEZONE, SYLLABUS_MAX_UPLOAD_BYTES
    )

def main():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "ClassFlow must be started as a standalone process; do not call main() inside asyncio.run() or another running event loop."
        )

    # Python 3.12+ may start the main thread without a current event loop.
    # PTB's synchronous run_polling() needs one registered before Application setup.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        init_db()
        app = (
            Application.builder()
            .token(BOT_TOKEN)
            .post_init(start_makeup_reminder_task)
            .post_shutdown(stop_makeup_reminder_task)
            .build()
        )
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("cancel", cancel))
        app.add_handler(CommandHandler("makeups", makeups_command))
        app.add_handler(CommandHandler("myid", my_id))
        app.add_handler(CommandHandler("groupid", group_id))
        app.add_handler(MessageHandler(filters.Document.ALL, syllabus_document_upload))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plan_makeup_text))
        app.add_handler(CallbackQueryHandler(callback))
        print("ClassFlow Confirm Regular Class module is running.", flush=True)
        app.run_polling(close_loop=True)
    finally:
        # PTB normally closes the loop; clean it up too if startup exits early.
        if not loop.is_closed():
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
        asyncio.set_event_loop(None)

if __name__ == "__main__":
    main()
