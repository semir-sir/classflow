"""Telegram UI for teacher-reviewed syllabuses and per-class lesson progress."""

import hashlib
import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

import database
from lesson_prep import (
    SyllabusDocumentError, extract_syllabus, find_outline_node, flatten_outline,
    outline_path_text, parse_outline_path, remove_outline_node,
)


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_MIME_TYPES = {
    ".pdf": {"application/pdf", "application/octet-stream"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/octet-stream",
    },
    ".pptx": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/octet-stream",
    },
}
REVIEW_PAGE_SIZE = 8
TOPIC_PAGE_SIZE = 8
CLASS_PAGE_SIZE = 8
_STATUS_CODES = {
    "p": "Planned",
    "q": "Partially covered",
    "c": "Completed",
    "n": "Not covered",
}
_STATUS_ICONS = {
    "Planned": "📝",
    "Partially covered": "🟡",
    "Completed": "✅",
    "Not covered": "🚫",
}


def post_confirmation_markup(confirmation_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Prepare Lesson", callback_data=f"lesson:prepare:{confirmation_id}")],
        [InlineKeyboardButton("⏭️ Skip for Now", callback_data="lesson:skip")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
    ])


def _now(timezone):
    return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")


def _short(text, limit=34):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _outline_review(import_id, teacher_id, page):
    staged = database.get_pending_syllabus_import(import_id, teacher_id)
    if not staged:
        return None
    outline = staged["outline"]
    flat = flatten_outline(outline)
    page_count = max(1, (len(flat) + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
    page = min(max(0, int(page)), page_count - 1)
    start = page * REVIEW_PAGE_SIZE
    visible = flat[start:start + REVIEW_PAGE_SIZE]
    title = outline.get("course_title") or "(course title not detected)"
    text_lines = [
        "📚 Review Syllabus Import",
        f"File: {staged['filename']}",
        f"Course: {title}",
        f"Outline page {page + 1}/{page_count}",
        "",
    ]
    rows = [
        [InlineKeyboardButton("✏️ Set Course Title", callback_data=f"syllabus:edit:{import_id}:course:{page}")],
        [InlineKeyboardButton("➕ Add Chapter", callback_data=f"syllabus:addchapter:{import_id}:{page}")],
    ]
    if not visible:
        text_lines.append("No chapters detected yet. Add one below, or cancel this import.")
    for item in visible:
        if item["kind"] == "chapter":
            text_lines.append(f"📖 {item['title']}")
            path = outline_path_text(item["path"])
            rows.append([
                InlineKeyboardButton(f"✏️ {_short(item['title'])}", callback_data=f"syllabus:edit:{import_id}:{path}:{page}"),
                InlineKeyboardButton("➕ Topic", callback_data=f"syllabus:addtopic:{import_id}:{item['path'][0]}:{page}"),
                InlineKeyboardButton("🗑️", callback_data=f"syllabus:remove:{import_id}:{path}:{page}"),
            ])
        else:
            prefix = "　" * (item["depth"] + 1) + "• "
            text_lines.append(f"{prefix}{item['title']}")
            path = outline_path_text(item["path"])
            rows.append([
                InlineKeyboardButton(f"✏️ {_short(item['title'])}", callback_data=f"syllabus:edit:{import_id}:{path}:{page}"),
                InlineKeyboardButton("➕ Subtopic", callback_data=f"syllabus:addchild:{import_id}:{path}:{page}"),
                InlineKeyboardButton("🗑️", callback_data=f"syllabus:remove:{import_id}:{path}:{page}"),
            ])
    for warning in outline.get("warnings", []):
        text_lines.extend(["", f"⚠️ {warning}"])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"syllabus:review:{import_id}:{page - 1}"))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"syllabus:review:{import_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.extend([
        [InlineKeyboardButton("✅ Confirm Syllabus", callback_data=f"syllabus:confirm:{import_id}")],
        [InlineKeyboardButton("🚫 Cancel Import", callback_data=f"syllabus:cancel:{import_id}")],
    ])
    return "\n".join(text_lines), InlineKeyboardMarkup(rows), page


async def _show_review(target, import_id, teacher_id, page=0, edit=False):
    view = _outline_review(import_id, teacher_id, page)
    if not view:
        text = "⚠️ This syllabus review has expired. Upload the document again."
        markup = None
    else:
        text, markup, _ = view
    if edit:
        await target.edit_message_text(text, reply_markup=markup)
    else:
        await target.reply_text(text, reply_markup=markup)


