import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram.error import TelegramError

from database import (
    claim_makeup_reminder,
    claim_group_notification,
    finish_group_notification,
    finish_makeup_reminder,
    get_group_notifications,
    list_due_makeup_reminders,
    list_pending_group_notifications,
)


def format_makeup_reminder(obligation):
    sections = ", ".join(obligation["sections"].split(","))
    duration_hours = obligation["duration_minutes"] / 60
    return (
        "🔔 Makeup class reminder: a cancelled class needs a makeup plan.\n"
        f"📘 Course: {obligation['course']}\n"
        f"👥 Section(s): {sections}\n"
        f"🗓️ Original class date: {obligation['original_date']}\n"
        f"🕒 Time: {obligation['start']}–{obligation['end']}\n"
        f"⏱️ Duration: {duration_hours:g} hour(s)\n\n"
        "📝 Use /makeups to plan or update its status."
    )


async def deliver_due_makeup_reminders(application, teacher_id, timezone, now=None):
    """Deliver each due reminder at most once, with a persistent claim before I/O."""
    now = now or datetime.now(ZoneInfo(timezone))
    stamp = now.isoformat(timespec="seconds")
    obligations = list_due_makeup_reminders(now.date().isoformat())
    delivered = 0
    for obligation in obligations:
        if not claim_makeup_reminder(obligation["id"], stamp):
            continue
        try:
            await application.bot.send_message(
                chat_id=teacher_id,
                text=format_makeup_reminder(obligation),
            )
        except TelegramError as exc:
            # Keep the obligation pending and visible; do not retry automatically,
            # which could duplicate a message when Telegram's response is ambiguous.
            finish_makeup_reminder(
                obligation["id"],
                error=f"Private reminder delivery failed ({type(exc).__name__}).",
            )
        else:
            finish_makeup_reminder(obligation["id"], sent_at=stamp)
            delivered += 1
    return delivered


async def reminder_scheduler(application, teacher_id, timezone, interval_seconds=60):
    """Check persistent due reminders on startup and periodically while polling."""
    while True:
        try:
            await notify_pending_cancelled_groups(
                application.bot, application.bot_data.get("section_group_ids", {}), timezone
            )
            await deliver_due_makeup_reminders(application, teacher_id, timezone)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Avoid exposing configuration or credentials in logs.
            logging.getLogger(__name__).error("Makeup reminder check failed (%s).", type(exc).__name__)
        await asyncio.sleep(interval_seconds)


async def _send_group_notice(bot, notice, section_group_ids, timezone):
    stamp = datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")
    if not claim_group_notification(notice["notification_id"], stamp):
        return None
    chat_id = section_group_ids.get(notice["section"])
    if chat_id is None:
        finish_group_notification(
            notice["notification_id"], error=f"No Telegram group configured for Section {notice['section']}."
        )
        return notice["section"]
    text = (
        f"❌ Class cancelled: {notice['course']}\n"
        f"👥 Section: {notice['section']}\n"
        f"🗓️ Original date: {notice['original_date']}\n"
        f"🕒 Time: {notice['start']}–{notice['end']}\n"
        f"⏱️ Duration: {notice['duration_minutes'] / 60:g} hour(s)"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except TelegramError as exc:
        finish_group_notification(
            notice["notification_id"], error=f"Group notification failed ({type(exc).__name__})."
        )
        return notice["section"]
    finish_group_notification(
        notice["notification_id"], sent_at=datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")
    )
    return None


async def notify_cancelled_groups(bot, obligation, section_group_ids, timezone):
    """Send one immediate cancellation notice to each affected section group."""
    failures = []
    details = {key: obligation[key] for key in ("course", "original_date", "start", "end", "duration_minutes")}
    for saved_notice in get_group_notifications(obligation["id"]):
        notice = {**details, **saved_notice, "notification_id": saved_notice["id"]}
        failure = await _send_group_notice(bot, notice, section_group_ids, timezone)
        if failure:
            failures.append(failure)
    return failures


async def notify_pending_cancelled_groups(bot, section_group_ids, timezone):
    """Recover queued group notices after a restart without duplicating attempted sends."""
    for notice in list_pending_group_notifications():
        await _send_group_notice(bot, notice, section_group_ids, timezone)


async def broadcast_scheduled_makeup(bot, teacher_id, section_group_ids, student_group_id, obligation, timezone):
    """Send a scheduled makeup confirmation privately and to the student audience."""
    planned = datetime.fromisoformat(obligation["planned_datetime"]).astimezone(ZoneInfo(timezone))
    duration = obligation["planned_duration_minutes"]
    end_minutes = planned.hour * 60 + planned.minute + duration
    end_time = f"{end_minutes // 60:02d}:{end_minutes % 60:02d}"
    text = (
        f"✅ Makeup class scheduled\n📘 Course: {obligation['course']}\n👥 Section(s): {obligation['sections']}\n"
        f"🗓️ Date: {planned.date().isoformat()} · {planned:%H:%M}–{end_time}\n"
        f"⏱️ Duration: {duration / 60:g} hour(s)\n"
        "💳 Credit hour deducted successfully."
    )
    recipients = ([student_group_id] if student_group_id is not None else
                  [section_group_ids[section] for section in obligation["sections"].split(",")
                   if section_group_ids.get(section) is not None])
    failures = []
    for chat_id in dict.fromkeys([teacher_id, *recipients]):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except TelegramError as exc:
            failures.append((chat_id, type(exc).__name__))
    return text, failures
