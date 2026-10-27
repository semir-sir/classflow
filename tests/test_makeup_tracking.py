import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import database
from makeup import deliver_due_makeup_reminders, notify_cancelled_groups, broadcast_scheduled_makeup
from telegram.error import Forbidden
from timetable import confirmation_matches_timetable_date, most_recent_occurrence_date, load_timetable
from timetable import find_conflicts


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class FakeApplication:
    def __init__(self, bot):
        self.bot = bot


class ForbiddenBot(FakeBot):
    async def send_message(self, chat_id, text):
        raise Forbidden("private chat has not been started")


class MakeupTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test.sqlite3"
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def create_confirmation(self, sections=("A",), confirmation_id_date="2026-10-24"):
        database.confirm_session(
            session_id="sad-test-session",
            day="Saturday",
            class_date=confirmation_id_date,
            sections=list(sections),
            duration_hours=2.0,
            confirmed_at=f"{confirmation_id_date}T08:00:00+03:00",
            course="System Analysis and Design (InSy2061)",
            start="08:00",
            end="10:00",
        )
        return database.get_confirmation("sad-test-session", confirmation_id_date)

    def cancel(self, confirmation):
        return database.cancel_confirmation_occurrence(
            confirmation["id"], "2026-10-25", "2026-10-24T11:00:00+03:00"
        )

    def test_single_cancellation_notifies_group_and_does_not_change_balances(self):
        confirmation = self.create_confirmation()
        before = database.get_balances()
        obligation, created = self.cancel(confirmation)
        self.assertTrue(created)
        self.assertEqual(obligation["sections"], "A")
        self.assertEqual(obligation["duration_minutes"], 120)
        self.assertEqual(database.get_balances(), before)

        bot = FakeBot()
        failures = asyncio.run(notify_cancelled_groups(bot, obligation, {"A": -1001, "B": -1002}, "Africa/Addis_Ababa"))
        self.assertEqual(failures, [])
        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(bot.messages[0][0], -1001)
        self.assertIn("Original date: 2026-10-24", bot.messages[0][1])
        self.assertIn("Duration: 2 hour(s)", bot.messages[0][1])
        asyncio.run(notify_cancelled_groups(bot, obligation, {"A": -1001, "B": -1002}, "Africa/Addis_Ababa"))
        self.assertEqual(len(bot.messages), 1)

    def test_credit_analysis_reports_balance_percentages_and_active_sessions(self):
        database.confirm_session(
            session_id="credit-analysis-session", day="Monday", class_date="2026-10-26",
            sections=["A"], duration_hours=2.0, confirmed_at="2026-10-26T08:00:00+03:00",
            course="System Analysis and Design", start="02:30", end="04:30",
        )
        analysis = database.get_credit_analysis("A")
        self.assertEqual(analysis["baseline_hours"], 64.0)
        self.assertEqual(analysis["used_hours"], 2.0)
        self.assertEqual(analysis["remaining_hours"], 62.0)
        self.assertAlmostEqual(analysis["percent_used"], 100 * 2 / 64)
        self.assertAlmostEqual(analysis["percent_remaining"], 100 * 62 / 64)
        self.assertEqual(analysis["active_sessions"], 1)
        self.assertEqual(database.get_credit_analysis("B")["active_sessions"], 0)

        confirmation = database.get_confirmation("credit-analysis-session", "2026-10-26")
        obligation, _ = database.cancel_confirmation_occurrence(
            confirmation["id"], "2026-10-27", "2026-10-26T09:00:00+03:00"
        )
        database.set_makeup_status(obligation["id"], "Completed")
        analysis = database.get_credit_analysis("A")
        self.assertEqual(analysis["active_sessions"], 1)
        self.assertEqual(analysis["remaining_hours"], 62.0)

    def test_combined_sections_are_one_obligation_with_one_teaching_duration(self):
        confirmation = self.create_confirmation(("A", "B"))
        obligation, created = self.cancel(confirmation)
        self.assertTrue(created)
        self.assertEqual(obligation["sections"], "A,B")
        self.assertEqual(obligation["duration_minutes"], 120)
        self.assertEqual(len(database.get_group_notifications(obligation["id"])), 2)

        bot = FakeBot()
        asyncio.run(notify_cancelled_groups(bot, obligation, {"A": -1001, "B": -1002}, "Africa/Addis_Ababa"))
        self.assertEqual(len(bot.messages), 2)
        self.assertEqual({chat_id for chat_id, _ in bot.messages}, {-1001, -1002})
        self.assertTrue(all("Duration: 2 hour(s)" in text for _, text in bot.messages))
        self.assertEqual(database.get_balances(), {"A": 62.0, "B": 62.0})

    def test_repeated_cancel_attempt_creates_no_duplicate_record_or_notices(self):
        confirmation = self.create_confirmation(("A", "B"))
        first, created = self.cancel(confirmation)
        second, created_again = self.cancel(confirmation)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(database.list_makeup_obligations(include_closed=True)), 1)
        self.assertEqual(len(database.get_group_notifications(first["id"])), 2)

    def test_unconfirmed_timetable_occurrence_can_be_cancelled_without_balance_change(self):
        before = database.get_balances()
        values = dict(session_id="insy2061-B-tue", original_date="2026-10-27",
                      day="Tuesday", course="System Analysis and Design (InSy2061)",
                      sections=["B"], start="04:30", end="06:30", duration_minutes=120,
                      reminder_date="2026-10-28", created_at="2026-10-26T12:00:00+03:00")
        first, created = database.cancel_scheduled_occurrence(**values)
        second, duplicate_created = database.cancel_scheduled_occurrence(**values)
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertIsNone(first["confirmation_id"])
        self.assertEqual(first["duration_minutes"], 120)
        self.assertEqual(first["sections"], "B")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(database.get_balances(), before)
        self.assertEqual(len(database.get_group_notifications(first["id"])), 1)

    def test_free_slots_exclude_classes_and_lunch_window(self):
        schedule = load_timetable(Path(__file__).parents[1] / "data" / "timetable.json")
        slots = database.get_available_makeup_slots(
            schedule, ["B"], "Wednesday", "2026-10-28", 60
        )
        self.assertIn({"start": "04:30", "end": "05:30"}, slots)
        self.assertNotIn({"start": "09:30", "end": "10:30"}, slots)
        self.assertFalse(any(slot["start"] < "07:30" and "06:30" < slot["end"] for slot in slots))
        no_long_slot = database.get_available_makeup_slots(
            schedule, ["B"], "Wednesday", "2026-10-28", 240
        )
        self.assertEqual(no_long_slot, [])

    def test_scheduling_makeup_deducts_chosen_duration_once_and_persists(self):
        obligation, _ = database.cancel_scheduled_occurrence(
            session_id="insy2061-B-wed", original_date="2026-10-21", day="Wednesday",
            course="System Analysis and Design", sections=["B"], start="02:30", end="04:30",
            duration_minutes=120, reminder_date="2026-10-22", created_at="2026-10-20T08:00:00+03:00"
        )
        before = database.get_balances()
        schedule = load_timetable(Path(__file__).parents[1] / "data" / "timetable.json")
        scheduled = database.schedule_makeup_class(
            obligation["id"], "2026-10-28T04:30+03:00", 60, schedule
        )
        self.assertEqual(scheduled["status"], "Planned")
        self.assertEqual(scheduled["planned_duration_minutes"], 60)
        self.assertEqual(database.get_balances(), {"A": before["A"], "B": before["B"] - 1})
        database.init_db()
        saved = database.get_makeup_obligation(obligation["id"])
        self.assertEqual(saved["planned_datetime"], "2026-10-28T04:30+03:00")
        self.assertEqual(saved["planned_duration_minutes"], 60)
        with self.assertRaises(ValueError):
            database.schedule_makeup_class(obligation["id"], "2026-10-28T05:30+03:00", 60, schedule)
        self.assertEqual(database.get_balances()["B"], before["B"] - 1)

    def test_scheduled_makeup_confirmation_and_group_broadcast_are_mocked(self):
        bot = FakeBot()
        obligation = {
            "course": "System Analysis and Design", "sections": "B",
            "planned_datetime": "2026-10-28T04:30+03:00", "planned_duration_minutes": 90,
        }
        text, failures = asyncio.run(broadcast_scheduled_makeup(
            bot, 6020889329, {"A": -1001, "B": -1002}, -1003,
            obligation, "Africa/Addis_Ababa"
        ))
        self.assertEqual(failures, [])
        self.assertEqual({chat_id for chat_id, _ in bot.messages}, {6020889329, -1003})
        self.assertIn("2026-10-28 · 04:30–06:00", text)
        self.assertIn("Credit hour deducted successfully.", text)
        self.assertNotIn("each section", text)
        self.assertEqual(len(bot.messages), 2)

    def test_next_calendar_day_private_reminder_is_mocked_and_sent_once(self):
        confirmation = self.create_confirmation(("A", "B"))
        obligation, _ = self.cancel(confirmation)
        bot = FakeBot()
        application = FakeApplication(bot)
        before_due = datetime(2026, 10, 24, 23, 59, tzinfo=ZoneInfo("Africa/Addis_Ababa"))
        next_day = datetime(2026, 10, 25, 0, 1, tzinfo=ZoneInfo("Africa/Addis_Ababa"))

        self.assertEqual(asyncio.run(deliver_due_makeup_reminders(application, 6020889329, "Africa/Addis_Ababa", before_due)), 0)
        self.assertEqual(bot.messages, [])
        self.assertEqual(asyncio.run(deliver_due_makeup_reminders(application, 6020889329, "Africa/Addis_Ababa", next_day)), 1)
        database.init_db()
        self.assertEqual(asyncio.run(deliver_due_makeup_reminders(application, 6020889329, "Africa/Addis_Ababa", next_day)), 0)
        self.assertEqual(len(bot.messages), 1)
        recipient, text = bot.messages[0]
        self.assertEqual(recipient, 6020889329)
        for expected in (obligation["course"], "Section(s): A, B", "Original class date: 2026-10-24", "Time: 08:00–10:00", "Duration: 2 hour(s)"):
            self.assertIn(expected, text)
        saved = database.get_makeup_obligation(obligation["id"])
        self.assertIsNotNone(saved["reminder_sent_at"])

    def test_status_and_records_survive_database_reinitialization(self):
        confirmation = self.create_confirmation()
        obligation, _ = self.cancel(confirmation)
        before = database.get_balances()
        planned = database.set_makeup_status(
            obligation["id"], "Planned", "2026-10-27T09:30+03:00"
        )
        database.init_db()
        saved = database.get_makeup_obligation(obligation["id"])
        self.assertEqual(saved["status"], "Planned")
        self.assertEqual(saved["planned_datetime"], planned["planned_datetime"])
        self.assertEqual(len(database.list_makeup_obligations()), 1)
        database.set_makeup_status(obligation["id"], "Completed")
        self.assertEqual(database.get_balances(), before)
        with self.assertRaises(ValueError):
            database.set_makeup_status(obligation["id"], "Planned")

    def test_no_makeup_required_closes_obligation_without_balance_change(self):
        confirmation = self.create_confirmation()
        obligation, _ = self.cancel(confirmation)
        before = database.get_balances()
        saved = database.set_makeup_status(obligation["id"], "No Makeup Required")
        self.assertEqual(saved["status"], "No Makeup Required")
        self.assertEqual(database.list_makeup_obligations(), [])
        self.assertEqual(database.get_balances(), before)

    def test_forbidden_private_reminder_is_visible_and_can_retry_after_start(self):
        confirmation = self.create_confirmation()
        obligation, _ = self.cancel(confirmation)
        due = datetime(2026, 10, 25, 8, 0, tzinfo=ZoneInfo("Africa/Addis_Ababa"))
        failed = asyncio.run(deliver_due_makeup_reminders(
            FakeApplication(ForbiddenBot()), 6020889329, "Africa/Addis_Ababa", due
        ))
        self.assertEqual(failed, 0)
        saved = database.get_makeup_obligation(obligation["id"])
        self.assertIn("Forbidden", saved["reminder_error"])
        self.assertEqual(database.reset_forbidden_makeup_reminders(), 1)

        bot = FakeBot()
        delivered = asyncio.run(deliver_due_makeup_reminders(
            FakeApplication(bot), 6020889329, "Africa/Addis_Ababa", due
        ))
        self.assertEqual(delivered, 1)
        self.assertEqual(len(bot.messages), 1)

    def test_legacy_confirmation_schema_is_upgraded_before_makeup_foreign_keys(self):
        with database.connect() as conn:
            conn.execute("DROP TABLE makeup_group_notifications")
            conn.execute("DROP TABLE makeup_obligations")
            conn.execute("DROP TABLE confirmations")
            conn.execute("""
                CREATE TABLE confirmations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    course TEXT NOT NULL,
                    start TEXT NOT NULL,
                    end TEXT NOT NULL,
                    sections TEXT NOT NULL,
                    duration_hours REAL NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    UNIQUE(session_id, day)
                )
            """)
            conn.execute("""
                INSERT INTO confirmations
                    (session_id, day, course, start, end, sections, duration_hours, confirmed_at)
                VALUES ('legacy', 'Monday', 'Course', '08:00', '10:00', 'A', 2, '2026-10-24T08:00:00+03:00')
            """)
        database.init_db()
        migrated = database.get_confirmation("legacy", "2026-10-24")
        self.assertIsNotNone(migrated)
        with database.connect() as conn:
            foreign_tables = {
                row["table"] for row in conn.execute("PRAGMA foreign_key_list(makeup_obligations)")
            }
        self.assertEqual(foreign_tables, {"confirmations"})