async def handle_document_upload(update, context, teacher_id, timezone, max_upload_bytes):
    message = update.effective_message
    sender = update.effective_user
    chat = update.effective_chat
    if not message or not sender or not message.document:
        return
    if sender.id != teacher_id:
        await message.reply_text("🔒 Only the authorized teacher can import syllabuses.")
        return
    if not chat or chat.type != "private":
        await message.reply_text("💬 Please upload teaching materials in a private chat with ClassFlow.")
        return
    material_target = context.user_data.get("course_material_upload")
    if material_target:
        if chat.id != material_target.get("chat_id"):
            await message.reply_text("💬 Upload this file in the same private chat where you selected the course chapter.")
            return
        document = message.document
        if document.file_size is None or document.file_size > max_upload_bytes:
            await message.reply_text(f"⚠️ Upload a document under {max_upload_bytes // (1024 * 1024)} MB.")
            return
        filename = Path((document.file_name or "teaching-material").replace("\\", "/")).name
        filename = "".join(char for char in filename if char.isprintable() and char not in "\r\n")[:180] or "teaching-material"
        mime_type = (document.mime_type or "application/octet-stream").split(";", 1)[0].strip().lower()
        try:
            saved = database.attach_syllabus_chapter_material(
                material_target["syllabus_id"], material_target["chapter_id"], teacher_id,
                document.file_id, filename, mime_type, _now(timezone),
            )
            if not saved:
                raise ValueError("That chapter is no longer available in this course.")
        except ValueError as exc:
            context.user_data.pop("course_material_upload", None)
            await message.reply_text(f"⚠️ {exc}")
            return
        context.user_data.pop("course_material_upload", None)
        text, markup = _course_chapter_view(
            material_target["syllabus_id"], material_target["chapter_id"], teacher_id
        )
        await message.reply_text(f"✅ {filename} is attached to this course chapter.")
        await message.reply_text(text, reply_markup=markup)
        return
    context.user_data.pop("syllabus_edit", None)
    context.user_data.pop("lesson_note", None)

    document = message.document
    filename = Path((document.file_name or "").replace("\\", "/")).name
    filename = "".join(character for character in filename if character.isprintable())[:180]
    if not filename:
        filename = "uploaded-syllabus"
    suffix = Path(filename).suffix.lower()
    if suffix not in UPLOAD_MIME_TYPES:
        await message.reply_text("📎 I can read text-based PDF, DOCX, and PPTX files. Please upload one of those formats.")
        return
    mime_type = (document.mime_type or "").split(";", 1)[0].strip().lower()
    if mime_type and mime_type not in UPLOAD_MIME_TYPES[suffix]:
        await message.reply_text("⚠️ The file type does not match its extension. Please upload a valid PDF, DOCX, or PPTX.")
        return
    if document.file_size is not None and document.file_size > max_upload_bytes:
        await message.reply_text(f"⚠️ This file is larger than the {max_upload_bytes // (1024 * 1024)} MB upload limit.")
        return

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="classflow_syllabus_", suffix=suffix, delete=False) as stream:
            temporary_path = Path(stream.name)
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=temporary_path)
        if temporary_path.stat().st_size > max_upload_bytes:
            await message.reply_text(f"⚠️ This file is larger than the {max_upload_bytes // (1024 * 1024)} MB upload limit.")
            return
        outline = extract_syllabus(temporary_path, suffix)
        def canonical_topics(topics):
            return [{
                "title": " ".join(item["title"].split()).casefold(),
                "children": canonical_topics(item.get("children", [])),
            } for item in topics]
        fingerprint = {
            "course_title": " ".join(outline.get("course_title", "").split()).casefold(),
            "chapters": [{
                "title": " ".join(chapter["title"].split()).casefold(),
                "topics": canonical_topics(chapter.get("topics", [])),
            } for chapter in outline.get("chapters", [])],
        }
        digest = hashlib.sha256(json.dumps(
            fingerprint, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        result = database.create_pending_syllabus_import(
            teacher_id, filename, digest, outline, _now(timezone)
        )
        if result["status"] == "already_imported":
            await message.reply_text(
                f"ℹ️ This document is already saved as {result['syllabus']['title']}. "
                "I didn't create a duplicate syllabus."
            )
            return
        import_id = result["import"]["id"]
        if result["status"] == "already_pending":
            await message.reply_text("ℹ️ This file already has a pending review. Reopening it:")
        await _show_review(message, import_id, teacher_id)
    except SyllabusDocumentError as exc:
        await message.reply_text(f"⚠️ {exc}")
    except TelegramError as exc:
        logging.getLogger(__name__).warning("Syllabus file download failed (%s).", type(exc).__name__)
        await message.reply_text("⚠️ Telegram couldn't download that file. Please try uploading it again.")
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("Syllabus upload processing failed (%s).", type(exc).__name__)
        await message.reply_text("⚠️ I couldn't process that file safely. Please check it and try again.")
    except Exception as exc:
        logging.getLogger(__name__).warning("Syllabus upload failed (%s).", type(exc).__name__)
        await message.reply_text("⚠️ I couldn't finish importing that file. Please try again later.")
    finally:
        if temporary_path:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning("Temporary syllabus file cleanup failed.")


def _outline_edit(outline, action, path, value=None):
    text = " ".join(str(value or "").split())[:240]
    if not text:
        raise ValueError("Please enter a non-empty title.")
    if action == "course":
        outline["course_title"] = text[:160]
        return
    if action == "chapter":
        outline.setdefault("chapters", []).append({"title": text, "topics": []})
        return
    if action == "topic":
        chapter = find_outline_node(outline, path)
        if not chapter or "topics" not in chapter:
            raise ValueError("That chapter is no longer in the draft.")
        chapter.setdefault("topics", []).append({"title": text, "children": []})
        return
    if action == "child":
        parent = find_outline_node(outline, path)
        if not parent or "children" not in parent:
            raise ValueError("That topic is no longer in the draft.")
        parent.setdefault("children", []).append({"title": text, "children": []})
        return
    node = find_outline_node(outline, path)
    if not node:
        raise ValueError("That outline item is no longer in the draft.")
    node["title"] = text


def _lesson_classes_view(teacher_id, timezone, page):
    page = max(0, int(page))
    items = database.get_teacher_lessons(teacher_id, datetime.now(ZoneInfo(timezone)).date().isoformat(),
                                         offset=page * CLASS_PAGE_SIZE, limit=CLASS_PAGE_SIZE + 1)
    has_next = len(items) > CLASS_PAGE_SIZE
    items = items[:CLASS_PAGE_SIZE]
    rows = []
    lines = ["📖 Lesson Tracker", "Choose a confirmed class to plan or review lesson topics.", ""]
    if not items:
        lines.append("No recent or upcoming confirmed classes are available yet.")
    for item in items:
        sections = ", ".join(item["sections"].split(","))
        lines.append(
            f"📘 {item['course']} · {item['class_date']} · Section(s) {sections} · "
            f"{item['planned_topic_count']} topic(s) planned"
        )
        rows.append([InlineKeyboardButton(
            f"📅 {item['class_date']} · Sec {sections} · {_short(item['course'], 24)}",
            callback_data=f"lesson:class:{item['id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"lesson:classes:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"lesson:classes:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.extend([
        [InlineKeyboardButton("📚 Courses & Lessons", callback_data="lesson:library:0")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _class_view(confirmation_id, teacher_id, timezone):
    item = database.get_lesson_class(confirmation_id, teacher_id)
    if not item:
        return "⚠️ That class is unavailable or was cancelled.", InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 Lessons", callback_data="lesson:classes:0")],
        ])
    sections = ", ".join(item["sections"].split(","))
    syllabuses = database.get_class_syllabuses(confirmation_id, teacher_id)
    if not syllabuses:
        default_syllabus_id = database.get_course_syllabus_default(item["course"], teacher_id)
        if not default_syllabus_id:
            available_syllabuses = database.list_syllabuses(teacher_id)
            if len(available_syllabuses) == 1:
                default_syllabus_id = available_syllabuses[0]["id"]
        if default_syllabus_id:
            database.link_syllabus_to_class(confirmation_id, default_syllabus_id, teacher_id, _now(timezone))
            syllabuses = database.get_class_syllabuses(confirmation_id, teacher_id)
    assignments = database.get_class_topic_assignments(confirmation_id, teacher_id)
    rows = []
    for syllabus in syllabuses:
        rows.append([InlineKeyboardButton(
            f"📖 Open { _short(syllabus['title'], 30) }",
            callback_data=f"lesson:topics:{confirmation_id}:{syllabus['id']}:0:0:0"
        )])
    rows.extend([
        [InlineKeyboardButton("➕ Choose a Syllabus", callback_data=f"lesson:choose:{confirmation_id}:0")],
        [InlineKeyboardButton("📋 Update Teaching Progress", callback_data=f"lesson:progressclass:{confirmation_id}:0")],
        [InlineKeyboardButton("⬅️ Back to Classes", callback_data="lesson:classes:0")],
    ])
    text = (
        f"📘 {item['course']}\n🗓️ {item['day']} · {item['class_date']}\n"
        f"🕒 {item['start']}–{item['end']}\n👥 Section(s): {sections}\n"
        f"📝 Planned topic assignments: {len(assignments)}"
    )
    return text, InlineKeyboardMarkup(rows)


def _syllabus_choices(confirmation_id, teacher_id, page):
    all_items = database.list_syllabuses(teacher_id)
    page = max(0, int(page))
    start = page * CLASS_PAGE_SIZE
    items = all_items[start:start + CLASS_PAGE_SIZE]
    lines = ["📚 Choose the syllabus for this class:"]
    rows = []
    if not all_items:
        lines.append("No active syllabuses yet. Upload a PDF, DOCX, or PPTX in this private chat first.")
    for item in items:
        rows.append([InlineKeyboardButton(
            f"📘 { _short(item['title'], 38) } · {item['topic_count']} topics",
            callback_data=f"lesson:chooseitem:{confirmation_id}:{item['id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"lesson:choose:{confirmation_id}:{page - 1}"))
    if start + CLASS_PAGE_SIZE < len(all_items):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"lesson:choose:{confirmation_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Back to Class", callback_data=f"lesson:class:{confirmation_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _topic_tree(rows):
    by_id = {item["id"]: {**item, "children": []} for item in rows}
    roots = []
    for item in rows:
        node = by_id[item["id"]]
        parent = by_id.get(item["parent_topic_id"])
        (parent["children"] if parent else roots).append(node)
    return roots


def _flatten_topics(nodes, depth=0):
    result = []
    for node in nodes:
        result.append((node, depth))
        result.extend(_flatten_topics(node["children"], depth + 1))
    return result


def _topic_selector(confirmation_id, syllabus_id, teacher_id, chapter_page, expanded_chapter_id, topic_page):
    syllabus = database.get_syllabus(syllabus_id, teacher_id)
    linked = database.get_class_syllabuses(confirmation_id, teacher_id)
    lesson = database.get_lesson_class(confirmation_id, teacher_id)
    if not syllabus or not lesson or not any(item["id"] == int(syllabus_id) for item in linked):
        return "⚠️ That syllabus is not linked to this class.", InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Class", callback_data=f"lesson:class:{confirmation_id}")]
        ])
    chapters = database.get_syllabus_chapters(syllabus_id, teacher_id)
    chapter_page = max(0, int(chapter_page))
    chapter_count = max(1, (len(chapters) + 5) // 6)
    chapter_page = min(chapter_page, chapter_count - 1)
    page_chapters = chapters[chapter_page * 6:(chapter_page + 1) * 6]
    assigned = database.get_class_syllabus_topics(confirmation_id, syllabus_id, teacher_id)
    by_chapter = {}
    for row in assigned:
        by_chapter.setdefault(row["chapter_id"], []).append(row)
    selected_count = sum(bool(row["status"]) for row in assigned)
    lines = [
        f"📘 {syllabus['title']} · {lesson['class_date']}",
        f"📝 Selected topics: {selected_count}",
        "Tap a chapter to expand it, then tap a topic to select or deselect it.",
        "",
    ]
    buttons = []
    for chapter in page_chapters:
        is_open = int(expanded_chapter_id or 0) == chapter["id"]
        icon = "📂" if is_open else "📁"
        lines.append(f"{icon} {chapter['title']}")
        if chapter.get("material_file_id"):
            lines.append(f"　📎 Material: {chapter['material_filename']}")
        buttons.append([InlineKeyboardButton(
            f"{icon} {_short(chapter['title'], 42)}",
            callback_data=(f"lesson:topics:{confirmation_id}:{syllabus_id}:0:{chapter_page}:0" if is_open
                           else f"lesson:topics:{confirmation_id}:{syllabus_id}:{chapter['id']}:{chapter_page}:0")
        )])
        if not is_open:
            continue
        if chapter.get("material_file_id"):
            buttons.append([InlineKeyboardButton(
                "📥 Send Chapter Material",
                callback_data=f"lesson:sendmaterial:{syllabus_id}:{chapter['id']}"
            )])
        tree = _topic_tree(by_chapter.get(chapter["id"], []))
        flat = _flatten_topics(tree)
        max_topic_page = max(0, (len(flat) - 1) // TOPIC_PAGE_SIZE)
        topic_page = min(max(0, int(topic_page)), max_topic_page)
        visible = flat[topic_page * TOPIC_PAGE_SIZE:(topic_page + 1) * TOPIC_PAGE_SIZE]
        leaves = [node for node, _ in flat if not node["children"]]
        selected_leaves = sum(bool(node["status"]) for node in leaves)
        if leaves:
            target = "0" if selected_leaves == len(leaves) else "1"
            label = "🧹 Clear Chapter" if target == "0" else "✅ Plan Entire Chapter"
            buttons.append([InlineKeyboardButton(
                label, callback_data=f"lesson:all:{confirmation_id}:{syllabus_id}:{chapter['id']}:{target}:{chapter_page}:{topic_page}"
            )])
        if not visible:
            lines.append("　No topics are stored in this chapter yet.")
        for node, depth in visible:
            status_icon = _STATUS_ICONS.get(node["status"] or "", "⬜")
            prefix = "　" * (depth + 1)
            lines.append(f"{prefix}{status_icon} {node['title']}")
            buttons.append([InlineKeyboardButton(
                f"{status_icon} {_short(prefix + node['title'], 46)}",
                callback_data=(f"lesson:toggle:{confirmation_id}:{syllabus_id}:{node['id']}:{chapter['id']}:{chapter_page}:{topic_page}")
            )])
        if max_topic_page > 0:
            nav_topics = []
            if topic_page > 0:
                nav_topics.append(InlineKeyboardButton(
                    "⬅️ Topics", callback_data=f"lesson:topics:{confirmation_id}:{syllabus_id}:{chapter['id']}:{chapter_page}:{topic_page - 1}"
                ))
            if topic_page < max_topic_page:
                nav_topics.append(InlineKeyboardButton(
                    "More Topics ➡️", callback_data=f"lesson:topics:{confirmation_id}:{syllabus_id}:{chapter['id']}:{chapter_page}:{topic_page + 1}"
                ))
            buttons.append(nav_topics)
    nav_chapters = []
    if chapter_page > 0:
        nav_chapters.append(InlineKeyboardButton(
            "⬅️ Chapters", callback_data=f"lesson:topics:{confirmation_id}:{syllabus_id}:0:{chapter_page - 1}:0"
        ))
    if chapter_page + 1 < chapter_count:
        nav_chapters.append(InlineKeyboardButton(
            "More Chapters ➡️", callback_data=f"lesson:topics:{confirmation_id}:{syllabus_id}:0:{chapter_page + 1}:0"
        ))
    if nav_chapters:
        buttons.append(nav_chapters)
    buttons.extend([
        [InlineKeyboardButton("📋 Teaching Progress", callback_data=f"lesson:progressclass:{confirmation_id}:0")],
        [InlineKeyboardButton("📊 Course Progress", callback_data=f"lesson:progress:{syllabus_id}")],
        [InlineKeyboardButton("⬅️ Back to Class", callback_data=f"lesson:class:{confirmation_id}")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _class_progress_view(confirmation_id, teacher_id, page):
    lesson = database.get_lesson_class(confirmation_id, teacher_id)
    if not lesson:
        return "⚠️ That class is unavailable or was cancelled.", InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 Lesson Tracker", callback_data="lesson:classes:0")]
        ])
    assignments = database.get_class_topic_assignments(confirmation_id, teacher_id)
    page = max(0, int(page))
    page_count = max(1, (len(assignments) + TOPIC_PAGE_SIZE - 1) // TOPIC_PAGE_SIZE)
    page = min(page, page_count - 1)
    visible = assignments[page * TOPIC_PAGE_SIZE:(page + 1) * TOPIC_PAGE_SIZE]
    lines = [f"📋 Teaching progress · {lesson['course']} · {lesson['class_date']}", ""]
    rows = []
    if not assignments:
        lines.append("No topics selected for this class yet. Open the syllabus topic selector to plan them.")
    for item in visible:
        label = (
            f"{_short(item['syllabus_title'], 64)} · {_short(item['chapter_title'], 72)} · "
            f"{_short(item['topic_title'], 120)}"
        )
        lines.append(f"{_STATUS_ICONS[item['status']]} {label}")
        if item["note"]:
            lines.append(f"　💬 {_short(item['note'], 120)}")
        rows.append([InlineKeyboardButton(
            f"{_STATUS_ICONS[item['status']]} {_short(item['topic_title'], 42)}",
            callback_data=f"lesson:status:{confirmation_id}:{item['topic_id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"lesson:progressclass:{confirmation_id}:{page - 1}"))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"lesson:progressclass:{confirmation_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.extend([
        [InlineKeyboardButton("📖 Back to Class", callback_data=f"lesson:class:{confirmation_id}")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _syllabus_library(teacher_id, page):
    items = database.list_syllabuses(teacher_id)
    page = max(0, int(page))
    start = page * CLASS_PAGE_SIZE
    visible = items[start:start + CLASS_PAGE_SIZE]
    lines = [
        "📚 Courses & Lessons",
        "Open a course to manage chapters, files, topics, and progress.",
        "Send a complete syllabus document to import it, or create a course and add chapters one at a time.",
        "",
    ]
    rows = []
    if not items:
        lines.append("No courses yet. Send a syllabus PDF, DOCX, or PPTX here to set one up.")
    for item in visible:
        rows.append([InlineKeyboardButton(
            f"📘 {_short(item['title'], 38)} · {item['topic_count']} topics",
            callback_data=f"lesson:course:{item['id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"lesson:library:{page - 1}"))
    if start + CLASS_PAGE_SIZE < len(items):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"lesson:library:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.extend([
        [InlineKeyboardButton("➕ Create a Course", callback_data="lesson:newcourse")],
        [InlineKeyboardButton("🗓️ Upcoming & Recent Lessons", callback_data="lesson:classes:0")],
        [InlineKeyboardButton("📂 Existing Chapter Files", callback_data="chapter:list:0")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _course_view(syllabus_id, teacher_id):
    syllabus = database.get_syllabus(syllabus_id, teacher_id)
    if not syllabus:
        return "⚠️ That course could not be found.", InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Courses", callback_data="lesson:library:0")],
        ])
    chapters = database.get_syllabus_chapters(syllabus_id, teacher_id)
    lines = [f"📘 {syllabus['title']}", "Manage course chapters, materials, and topics here.", ""]
    rows = []
    if not chapters:
        lines.append("No chapters yet. Add a chapter below or import a syllabus document.")
    for chapter in chapters:
        material = "📎" if chapter["material_file_id"] else "📄"
        lines.append(f"{material} {chapter['title']} · {chapter['topic_count']} topics")
        rows.append([InlineKeyboardButton(
            f"{material} {_short(chapter['title'], 38)} · {chapter['topic_count']} topics",
            callback_data=f"lesson:chapter:{syllabus_id}:{chapter['id']}"
        )])
    rows.extend([
        [InlineKeyboardButton("➕ Add Chapter", callback_data=f"lesson:addchapter:{syllabus_id}")],
        [InlineKeyboardButton("📊 Course Progress", callback_data=f"lesson:progress:{syllabus_id}")],
        [InlineKeyboardButton("🗓️ Lessons", callback_data="lesson:classes:0")],
        [InlineKeyboardButton("⬅️ All Courses", callback_data="lesson:library:0")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _course_chapter_view(syllabus_id, chapter_id, teacher_id):
    syllabus = database.get_syllabus(syllabus_id, teacher_id)
    chapter = next((item for item in database.get_syllabus_chapters(syllabus_id, teacher_id)
                    if item["id"] == int(chapter_id)), None)
    if not syllabus or not chapter:
        return "⚠️ That chapter could not be found in this course.", InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Course", callback_data=f"lesson:course:{syllabus_id}")],
        ])
    rows = [[InlineKeyboardButton("➕ Add Topics", callback_data=f"lesson:addtopics:{syllabus_id}:{chapter_id}")],
            [InlineKeyboardButton("✏️ Rename Chapter", callback_data=f"lesson:renamechapter:{syllabus_id}:{chapter_id}")]]
    if chapter["material_file_id"]:
        text = (f"📘 {syllabus['title']}\n📖 {chapter['title']}\n\n"
                f"📎 Material: {chapter['material_filename']}\n"
                f"🗂️ Type: {chapter['material_mime_type']}\n\n")
        rows.extend([
            [InlineKeyboardButton("📥 Send Material", callback_data=f"lesson:sendmaterial:{syllabus_id}:{chapter_id}")],
            [InlineKeyboardButton("♻️ Replace Material", callback_data=f"lesson:uploadmaterial:{syllabus_id}:{chapter_id}")],
        ])
    else:
        text = f"📘 {syllabus['title']}\n📖 {chapter['title']}\n\n📎 No material attached yet.\n\n"
        rows.append([InlineKeyboardButton("📎 Attach Material", callback_data=f"lesson:uploadmaterial:{syllabus_id}:{chapter_id}")])
    rows.append([InlineKeyboardButton("🔗 Use a Previously Uploaded File", callback_data=f"lesson:legacyfiles:{syllabus_id}:{chapter_id}:0")])
    topics = database.get_syllabus_chapter_topics(syllabus_id, chapter_id, teacher_id)
    text += f"📝 Topics ({len(topics)}):\n"
    text += "\n".join(f"• {item['title']}" for item in topics) if topics else "No topics yet. Add them below, one per line."
    for topic in topics:
        rows.append([InlineKeyboardButton(
            f"✏️ {_short(topic['title'], 42)}",
            callback_data=f"lesson:renametopic:{syllabus_id}:{chapter_id}:{topic['id']}"
        )])
    rows.extend([
        [InlineKeyboardButton("⬅️ Back to Course", callback_data=f"lesson:course:{syllabus_id}")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
    ])
    return text, InlineKeyboardMarkup(rows)


def _legacy_materials_view(syllabus_id, chapter_id, teacher_id, page):
    result = database.list_teacher_chapters(teacher_id, limit=50, offset=0)
    items = [item for item in result["items"] if item["material_attached"]]
    page_size = 8
    page_count = max(1, (len(items) + page_size - 1) // page_size)
    page = min(max(0, int(page)), page_count - 1)
    visible = items[page * page_size:(page + 1) * page_size]
    lines = ["📂 Link a previously uploaded file", "Choose the file to attach to this course chapter.", ""]
    rows = []
    if not visible:
        lines.append("No previously uploaded chapter files were found.")
    for item in visible:
        lines.append(f"📎 {item['name']} · {item['filename']}")
        rows.append([InlineKeyboardButton(
            f"📎 {_short(item['name'], 24)} · {_short(item['filename'], 24)}",
            callback_data=f"lesson:importfile:{syllabus_id}:{chapter_id}:{item['id']}"
        )])
    nav = []
    if page:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"lesson:legacyfiles:{syllabus_id}:{chapter_id}:{page - 1}"))
    if (page + 1) * page_size < len(items):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"lesson:legacyfiles:{syllabus_id}:{chapter_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Back to Course", callback_data=f"lesson:course:{syllabus_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def handle_text_input(update, context, teacher_id, timezone):
    """Consume pending syllabus edits or lesson notes; return whether handled."""
    message = update.effective_message
    sender = update.effective_user
    if not message or not sender or sender.id != teacher_id or not message.text:
        return False
    if not update.effective_chat or update.effective_chat.type != "private":
        return False
    edit = context.user_data.get("syllabus_edit")
    course_edit = context.user_data.get("course_edit")
    note = context.user_data.get("lesson_note")
    if not edit and not course_edit and not note:
        return False
    text = message.text.strip()
    if len(text) > (4000 if course_edit and course_edit["action"] == "addtopics" else 500):
        await message.reply_text("⚠️ Keep the text within the message limit and try again.")
        return True
    try:
        if edit:
            staged = database.get_pending_syllabus_import(edit["import_id"], teacher_id)
            if not staged:
                context.user_data.pop("syllabus_edit", None)
                await message.reply_text("⚠️ That review has expired. Upload the document again.")
                return True
            path = parse_outline_path(edit.get("path", ""))
            _outline_edit(staged["outline"], edit["action"], path, text)
            database.update_pending_syllabus_import(edit["import_id"], teacher_id, staged["outline"])
            context.user_data.pop("syllabus_edit", None)
            view = _outline_review(edit["import_id"], teacher_id, edit["page"])
            if view:
                target_message = edit.get("message_id")
                chat_id = edit.get("chat_id")
                if target_message and chat_id:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=target_message,
                        text=view[0], reply_markup=view[1],
                    )
                else:
                    await message.reply_text(view[0], reply_markup=view[1])
            return True
        if course_edit:
            if course_edit["action"] == "newcourse":
                saved_course = database.create_manual_syllabus(teacher_id, text, _now(timezone))
                reply = "✅ Course created. Add its chapters and materials here."
            elif course_edit["action"] == "addchapter":
                database.add_syllabus_chapter(course_edit["syllabus_id"], teacher_id, text)
                reply = "✅ Chapter added."
            elif course_edit["action"] == "renamechapter":
                if not database.rename_syllabus_chapter(
                    course_edit["syllabus_id"], course_edit["chapter_id"], teacher_id, text
                ):
                    raise ValueError("That chapter could not be found.")
                reply = "✅ Chapter renamed."
            elif course_edit["action"] == "renametopic":
                if not database.rename_syllabus_topic(
                    course_edit["syllabus_id"], course_edit["chapter_id"],
                    course_edit["topic_id"], teacher_id, text,
                ):
                    raise ValueError("That topic could not be found.")
                reply = "✅ Topic renamed."
            else:
                count = database.add_syllabus_topics(
                    course_edit["syllabus_id"], course_edit["chapter_id"], teacher_id,
                    [line.strip(" •\t") for line in text.splitlines() if line.strip(" •\t")],
                )
                reply = f"✅ Added {count} topic(s)."
            context.user_data.pop("course_edit", None)
            if course_edit["action"] == "newcourse":
                view, markup = _course_view(saved_course["id"], teacher_id)
            elif course_edit["action"] == "addchapter":
                view, markup = _course_view(course_edit["syllabus_id"], teacher_id)
            else:
                view, markup = _course_chapter_view(
                    course_edit["syllabus_id"], course_edit["chapter_id"], teacher_id
                )
            await context.bot.edit_message_text(
                chat_id=course_edit["chat_id"], message_id=course_edit["message_id"],
                text=view, reply_markup=markup,
            )
            await message.reply_text(reply)
            return True
        if note:
            database.set_class_topic_note(
                note["confirmation_id"], note["topic_id"], teacher_id,
                text, _now(timezone),
            )
            context.user_data.pop("lesson_note", None)
            page_text, markup = _class_progress_view(note["confirmation_id"], teacher_id, note.get("page", 0))
            await context.bot.edit_message_text(
                chat_id=note["chat_id"], message_id=note["message_id"],
                text=page_text, reply_markup=markup,
            )
            await message.reply_text("💬 Note saved.")
            return True
    except (ValueError, IndexError) as exc:
        await message.reply_text(f"⚠️ {exc}")
        return True
    return False


async def handle_callback(query, context, teacher_id, timezone):
    """Handle syllabus/lesson callbacks. Returns False for other bot features."""
    data = query.data or ""
    if not (data.startswith("syllabus:") or data.startswith("lesson:")):
        return False
    if query.from_user.id != teacher_id:
        await query.edit_message_text("🔒 Teacher access only.")
        return True

    if data.startswith("syllabus:"):
        parts = data.split(":")
        action = parts[1]
        if action not in {"edit", "addchapter", "addtopic", "addchild"}:
            context.user_data.pop("syllabus_edit", None)
        if action == "review":
            await _show_review(query, int(parts[2]), teacher_id, int(parts[3]), edit=True)
            return True
        if action in {"edit", "addchapter", "addtopic", "addchild"}:
            import_id = int(parts[2])
            if action == "edit":
                path_text, page = parts[3], int(parts[4])
                edit_action = "course" if path_text == "course" else "rename"
                path = "" if path_text == "course" else path_text
                prompt = "✏️ Send the course/syllabus title:" if edit_action == "course" else "✏️ Send the replacement title:"
            elif action == "addchapter":
                page = int(parts[3]); path = ""; edit_action = "chapter"; prompt = "➕ Send the new chapter title:"
            elif action == "addtopic":
                page = int(parts[4]); path = parts[3]; edit_action = "topic"; prompt = "➕ Send the new topic title:"
            else:
                page = int(parts[4]); path = parts[3]; edit_action = "child"; prompt = "➕ Send the new subtopic title:"
            if not database.get_pending_syllabus_import(import_id, teacher_id):
                await query.edit_message_text("⚠️ This review has expired. Upload the document again.")
                return True
            context.user_data["syllabus_edit"] = {
                "import_id": import_id, "action": edit_action, "path": path, "page": page,
                "chat_id": query.message.chat_id, "message_id": query.message.message_id,
            }
            await query.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Review", callback_data=f"syllabus:review:{import_id}:{page}")]
            ]))
            return True
        if action == "remove":
            import_id, path_text, page = int(parts[2]), parts[3], int(parts[4])
            staged = database.get_pending_syllabus_import(import_id, teacher_id)
            if not staged:
                await query.edit_message_text("⚠️ This review has expired.")
                return True
            path = parse_outline_path(path_text)
            if not find_outline_node(staged["outline"], path):
                await query.edit_message_text("⚠️ That outline item is already gone.")
                return True
            remove_outline_node(staged["outline"], path)
            database.update_pending_syllabus_import(import_id, teacher_id, staged["outline"])
            await _show_review(query, import_id, teacher_id, page, edit=True)
            return True
        if action == "cancel":
            import_id = int(parts[2])
            database.cancel_pending_syllabus_import(import_id, teacher_id)
            edit = context.user_data.get("syllabus_edit", {})
            if edit.get("import_id") == import_id:
                context.user_data.pop("syllabus_edit", None)
            await query.edit_message_text("🚫 Syllabus import cancelled. The pending outline was removed.")
            return True
        if action == "confirm":
            import_id = int(parts[2])
            try:
                saved = database.confirm_syllabus_import(import_id, teacher_id, _now(timezone))
            except ValueError as exc:
                view = _outline_review(import_id, teacher_id, 0)
                await query.edit_message_text(
                    f"⚠️ {exc}",
                    reply_markup=view[1] if view else None,
                )
                return True
            progress = database.get_syllabus_progress(saved["id"], teacher_id)
            await query.edit_message_text(
                f"✅ Syllabus saved: {saved['title']}\n📚 {progress['total_topics']} topic(s) are ready.\n"
                "Use the Lesson Tracker to select what you plan to teach for each class.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📖 Open Lesson Tracker", callback_data="lesson:classes:0")],
                    [InlineKeyboardButton("📊 Syllabus Progress", callback_data=f"lesson:progress:{saved['id']}")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
                ]),
            )
            return True
        return True

    parts = data.split(":")
    action = parts[1]
    context.user_data.pop("syllabus_edit", None)
    context.user_data.pop("course_edit", None)
    context.user_data.pop("course_material_upload", None)
    if action != "note":
        context.user_data.pop("lesson_note", None)
    if action == "skip":
        await query.edit_message_text(
            "👌 Lesson preparation skipped for now. You can open the Lesson Tracker from the main menu later.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📖 Lesson Tracker", callback_data="lesson:classes:0")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
            ]),
        )
        return True
    if action == "prepare":
        confirmation_id = int(parts[2])
        confirmed = database.get_confirmation_by_id(confirmation_id)
        if confirmed:
            timezone_info = ZoneInfo(timezone)
            class_end = datetime.strptime(
                f"{confirmed['class_date']} {confirmed['end']}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=timezone_info)
            if class_end <= datetime.now(timezone_info):
                await query.edit_message_text(
                    f"⏰ This class ended on {confirmed['class_date']} at {confirmed['end']}. "
                    "Lesson preparation is closed for a class that has already passed. "
                    "You can still use the Lesson Tracker to record what was covered.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📖 Open Lesson Tracker", callback_data="lesson:classes:0")],
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
                    ]),
                )
                return True
        if not database.get_lesson_class(confirmation_id, teacher_id):
            await query.edit_message_text(
                "⚠️ This class is marked as cancelled, so lesson preparation is unavailable.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗓️ Makeup Obligations", callback_data="makeup:list")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
                ]),
            )
            return True
        linked_syllabuses = database.get_class_syllabuses(confirmation_id, teacher_id)
        syllabus_id = linked_syllabuses[0]["id"] if linked_syllabuses else None
        if not syllabus_id:
            syllabus_id = database.get_course_syllabus_default(confirmed["course"], teacher_id) if confirmed else None
        available_syllabuses = database.list_syllabuses(teacher_id)
        if not syllabus_id and len(available_syllabuses) == 1:
            syllabus_id = available_syllabuses[0]["id"]
        if syllabus_id:
            database.link_syllabus_to_class(confirmation_id, syllabus_id, teacher_id, _now(timezone))
            text, markup = _topic_selector(confirmation_id, syllabus_id, teacher_id, 0, 0, 0)
        else:
            text, markup = _syllabus_choices(confirmation_id, teacher_id, 0)
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "classes":
        text, markup = _lesson_classes_view(teacher_id, timezone, int(parts[2]))
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "class":
        text, markup = _class_view(int(parts[2]), teacher_id, timezone)
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "choose":
        text, markup = _syllabus_choices(int(parts[2]), teacher_id, int(parts[3]))
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "chooseitem":
        confirmation_id, syllabus_id = int(parts[2]), int(parts[3])
        if not database.link_syllabus_to_class(confirmation_id, syllabus_id, teacher_id, _now(timezone)):
            await query.edit_message_text("⚠️ The class or syllabus could not be found.")
            return True
        text, markup = _topic_selector(confirmation_id, syllabus_id, teacher_id, 0, 0, 0)
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "topics":
        confirmation_id, syllabus_id = int(parts[2]), int(parts[3])
        expanded_id, chapter_page, topic_page = int(parts[4]), int(parts[5]), int(parts[6])
        text, markup = _topic_selector(
            confirmation_id, syllabus_id, teacher_id, chapter_page, expanded_id, topic_page
        )
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "toggle":
        confirmation_id, syllabus_id, topic_id = map(int, parts[2:5])
        chapter_id, chapter_page, topic_page = map(int, parts[5:8])
        database.toggle_class_topic(confirmation_id, syllabus_id, topic_id, teacher_id, _now(timezone))
        text, markup = _topic_selector(
            confirmation_id, syllabus_id, teacher_id, chapter_page, chapter_id, topic_page
        )
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "all":
        confirmation_id, syllabus_id, chapter_id = map(int, parts[2:5])
        select, chapter_page, topic_page = parts[5] == "1", int(parts[6]), int(parts[7])
        database.set_chapter_topics_selected(
            confirmation_id, syllabus_id, chapter_id, teacher_id, select, _now(timezone)
        )
        text, markup = _topic_selector(
            confirmation_id, syllabus_id, teacher_id, chapter_page, chapter_id, topic_page
        )
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "progressclass":
        confirmation_id, page = int(parts[2]), int(parts[3])
        text, markup = _class_progress_view(confirmation_id, teacher_id, page)
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "status":
        confirmation_id, topic_id = int(parts[2]), int(parts[3])
        assignment = next((item for item in database.get_class_topic_assignments(confirmation_id, teacher_id)
                          if item["topic_id"] == topic_id), None)
        if not assignment:
            await query.edit_message_text("⚠️ That topic is no longer assigned to this class.")
            return True
        rows = [[InlineKeyboardButton(
            f"{_STATUS_ICONS[status]} {status}",
            callback_data=f"lesson:set:{confirmation_id}:{topic_id}:{code}"
        )] for code, status in _STATUS_CODES.items()]
        rows.extend([
            [InlineKeyboardButton("💬 Add or Edit Note", callback_data=f"lesson:note:{confirmation_id}:{topic_id}")],
            [InlineKeyboardButton("🧹 Clear Note", callback_data=f"lesson:clearnote:{confirmation_id}:{topic_id}")],
            [InlineKeyboardButton("⬅️ Back to Progress", callback_data=f"lesson:progressclass:{confirmation_id}:0")],
        ])
        text = (
            f"📘 {assignment['syllabus_title']} · {assignment['chapter_title']}\n"
            f"Topic: {assignment['topic_title']}\nCurrent status: {_STATUS_ICONS[assignment['status']]} {assignment['status']}\n"
            f"Note: {assignment['note'] or 'None'}\n\nChoose teaching progress:"
        )
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
        return True
    if action == "set":
        confirmation_id, topic_id, code = int(parts[2]), int(parts[3]), parts[4]
        status = _STATUS_CODES.get(code)
        if not status:
            await query.edit_message_text("⚠️ Unknown progress status. Reopen the class and try again.")
            return True
        database.set_class_topic_status(confirmation_id, topic_id, teacher_id, status, _now(timezone))
        text, markup = _class_progress_view(confirmation_id, teacher_id, 0)
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "note":
        confirmation_id, topic_id = int(parts[2]), int(parts[3])
        context.user_data["lesson_note"] = {
            "confirmation_id": confirmation_id, "topic_id": topic_id,
            "chat_id": query.message.chat_id, "message_id": query.message.message_id, "page": 0,
        }
        await query.edit_message_text(
            "💬 Send a short note about what was covered or what remains (up to 500 characters).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Progress", callback_data=f"lesson:progressclass:{confirmation_id}:0")]
            ])
        )
        return True
    if action == "clearnote":
        confirmation_id, topic_id = int(parts[2]), int(parts[3])
        database.set_class_topic_note(confirmation_id, topic_id, teacher_id, "", _now(timezone))
        text, markup = _class_progress_view(confirmation_id, teacher_id, 0)
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "library":
        text, markup = _syllabus_library(teacher_id, int(parts[2]))
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "course":
        text, markup = _course_view(int(parts[2]), teacher_id)
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "chapter":
        text, markup = _course_chapter_view(int(parts[2]), int(parts[3]), teacher_id)
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action in {"newcourse", "addchapter", "addtopics", "renamechapter", "renametopic"}:
        if action == "newcourse":
            syllabus_id = None; chapter_id = None
            prompt = "📘 Enter the course name (for example: System Analysis and Design):"
        elif action == "addchapter":
            syllabus_id = int(parts[2]); chapter_id = None
            prompt = "➕ Send the chapter name:"
        elif action == "addtopics":
            syllabus_id, chapter_id = int(parts[2]), int(parts[3])
            prompt = "📝 Send the topics for this chapter, one topic per line. You can paste several at once."
        elif action == "renamechapter":
            syllabus_id, chapter_id = int(parts[2]), int(parts[3])
            prompt = "✏️ Send the new chapter name:"
        else:
            syllabus_id, chapter_id, topic_id = map(int, parts[2:5])
            if not any(item["id"] == topic_id for item in database.get_syllabus_chapter_topics(
                syllabus_id, chapter_id, teacher_id
            )):
                await query.edit_message_text("⚠️ That topic could not be found.")
                return True
            prompt = "✏️ Send the new topic name:"
        if syllabus_id is not None and not database.get_syllabus(syllabus_id, teacher_id):
            await query.edit_message_text("⚠️ That course could not be found.")
            return True
        if chapter_id is not None and not any(
            item["id"] == chapter_id for item in database.get_syllabus_chapters(syllabus_id, teacher_id)
        ):
            await query.edit_message_text("⚠️ That chapter could not be found.")
            return True
        context.user_data["course_edit"] = {
            "action": action, "syllabus_id": syllabus_id, "chapter_id": chapter_id,
            "topic_id": topic_id if action == "renametopic" else None,
            "chat_id": query.message.chat_id, "message_id": query.message.message_id,
        }
        await query.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="lesson:library:0" if action == "newcourse"
                                  else f"lesson:course:{syllabus_id}" if action == "addchapter"
                                  else f"lesson:chapter:{syllabus_id}:{chapter_id}")],
        ]))
        return True
    if action == "uploadmaterial":
        syllabus_id, chapter_id = int(parts[2]), int(parts[3])
        if not any(item["id"] == chapter_id for item in database.get_syllabus_chapters(syllabus_id, teacher_id)):
            await query.edit_message_text("⚠️ That chapter could not be found in this course.")
            return True
        context.user_data["course_material_upload"] = {
            "syllabus_id": syllabus_id, "chapter_id": chapter_id,
            "chat_id": query.message.chat_id,
        }
        await query.edit_message_text(
            "📎 Send the material for this chapter as a document. Any document type is accepted. "
            "It will be saved with the chapter and shown beside its topics.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data=f"lesson:chapter:{syllabus_id}:{chapter_id}")],
            ]),
        )
        return True
    if action == "sendmaterial":
        syllabus_id, chapter_id = int(parts[2]), int(parts[3])
        chapter = next((item for item in database.get_syllabus_chapters(syllabus_id, teacher_id)
                        if item["id"] == chapter_id), None)
        if not chapter or not chapter["material_file_id"]:
            await query.edit_message_text("⚠️ There is no material attached to this chapter.")
            return True
        try:
            await context.bot.send_document(
                chat_id=query.message.chat_id, document=chapter["material_file_id"],
                filename=chapter["material_filename"], caption=f"📚 {chapter['title']} teaching material",
            )
        except TelegramError:
            await query.edit_message_text(
                "⚠️ Telegram couldn't retrieve that file. Upload it again to replace the attachment.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("♻️ Replace Material", callback_data=f"lesson:uploadmaterial:{syllabus_id}:{chapter_id}")],
                    [InlineKeyboardButton("⬅️ Back", callback_data=f"lesson:chapter:{syllabus_id}:{chapter_id}")],
                ]),
            )
            return True
        text, markup = _course_chapter_view(syllabus_id, chapter_id, teacher_id)
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "legacyfiles":
        syllabus_id, chapter_id, page = map(int, parts[2:5])
        text, markup = _legacy_materials_view(syllabus_id, chapter_id, teacher_id, page)
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "importfile":
        syllabus_id, chapter_id, legacy_id = map(int, parts[2:5])
        if not database.copy_teacher_chapter_material_to_syllabus(
            legacy_id, syllabus_id, chapter_id, teacher_id
        ):
            await query.edit_message_text("⚠️ That saved file could not be linked. Check that it still exists.")
            return True
        text, markup = _course_chapter_view(syllabus_id, chapter_id, teacher_id)
        await query.edit_message_text("✅ Existing file linked to the course chapter.\n\n" + text, reply_markup=markup)
        return True
    if action == "progress":
        syllabus_id = int(parts[2])
        syllabus = database.get_syllabus(syllabus_id, teacher_id)
        progress = database.get_syllabus_progress(syllabus_id, teacher_id)
        if not syllabus or not progress:
            await query.edit_message_text("⚠️ That syllabus is not available to this teacher.")
            return True
        width = 16
        filled = round(progress["percent_complete"] * width / 100)
        bar = "🟩" * filled + "⬜" * (width - filled)
        await query.edit_message_text(
            f"📊 Course Progress · {syllabus['title']}\n"
            f"📚 Unique topics: {progress['total_topics']}\n"
            f"✅ Completed: {progress['completed_topics']}\n"
            f"🟡 Partially covered: {progress['partially_covered_topics']}\n"
            f"📝 Planned: {progress['planned_topics']}\n"
            f"⏳ Remaining: {progress['remaining_topics']}\n"
            f"📈 Complete: {progress['percent_complete']:.1f}%\n{bar}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📖 Lessons", callback_data="lesson:classes:0")],
                [InlineKeyboardButton("📚 Courses & Lessons", callback_data="lesson:library:0")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="makeup:home")],
            ])
        )
        return True
    return True
