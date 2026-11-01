import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import database
from chapter_management import (
    CHAPTERS_PER_PAGE, _chapter_view, _list_view, handle_chapter_callback,
    handle_chapter_document, handle_chapter_text,
)


TEACHER = 6020889329
OTHER_TEACHER = 123456789
TIMEZONE = "Africa/Addis_Ababa"


class FakeMessage:
    def __init__(self, document=None, text=None, chat_id=77):
        self.document = document
        self.text = text
        self.chat_id = chat_id
        self.message_id = 99
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs.get("reply_markup")))


class FakeQuery:
    def __init__(self, data, chat_id=77, user_id=TEACHER):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage(chat_id=chat_id)
        self.text = None
        self.reply_markup = None

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.text = text
        self.reply_markup = reply_markup


class FakeBot:
    def __init__(self):
        self.edits = []
        self.sent_documents = []

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)

    async def send_document(self, **kwargs):
        self.sent_documents.append(kwargs)


class FakeContext:
    def __init__(self, user_data=None):
        self.user_data = user_data if user_data is not None else {}
        self.bot = FakeBot()


class ChapterManagementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.directory.name) / "chapters.sqlite3"
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.directory.cleanup()

    def chapters(self, teacher=TEACHER):
        database.ensure_default_teacher_chapters(teacher, "test-seed")
        return database.list_teacher_chapters(teacher, limit=100, offset=0)["items"]

    def make_doc(self, filename="notes.pdf", mime="application/pdf", size=12,
                 file_id="tg-file-1", unique_id="tg-unique-1"):
        document = SimpleNamespace(
            file_name=filename, mime_type=mime, file_size=size,
            file_id=file_id, file_unique_id=unique_id,
        )
        return document

    def update(self, message, user=TEACHER, chat_id=77, chat_type="private"):
        return SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=user),
            effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        )

    def upload_state(self, chapter_id, chat_id=77):
        return {"chapter_id": chapter_id, "page": 0, "chat_id": chat_id,
                "expires_at": time.time() + 60}

    def test_five_defaults_are_created_once_per_teacher_and_persist(self):
        query = FakeQuery("chapter:list:0")
        context = FakeContext()
        asyncio.run(handle_chapter_callback(query, context, TEACHER, TIMEZONE))
        names = [row["name"] for row in self.chapters()]
        self.assertEqual(names, [f"Chapter {index}" for index in range(1, 6)])
        self.assertTrue(all(not row["material_attached"] for row in self.chapters()))
        asyncio.run(handle_chapter_callback(FakeQuery("chapter:list:0"), context, TEACHER, TIMEZONE))
        self.assertEqual(len(self.chapters()), 5)
        asyncio.run(handle_chapter_callback(FakeQuery("chapter:list:0", user_id=OTHER_TEACHER), context,
                                           OTHER_TEACHER, TIMEZONE))
        self.assertEqual([row["name"] for row in self.chapters(OTHER_TEACHER)], [
            f"Chapter {index}" for index in range(1, 6)
        ])
        database.init_db()
        self.assertEqual([row["name"] for row in self.chapters()], names)

    def test_add_chapter_validation_casefold_duplicates_and_pagination(self):
        database.ensure_default_teacher_chapters(TEACHER, "now")
        with self.assertRaisesRegex(ValueError, "chapter name"):
            database.add_teacher_chapter(TEACHER, "   ", "now")
        with self.assertRaisesRegex(ValueError, "80 characters"):
            database.add_teacher_chapter(TEACHER, "x" * 81, "now")
        database.add_teacher_chapter(TEACHER, "Advanced Topics", "now")
        with self.assertRaisesRegex(ValueError, "already exists"):
            database.add_teacher_chapter(TEACHER, "advanced topics", "now")
        for number in range(6, 13):
            database.add_teacher_chapter(TEACHER, f"Chapter {number}", "now")
        text, markup, page = _list_view(TEACHER, 0)
        self.assertEqual(page, 0)
        self.assertEqual(text.count("📖 Chapter"), CHAPTERS_PER_PAGE)
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("chapter:list:1", callbacks)
        _, next_markup, page = _list_view(TEACHER, 2)
        self.assertEqual(page, 2)
        nav = [button.callback_data for row in next_markup.inline_keyboard for button in row]
        self.assertIn("chapter:list:1", nav)
        self.assertNotIn("chapter:list:3", nav)

    def test_chapter_add_conversation_cancel_and_save(self):
        database.ensure_default_teacher_chapters(TEACHER, "now")
        context = FakeContext()
        query = FakeQuery("chapter:add:0")
        asyncio.run(handle_chapter_callback(query, context, TEACHER, TIMEZONE))
        self.assertIn("chapter_add", context.user_data)
        blank = FakeMessage(text="   ")
        asyncio.run(handle_chapter_text(self.update(blank), context, TEACHER, TIMEZONE))
        self.assertIn("cannot be empty", blank.replies[0][0])
        duplicate = FakeMessage(text="chapter 1")
        asyncio.run(handle_chapter_text(self.update(duplicate), context, TEACHER, TIMEZONE))
        self.assertIn("already exists", duplicate.replies[0][0])
        too_long = FakeMessage(text="x" * 81)
        asyncio.run(handle_chapter_text(self.update(too_long), context, TEACHER, TIMEZONE))
        self.assertIn("80 characters", too_long.replies[0][0])
        good = FakeMessage(text="  Field   Work  ")
        asyncio.run(handle_chapter_text(self.update(good), context, TEACHER, TIMEZONE))
        self.assertIn("Field Work", [chapter["name"] for chapter in self.chapters()])
        self.assertNotIn("chapter_add", context.user_data)

        asyncio.run(handle_chapter_callback(FakeQuery("chapter:add:0"), context, TEACHER, TIMEZONE))
        cancel_query = FakeQuery("chapter:addcancel:0")
        asyncio.run(handle_chapter_callback(cancel_query, context, TEACHER, TIMEZONE))
        self.assertNotIn("chapter_add", context.user_data)
        self.assertIn("Chapter Management", cancel_query.text)

    def test_empty_chapter_prompt_and_chapter_specific_upload(self):
        database.ensure_default_teacher_chapters(TEACHER, "now")
        first, second = self.chapters()[:2]
        text, markup = _chapter_view(first, 0)
        self.assertIn("No teaching material", text)
        self.assertIn("any file type", text)
        self.assertTrue(any(button.callback_data == f"chapter:upload:{first['id']}:0"
                            for row in markup.inline_keyboard for button in row))

        context = FakeContext()
        query = FakeQuery(f"chapter:upload:{first['id']}:0")
        asyncio.run(handle_chapter_callback(query, context, TEACHER, TIMEZONE))
        self.assertEqual(context.user_data["chapter_upload"]["chapter_id"], first["id"])
        document = self.make_doc(file_id="file-for-first")
        message = FakeMessage(document=document)
        handled = asyncio.run(handle_chapter_document(self.update(message), context, TEACHER,
                                                     TIMEZONE, 1_000))
        self.assertTrue(handled)
        saved_first = database.get_teacher_chapter(first["id"], TEACHER)
        saved_second = database.get_teacher_chapter(second["id"], TEACHER)
        self.assertEqual(saved_first["file_id"], "file-for-first")
        self.assertTrue(saved_first["material_attached"])
        self.assertFalse(saved_second["material_attached"])
        self.assertNotIn("chapter_upload", context.user_data)
        self.assertIn("Material attached", message.replies[0][0])
        self.assertEqual(saved_first["filename"], "notes.pdf")

    def test_multiple_document_types_are_accepted_but_oversized_uploads_are_rejected(self):
        chapters = self.chapters()[:3]
        for chapter, document in zip(chapters, [
            self.make_doc("slides.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", file_id="pptx-file"),
            self.make_doc("table.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", file_id="xlsx-file"),
            self.make_doc("drawing.custom", "application/x-custom", file_id="custom-file"),
        ]):
            context = FakeContext({"chapter_upload": self.upload_state(chapter["id"])})
            message = FakeMessage(document=document)
            handled = asyncio.run(handle_chapter_document(self.update(message), context, TEACHER, TIMEZONE, 1000))
            self.assertTrue(handled)
            saved = database.get_teacher_chapter(chapter["id"], TEACHER)
            self.assertEqual(saved["file_id"], document.file_id)
            self.assertEqual(saved["filename"], document.file_name)
            self.assertEqual(saved["mime_type"], document.mime_type)

        chapter = self.chapters()[3]
        context = FakeContext({"chapter_upload": self.upload_state(chapter["id"])})
        oversized = FakeMessage(document=self.make_doc("large.pptx", "application/octet-stream", size=1001))
        asyncio.run(handle_chapter_document(self.update(oversized), context, TEACHER, TIMEZONE, 1000))
        self.assertIn("upload limit", oversized.replies[-1][0])
        self.assertFalse(database.get_teacher_chapter(chapter["id"], TEACHER)["material_attached"])

    def test_switching_or_text_cancels_pending_upload_and_does_not_leak_to_other_chapter(self):
        database.ensure_default_teacher_chapters(TEACHER, "now")
        chapter = self.chapters()[0]
        context = FakeContext({"chapter_upload": self.upload_state(chapter["id"])})
        stray_text = FakeMessage(text="hello")
        self.assertTrue(asyncio.run(handle_chapter_text(self.update(stray_text), context, TEACHER, TIMEZONE)))
        self.assertNotIn("chapter_upload", context.user_data)
        asyncio.run(handle_chapter_callback(FakeQuery("chapter:upload:" + str(chapter["id"]) + ":0"),
                                           context, TEACHER, TIMEZONE))
        self.assertIn("chapter_upload", context.user_data)
        self.assertFalse(asyncio.run(handle_chapter_callback(FakeQuery("makeup:home"), context, TEACHER, TIMEZONE)))
        self.assertNotIn("chapter_upload", context.user_data)

    def test_owner_checks_invalid_chapter_callback_and_wrong_chat(self):
        chapter = self.chapters()[0]
        context = FakeContext()
        hostile = FakeQuery(f"chapter:open:{chapter['id']}:0", user_id=OTHER_TEACHER)
        asyncio.run(handle_chapter_callback(hostile, context, TEACHER, TIMEZONE))
        self.assertIn("Teacher access only", hostile.text)
        forged = FakeQuery(f"chapter:open:{chapter['id']}:0", user_id=OTHER_TEACHER)
        asyncio.run(handle_chapter_callback(forged, context, OTHER_TEACHER, TIMEZONE))
        # This call uses the other teacher identity and must not reveal the real record.
        self.assertIn("not found", forged.text)

        context.user_data["chapter_upload"] = self.upload_state(chapter["id"])
        group_message = FakeMessage(document=self.make_doc())
        asyncio.run(handle_chapter_document(self.update(group_message, chat_id=-100, chat_type="group"),
                                            context, TEACHER, TIMEZONE, 1000))
        self.assertFalse(database.get_teacher_chapter(chapter["id"], TEACHER)["material_attached"])

    def test_replacement_changes_only_selected_chapter_and_details_resend_file_id(self):
        chapters = self.chapters()[:2]
        original = database.attach_chapter_material(
            chapters[0]["id"], TEACHER, "old-file-id", "old-unique", "old.pdf", "application/pdf", "now"
        )
        untouched = database.attach_chapter_material(
            chapters[1]["id"], TEACHER, "second-file-id", "second-unique", "second.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "now"
        )
        self.assertIn("old.pdf", _chapter_view(original, 0)[0])
        context = FakeContext()
        replace_query = FakeQuery(f"chapter:replace:{chapters[0]['id']}:0")
        asyncio.run(handle_chapter_callback(replace_query, context, TEACHER, TIMEZONE))
        self.assertIn("will replace old.pdf", replace_query.text)
        upload_query = FakeQuery(f"chapter:upload:{chapters[0]['id']}:0")
        asyncio.run(handle_chapter_callback(upload_query, context, TEACHER, TIMEZONE))
        message = FakeMessage(document=self.make_doc("new.pdf", file_id="new-file-id"))
        asyncio.run(handle_chapter_document(self.update(message), context, TEACHER, TIMEZONE, 1000))
        self.assertEqual(database.get_teacher_chapter(chapters[0]["id"], TEACHER)["file_id"], "new-file-id")
        self.assertEqual(database.get_teacher_chapter(chapters[1]["id"], TEACHER)["file_id"], untouched["file_id"])

        send_query = FakeQuery(f"chapter:send:{chapters[1]['id']}:0")
        context.bot = FakeBot()
        asyncio.run(handle_chapter_callback(send_query, context, TEACHER, TIMEZONE))
        self.assertEqual(context.bot.sent_documents[0]["document"], "second-file-id")

    def test_database_failure_does_not_claim_success_or_clear_retry_state(self):
        chapter = self.chapters()[0]
        context = FakeContext({"chapter_upload": self.upload_state(chapter["id"])})
        message = FakeMessage(document=self.make_doc())
        with patch("chapter_management.database.attach_chapter_material", side_effect=sqlite3.OperationalError("disk")):
            with self.assertLogs("chapter_management", level="ERROR"):
                asyncio.run(handle_chapter_document(self.update(message), context, TEACHER, TIMEZONE, 1000))
        self.assertNotIn("Material attached", " ".join(reply[0] for reply in message.replies))
        self.assertIn("couldn't save", message.replies[-1][0])
        self.assertIn("chapter_upload", context.user_data)


if __name__ == "__main__":
    unittest.main()
