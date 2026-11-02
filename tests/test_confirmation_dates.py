import asyncio
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app
import database
from timetable import next_occurrence_date


TEACHER_ID = app.TEACHER_TELEGRAM_ID
TIMEZONE = "Africa/Addis_Ababa"


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=TEACHER_ID)
        self.text = None
        self.reply_markup = None

    async def answer(self):
        pass

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.text = text
        self.reply_markup = reply_markup


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class FrozenDateTime(datetime):
    current = None

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        if tz is not None:
            return value.astimezone(tz)
        return value.replace(tzinfo=None)


class ConfirmationDateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "confirmation-dates.sqlite3"
        database.init_db()
        self.schedule = [{
            "id": "insy2061-B-tue",
            "day": "Tuesday",
            "course": "System Analysis and Design (InSy2061)",
            "section": "B",
            "start": "04:30",
            "end": "06:30",
            "duration_minutes": 120,
            "course_type": "system_analysis_design",
        }]
        self.patches = [
            patch.object(app, "SCHEDULE", self.schedule),
            patch.object(app, "SECTION_GROUP_IDS", {"A": -1001, "B": -1002}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def choose_session(self, fixed_time, session_id="insy2061-B-tue"):
        FrozenDateTime.current = fixed_time
        query = FakeQuery(f"confirm:session:{session_id}")
        bot = FakeBot()
        context = SimpleNamespace(user_data={}, bot=bot)
        self.run_callback(query, context, fixed_time)
        return query, context, bot

    def run_callback(self, query, context, fixed_time):
        FrozenDateTime.current = fixed_time
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=TEACHER_ID),
        )
        with patch.object(app, "datetime", FrozenDateTime):
            asyncio.run(app.callback(update, context))

    def test_tuesday_after_class_ends_creates_obligation_and_selects_next_tuesday(self):
        now = datetime(2026, 11, 3, 12, 0, tzinfo=ZoneInfo(TIMEZONE))
        query, context, bot = self.choose_session(now)

        obligation = database.get_makeup_occurrence("insy2061-B-tue", "2026-11-03")
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation["sections"], "B")
        self.assertEqual(obligation["reminder_date"], "2026-11-04")
        self.assertEqual(context.user_data["pending_confirm"]["class_date"], "2026-11-10")
        self.assertIn("already ended", query.text)
        self.assertIn("2026-11-10", query.text)
        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(bot.messages[0][0], -1002)
        self.assertEqual(database.get_balances(), {"A": 64.0, "B": 64.0})
        self.assertIsNone(database.get_confirmation("insy2061-B-tue", "2026-11-03"))

    def test_before_start_confirms_the_occurrence_today(self):
        now = datetime(2026, 11, 3, 1, 0, tzinfo=ZoneInfo(TIMEZONE))
        query, context, bot = self.choose_session(now)
        self.assertEqual(context.user_data["pending_confirm"]["class_date"], "2026-11-03")
        self.assertIn("Class date: 2026-11-03", query.text)
        self.assertEqual(database.list_makeup_obligations(include_closed=True), [])
        self.assertEqual(bot.messages, [])

    def test_monday_after_noon_tracks_missed_occurrence_and_selects_next_monday(self):
        monday_session = {
            "id": "insy2061-A-mon", "day": "Monday",
            "course": "System Analysis and Design (InSy2061)", "section": "A",
            "start": "02:30", "end": "04:30", "duration_minutes": 120,
            "course_type": "system_analysis_design",
        }
        now = datetime(2026, 11, 2, 12, 0, tzinfo=ZoneInfo(TIMEZONE))
        with patch.object(app, "SCHEDULE", [monday_session]):
            query, context, bot = self.choose_session(now, "insy2061-A-mon")
        self.assertIsNotNone(database.get_makeup_occurrence("insy2061-A-mon", "2026-11-02"))
        self.assertEqual(context.user_data["pending_confirm"]["class_date"], "2026-11-09")
        self.assertIn("2026-11-09", query.text)
        self.assertEqual([chat_id for chat_id, _ in bot.messages], [-1001])

    def test_started_but_not_ended_class_is_not_confirmed_late(self):
        now = datetime(2026, 11, 3, 5, 0, tzinfo=ZoneInfo(TIMEZONE))
        query, context, bot = self.choose_session(now)
        self.assertEqual(context.user_data["pending_confirm"]["class_date"], "2026-11-10")
        self.assertIn("already started", query.text)
        self.assertEqual(database.list_makeup_obligations(include_closed=True), [])
        self.assertEqual(bot.messages, [])

    def test_final_section_choice_rechecks_time_and_reoffers_next_week(self):
        before_start = datetime(2026, 11, 3, 1, 0, tzinfo=ZoneInfo(TIMEZONE))
        _, context, bot = self.choose_session(before_start)
        self.assertEqual(context.user_data["pending_confirm"]["class_date"], "2026-11-03")

        after_end = datetime(2026, 11, 3, 12, 0, tzinfo=ZoneInfo(TIMEZONE))
        query = FakeQuery("confirm:mode:single")
        self.run_callback(query, context, after_end)
        self.assertIn("ended before confirmation", query.text)
        self.assertEqual(context.user_data["pending_confirm"]["class_date"], "2026-11-10")
        self.assertIsNotNone(database.get_makeup_occurrence("insy2061-B-tue", "2026-11-03"))
        self.assertIsNone(database.get_confirmation("insy2061-B-tue", "2026-11-03"))
        self.assertEqual(database.get_balances(), {"A": 64.0, "B": 64.0})
        self.assertEqual(len(bot.messages), 1)  # missed-class notice only, no regular-class confirmation

    def test_retry_does_not_duplicate_missed_obligation_or_group_notification(self):
        now = datetime(2026, 11, 3, 12, 0, tzinfo=ZoneInfo(TIMEZONE))
        _, _, first_bot = self.choose_session(now)
        query, context, retry_bot = self.choose_session(now)
        self.assertEqual(context.user_data["pending_confirm"]["class_date"], "2026-11-10")
        self.assertEqual(len(database.list_makeup_obligations(include_closed=True)), 1)
        self.assertEqual(len(first_bot.messages), 1)
        self.assertEqual(retry_bot.messages, [])
        self.assertIn("already cancelled and tracked", query.text)

    def test_weekday_resolver_never_rolls_back_to_a_past_occurrence(self):
        self.assertEqual(
            next_occurrence_date("Monday", date(2026, 11, 2)).isoformat(), "2026-11-02"
        )
        self.assertEqual(
            next_occurrence_date("Monday", date(2026, 11, 3)).isoformat(), "2026-11-09"
        )


if __name__ == "__main__":
    unittest.main()
