import tempfile
import unittest
from pathlib import Path

import database


class SectionProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = Path(self.temp.name) / "progress.sqlite3"
        database.init_db()
        self.teacher_id = 102030
        self.other_teacher_id = 908070
        self.syllabus = database.create_manual_syllabus(self.teacher_id, "Systems", "2026-11-05")
        self.chapter_id = database.add_syllabus_chapter(self.syllabus["id"], self.teacher_id, "Chapter 1")
        database.add_syllabus_topics(
            self.syllabus["id"], self.chapter_id, self.teacher_id,
            ["Topic 1", "Topic 2", "Topic 3"],
        )

    def tearDown(self):
        database.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_progress_is_independent_by_section_and_moves_only_forward(self):
        topics = database.get_syllabus_chapter_topics(self.syllabus["id"], self.chapter_id, self.teacher_id)
        first, second, third = [row["id"] for row in topics]
        saved = database.record_section_chapter_progress(
            self.teacher_id, "A", self.syllabus["id"], self.chapter_id,
            "2026-11-03", "2026-11-03T12:00:00", topic_id=second,
        )
        self.assertTrue(saved["saved"])
        backwards = database.record_section_chapter_progress(
            self.teacher_id, "A", self.syllabus["id"], self.chapter_id,
            "2026-11-04", "2026-11-04T12:00:00", topic_id=first,
        )
        self.assertEqual(backwards["reason"], "not_forward")
        section_a = database.get_section_chapter_progress(self.teacher_id, "A")
        section_b = database.get_section_chapter_progress(self.teacher_id, "B")
        self.assertEqual(section_a["covered_topics"], 2)
        self.assertEqual(section_a["percent_complete"], 200 / 3)
        self.assertEqual(section_b["covered_topics"], 0)

        complete = database.record_section_chapter_progress(
            self.teacher_id, "A", self.syllabus["id"], self.chapter_id,
            "2026-11-05", "2026-11-05T12:00:00", completed=True,
        )
        self.assertTrue(complete["saved"])
        reopened = database.record_section_chapter_progress(
            self.teacher_id, "A", self.syllabus["id"], self.chapter_id,
            "2026-11-06", "2026-11-06T12:00:00", topic_id=third,
        )
        self.assertEqual(reopened["reason"], "completed")
        self.assertEqual(len(database.get_teaching_progress_history(self.teacher_id, "A")), 2)
        self.assertEqual(database.get_teaching_progress_history(self.teacher_id, "B"), [])

    def test_completed_chapters_batch_and_restart_persistence(self):
        second_chapter = database.add_syllabus_chapter(self.syllabus["id"], self.teacher_id, "Chapter 2")
        database.add_syllabus_topics(self.syllabus["id"], second_chapter, self.teacher_id, ["Topic 2.1"])
        result = database.record_section_chapters_completed(
            self.teacher_id, "B", self.syllabus["id"], [self.chapter_id, second_chapter],
            "2026-11-04", "2026-11-04T12:00:00",
        )
        self.assertEqual(result, [self.chapter_id, second_chapter])
        database.init_db()
        progress = database.get_section_chapter_progress(self.teacher_id, "B")
        self.assertTrue(all(item["status"] == "Completed" for item in progress["chapters"]))
        self.assertEqual(progress["percent_complete"], 100.0)

    def test_progress_is_scoped_to_the_syllabus_owner(self):
        with self.assertRaisesRegex(ValueError, "not available"):
            database.record_section_chapter_progress(
                self.other_teacher_id, "A", self.syllabus["id"], self.chapter_id,
                "2026-11-05", "2026-11-05T12:00:00", completed=True,
            )

    def test_teacher_database_scopes_are_isolated(self):
        first_path = Path(self.temp.name) / "teacher-one.sqlite3"
        second_path = Path(self.temp.name) / "teacher-two.sqlite3"
        with database.use_database(first_path):
            database.init_db()
            course = database.create_manual_syllabus(111, "Teacher One Course", "2026-11-05")
        with database.use_database(second_path):
            database.init_db()
            self.assertEqual(database.list_syllabuses(111), [])
            database.create_manual_syllabus(111, "Teacher Two Course", "2026-11-05")
        with database.use_database(first_path):
            self.assertEqual([item["title"] for item in database.list_syllabuses(111)], [course["title"]])


if __name__ == "__main__":
    unittest.main()