class TimetableDateTests(unittest.TestCase):
    def test_updated_2026_schedule_has_all_entries_and_correct_local_sad_sessions(self):
        schedule = load_timetable(Path(__file__).parents[1] / "data" / "timetable.json")
        self.assertEqual(len(schedule), 24)
        sad = {(item["section"], item["day"], item["start"], item["end"])
               for item in schedule if item["course_type"] == "system_analysis_design"}
        self.assertEqual(sad, {
            ("A", "Monday", "02:30", "04:30"),
            ("A", "Friday", "02:30", "04:30"),
            ("B", "Tuesday", "04:30", "06:30"),
            ("B", "Thursday", "07:30", "09:30"),
        })
        conflicts = [
            find_conflicts(schedule, other_section, day, start, end, exclude_session_id=session_id)
            for other_section, day, start, end, session_id in (
                ("B", "Monday", "02:30", "04:30", "insy2061-A-mon"),
                ("B", "Friday", "02:30", "04:30", "insy2061-A-fri"),
                ("A", "Tuesday", "04:30", "06:30", "insy2061-B-tue"),
                ("A", "Thursday", "07:30", "09:30", "insy2061-B-thu"),
            )
        ]
        self.assertTrue(all(conflicts))

    def test_confirmation_date_uses_latest_real_timetable_weekday(self):
        today = datetime(2026, 10, 25).date()  # Sunday
        self.assertEqual(most_recent_occurrence_date("Wednesday", today).isoformat(), "2026-10-21")
        self.assertEqual(most_recent_occurrence_date("Friday", today).isoformat(), "2026-10-23")

    def test_sunday_confirmation_for_wednesday_course_is_rejected_as_legacy_mismatch(self):
        self.assertFalse(confirmation_matches_timetable_date({
            "day": "Wednesday", "class_date": "2026-10-25"
        }))
        self.assertTrue(confirmation_matches_timetable_date({
            "day": "Wednesday", "class_date": "2026-10-21"
        }))

    def test_mismatched_legacy_confirmation_still_blocks_double_confirmation_of_same_occurrence(self):
        with tempfile.TemporaryDirectory() as directory:
            original_db_path = database.DB_PATH
            database.DB_PATH = Path(directory) / "legacy.sqlite3"
            try:
                database.init_db()
                database.confirm_session(
                    session_id="wednesday-section-b",
                    day="Wednesday",
                    class_date="2026-10-25",  # Old bug: Sunday saved for Wednesday.
                    sections=["B"],
                    duration_hours=2,
                    confirmed_at="2026-10-25T08:00:00+03:00",
                    course="System Analysis and Design",
                    start="02:30",
                    end="04:30",
                )
                self.assertIsNotNone(database.get_confirmation_for_occurrence(
                    "wednesday-section-b", "2026-10-21", "Wednesday"
                ))
                self.assertIsNone(database.get_confirmation_for_occurrence(
                    "wednesday-section-b", "2026-10-28", "Wednesday"
                ))
            finally:
                database.DB_PATH = original_db_path


if __name__ == "__main__":
    unittest.main()
