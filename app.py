"""ClassFlow confirmation bot entry point.

This module restores the production app shell that the project tests expect:
- environment configuration for the active teacher and section groups;
- the regular-class session schedule loaded from the timetable JSON;
- confirmation callback handling for the session selection flow; and
- a minimal Telegram application bootstrap for the bot.
"""

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import (
    cancel_scheduled_occurrence,
    confirm_session,
    get_confirmation,
    get_makeup_occurrence,
)
from makeup import notify_cancelled_groups
from timetable import load_timetable, most_recent_occurrence_date, next_occurrence_date

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TIMETABLE_PATH = DATA_DIR / "timetable.json"


def _env_int(name, default=None):
    value = os.getenv(name, "")
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


TIMEZONE = os.getenv("TIMEZONE", "Africa/Addis_Ababa")
TEACHER_TELEGRAM_ID = _env_int("TEACHER_TELEGRAM_ID", 0) or 0
SECTION_GROUP_IDS = {
    "A": _env_int("TELEGRAM_GROUP_ID_A"),
    "B": _env_int("TELEGRAM_GROUP_ID_B"),
}
STUDENT_GROUP_CHAT_ID = _env_int("STUDENT_GROUP_CHAT_ID")
SCHEDULE = load_timetable(TIMETABLE_PATH)


def _start_time_for(session):
    return datetime.strptime(session["start"], "%H:%M").time()


def _end_time_for(session):
    return datetime.strptime(session["end"], "%H:%M").time()


def _resolve_session_class_date(session, now_value):
    latest_occurrence = most_recent_occurrence_date(session["day"], now_value.date())
    start_time = _start_time_for(session)
    end_time = _end_time_for(session)
    if now_value.date() == latest_occurrence and start_time <= now_value.time() < end_time:
        return (next_occurrence_date(session["day"], latest_occurrence + timedelta(days=1))).isoformat()
    if now_value.date() == latest_occurrence and now_value.time() >= end_time:
        return (next_occurrence_date(session["day"], latest_occurrence + timedelta(days=1))).isoformat()
    if now_value.date() == latest_occurrence and now_value.time() < start_time:
        return latest_occurrence.isoformat()
    return latest_occurrence.isoformat()


def _session_for_id(session_id):
    return next((item for item in SCHEDULE if item["id"] == session_id), None)


async def _handle_late_session(query, context, session, class_date):
    existing = get_makeup_occurrence(session["id"], class_date)
    if existing:
        await query.edit_message_text(
            f"⚠️ This class was already cancelled and tracked for {class_date}. "
            f"The next available regular time is {next_occurrence_date(session['day'], date.fromisoformat(class_date) + timedelta(days=1)).isoformat()}.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔁 Choose another class", callback_data="makeup:home")],
            ]),
        )
        return True
    reminder_date = (date.fromisoformat(class_date) + timedelta(days=1)).isoformat()
    obligation, created = cancel_scheduled_occurrence(
        session_id=session["id"],
        original_date=class_date,
        day=session["day"],
        course=session["course"],
        sections=[session["section"]],
        start=session["start"],
        end=session["end"],
        duration_minutes=session["duration_minutes"],
        reminder_date=reminder_date,
        created_at=datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds"),
    )
    if context.bot is not None and SECTION_GROUP_IDS:
        await notify_cancelled_groups(context.bot, obligation, SECTION_GROUP_IDS, TIMEZONE)
    next_week = next_occurrence_date(session["day"], date.fromisoformat(class_date) + timedelta(days=1)).isoformat()
    context.user_data["pending_confirm"] = {
        "session_id": session["id"],
        "class_date": next_week,
        "sections": [session["section"]],
    }
    await query.edit_message_text(
        f"⚠️ This class has already ended before confirmation. A makeup obligation was created for {class_date}. "
        f"The next regular session is {next_week}.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm next session", callback_data=f"confirm:session:{session['id']}")],
        ]),
    )
    return True


async def callback(update, context):
    """Process callback queries used by the confirmation flow."""
    query = getattr(update, "callback_query", None)
    if query is None or not query.data:
        return False

    data = query.data
    if data.startswith("confirm:session:"):
        session_id = data.split(":", 2)[2]
        session = _session_for_id(session_id)
        if session is None:
            return False

        now_value = datetime.now(ZoneInfo(TIMEZONE))
        class_date = _resolve_session_class_date(session, now_value)
        pending = context.user_data.setdefault("pending_confirm", {})
        pending.update({
            "session_id": session_id,
            "sections": [session["section"]],
            "class_date": class_date,
        })

        start_time = _start_time_for(session)
        end_time = _end_time_for(session)
        if start_time <= now_value.time() < end_time:
            await query.edit_message_text(
                f"⏳ This class has already started. The next available date for {session['course']} is {class_date}.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Confirm next session", callback_data=f"confirm:session:{session_id}")],
                ]),
            )
            return True
        if now_value.time() >= end_time:
            return await _handle_late_session(query, context, session, now_value.date().isoformat())

        await query.edit_message_text(
            f"📘 {session['course']}\n👥 Section: {session['section']}\n🗓️ Class date: {class_date}\n🕒 {session['start']}–{session['end']}\n\nChoose how to confirm this session.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm this class", callback_data="confirm:mode:single")],
            ]),
        )
        return True

    if data == "confirm:mode:single":
        pending = context.user_data.get("pending_confirm")
        if not pending:
            return False
        session = _session_for_id(pending["session_id"])
        if session is None:
            return False

        now_value = datetime.now(ZoneInfo(TIMEZONE))
        class_date = pending.get("class_date") or _resolve_session_class_date(session, now_value)
        start_time = _start_time_for(session)
        end_time = _end_time_for(session)

        if now_value.time() >= end_time or (now_value.date().isoformat() == class_date and now_value.time() >= end_time):
            return await _handle_late_session(query, context, session, class_date)
        if start_time <= now_value.time() < end_time and now_value.date().isoformat() == class_date:
            pending["class_date"] = next_occurrence_date(session["day"], date.fromisoformat(class_date) + timedelta(days=1)).isoformat()
            await query.edit_message_text(
                f"⏳ This class has already started. The next available date is {pending['class_date']}.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Confirm next session", callback_data=f"confirm:session:{session['id']}")],
                ]),
            )
            return True

        try:
            confirm_session(
                session_id=session["id"],
                day=session["day"],
                class_date=class_date,
                sections=[session["section"]],
                duration_hours=session["duration_minutes"] / 60,
                confirmed_at=datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds"),
                course=session["course"],
                start=session["start"],
                end=session["end"],
            )
        except ValueError as exc:
            await query.edit_message_text(f"⚠️ {exc}")
            return True

        pending["class_date"] = class_date
        await query.edit_message_text(
            f"✅ Class confirmed\n📘 {session['course']}\n👥 Section: {session['section']}\n🗓️ Class date: {class_date}\n🕒 {session['start']}–{session['end']}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
            ]),
        )
        return True

    return False


def main():
    """Run the bot using the configured Telegram token, if present."""
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not configured in the environment.")

    from telegram.ext import ApplicationBuilder

    application = ApplicationBuilder().token(token).build()
    application.add_handler(
        __import__("telegram.ext").ext.CallbackQueryHandler(callback)
    )
    application.run_polling()


__all__ = [
    "BASE_DIR",
    "DATA_DIR",
    "TIMEZONE",
    "TEACHER_TELEGRAM_ID",
    "SECTION_GROUP_IDS",
    "STUDENT_GROUP_CHAT_ID",
    "SCHEDULE",
    "callback",
    "main",
]
