"""Teacher-owned chapter list and chapter-specific Telegram material uploads."""

import logging
import tempfile
from datetime import datetime
from pathlib import Path
import time
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from pypdf import PdfReader

import database


CHAPTERS_PER_PAGE = 5
MAX_CHAPTER_NAME = 80
UPLOAD_SELECTION_TTL_SECONDS = 15 * 60
MAX_DOCX_ZIP_ENTRIES = 2_000
MAX_DOCX_EXPANDED_BYTES = 50 * 1024 * 1024
MATERIAL_MIME_TYPES = {
    ".pdf": {"application/pdf", "application/octet-stream"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/octet-stream",
    },
}


def _now(timezone):
    return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")


def _safe_filename(value):
    filename = Path((str(value or "").replace("\\", "/"))).name
    filename = "".join(char for char in filename if char.isprintable() and char not in "\r\n")[:180]
    return filename or "teaching-material"


def _validate_material_file(path, suffix):
    """Validate the actual temporary document structure without retaining its bytes."""
    if suffix == ".pdf":
        with Path(path).open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise ValueError("That file is not a valid PDF. Please upload a PDF or DOCX document.")
        import logging as _logging
        pdf_logger = _logging.getLogger("pypdf")
        prior_level = pdf_logger.level
        pdf_logger.setLevel(_logging.CRITICAL + 1)
        try:
            reader = PdfReader(str(path), strict=False)
            if reader.is_encrypted or not reader.pages:
                raise ValueError("That PDF is encrypted or has no readable pages. Upload an unlocked PDF or DOCX.")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("That PDF appears to be damaged. Please upload a valid PDF or DOCX.") from exc
        finally:
            pdf_logger.setLevel(prior_level)
        return
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ZIP_ENTRIES or sum(item.file_size for item in entries) > MAX_DOCX_EXPANDED_BYTES:
                raise ValueError("That DOCX expands beyond the safe validation limit.")
            names = {item.filename for item in entries}
            if not {"[Content_Types].xml", "word/document.xml"}.issubset(names):
                raise ValueError("That file is not a valid DOCX. Please upload a PDF or DOCX document.")
            ET.fromstring(archive.read("[Content_Types].xml"))
            ET.fromstring(archive.read("word/document.xml"))
    except ValueError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError("That DOCX appears to be damaged. Please upload a valid PDF or DOCX.") from exc


def _list_view(teacher_id, page):
    requested_page = max(0, int(page))
    total = database.list_teacher_chapters(teacher_id, limit=1, offset=0)["total"]
    page_count = max(1, (total + CHAPTERS_PER_PAGE - 1) // CHAPTERS_PER_PAGE)
    page = min(requested_page, page_count - 1)
    result = database.list_teacher_chapters(teacher_id, limit=CHAPTERS_PER_PAGE + 1, offset=page * CHAPTERS_PER_PAGE)
    has_next = len(result["items"]) > CHAPTERS_PER_PAGE
    items = result["items"][:CHAPTERS_PER_PAGE]

    lines = ["📚 Chapter Management", f"Your chapters · Page {page + 1}/{page_count}", ""]
    rows = []
    for item in items:
        status = "📎 Material attached" if item["material_attached"] else "⬜ Empty"
        lines.append(f"📖 {item['name']} · {status}")
        rows.append([InlineKeyboardButton(
            f"📖 {item['name'][:38]} · {'📎' if item['material_attached'] else '⬜'}",
            callback_data=f"chapter:open:{item['id']}:{page}",
        )])
    if not items:
        lines.append("No chapters found. Add one below.")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"chapter:list:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"chapter:list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.extend([
        [InlineKeyboardButton("➕ Add Chapter", callback_data=f"chapter:add:{page}")],
        [InlineKeyboardButton("🏠 Back to Main Menu", callback_data="makeup:home")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows), page


def _chapter_view(chapter, page):
    if not chapter:
        return "⚠️ That chapter was not found in your account.", InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Chapter Management", callback_data=f"chapter:list:{page}")],
        ])
    rows = []
    if not chapter["material_attached"]:
        text = (
            f"📚 Chapter: {chapter['name']}\n\n"
            "No teaching material has been uploaded for this chapter yet.\n\n"
            "Please upload a PDF or DOCX file to add teaching material to this chapter."
        )
        rows.append([InlineKeyboardButton(
            "📎 Upload PDF/DOCX", callback_data=f"chapter:upload:{chapter['id']}:{page}"
        )])
    else:
        text = (
            f"📚 Chapter: {chapter['name']}\n\n"
            "📎 Material attached\n"
            f"📄 File: {chapter['filename']}\n"
            f"🗂️ Type: {chapter['mime_type']}\n"
            f"🕒 Uploaded: {chapter['uploaded_at']}"
        )
        rows.extend([
            [InlineKeyboardButton("📥 Send Current Material", callback_data=f"chapter:send:{chapter['id']}:{page}")],
            [InlineKeyboardButton("♻️ Replace Material", callback_data=f"chapter:replace:{chapter['id']}:{page}")],
        ])
    rows.append([InlineKeyboardButton("⬅️ Back to Chapters", callback_data=f"chapter:list:{page}")])
    return text, InlineKeyboardMarkup(rows)


async def show_chapter_list(target, teacher_id, timezone, page=0, edit=False):
    database.ensure_default_teacher_chapters(teacher_id, _now(timezone))
    text, markup, page = _list_view(teacher_id, page)
    if edit:
        await target.edit_message_text(text, reply_markup=markup)
    else:
        await target.reply_text(text, reply_markup=markup)


async def handle_chapter_callback(query, context, teacher_id, timezone):
    """Handle chapter UI callbacks and clear stale upload state when navigating away."""
    data = query.data or ""
    if not data.startswith("chapter:"):
        context.user_data.pop("chapter_upload", None)
        context.user_data.pop("chapter_add", None)
        return False
    if query.from_user.id != teacher_id:
        await query.edit_message_text("🔒 Teacher access only.")
        return True

    parts = data.split(":")
    action = parts[1]
    if action not in {"uploadcancel"}:
        context.user_data.pop("chapter_upload", None)
    if action not in {"addcancel"}:
        context.user_data.pop("chapter_add", None)

    if action == "list":
        page = int(parts[2]) if len(parts) > 2 else 0
        await show_chapter_list(query, teacher_id, timezone, page, edit=True)
        return True
    if action == "open":
        chapter_id, page = int(parts[2]), int(parts[3])
        chapter = database.get_teacher_chapter(chapter_id, teacher_id)
        text, markup = _chapter_view(chapter, page)
        await query.edit_message_text(text, reply_markup=markup)
        return True
    if action == "add":
        page = int(parts[2]) if len(parts) > 2 else 0
        context.user_data["chapter_add"] = {
            "page": page, "chat_id": query.message.chat_id, "message_id": query.message.message_id,
        }
        await query.edit_message_text(
            f"➕ Enter a chapter name (1–{MAX_CHAPTER_NAME} characters):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data=f"chapter:addcancel:{page}")],
            ]),
        )
        return True
    if action == "addcancel":
        page = int(parts[2]) if len(parts) > 2 else 0
        context.user_data.pop("chapter_add", None)
        await show_chapter_list(query, teacher_id, timezone, page, edit=True)
        return True
    if action in {"upload", "uploadcancel"}:
        chapter_id, page = int(parts[2]), int(parts[3])
        chapter = database.get_teacher_chapter(chapter_id, teacher_id)
        if not chapter:
            context.user_data.pop("chapter_upload", None)
            await query.edit_message_text("⚠️ That chapter does not belong to your account.")
            return True
        if action == "uploadcancel":
            context.user_data.pop("chapter_upload", None)
            text, markup = _chapter_view(chapter, page)
            await query.edit_message_text(text, reply_markup=markup)
            return True
        context.user_data["chapter_upload"] = {
            "chapter_id": chapter_id, "page": page,
            "chat_id": query.message.chat_id,
            "expires_at": time.time() + UPLOAD_SELECTION_TTL_SECONDS,
        }
        verb = "replace" if chapter["material_attached"] else "attach"
        await query.edit_message_text(
            f"📎 Send the PDF or DOCX document you want to {verb} to {chapter['name']}.\n\n"
            "This selection is tied only to this chapter. Use Cancel before choosing another chapter.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel Upload", callback_data=f"chapter:uploadcancel:{chapter_id}:{page}")],
            ]),
        )
        return True
    if action == "replace":
        chapter_id, page = int(parts[2]), int(parts[3])
        chapter = database.get_teacher_chapter(chapter_id, teacher_id)
        if not chapter or not chapter["material_attached"]:
            await query.edit_message_text("⚠️ That chapter has no current material to replace.")
            return True
        await query.edit_message_text(
            f"⚠️ Uploading new material will replace {chapter['filename']} for {chapter['name']}. "
            "The current material stays attached until the new upload is accepted. Continue?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Continue to Upload", callback_data=f"chapter:upload:{chapter_id}:{page}")],
                [InlineKeyboardButton("⬅️ Keep Current Material", callback_data=f"chapter:open:{chapter_id}:{page}")],
            ]),
        )
        return True
    if action == "send":
        chapter_id, page = int(parts[2]), int(parts[3])
        chapter = database.get_teacher_chapter(chapter_id, teacher_id)
        if not chapter or not chapter["material_attached"]:
            await query.edit_message_text("⚠️ This chapter has no material attached.")
            return True
        try:
            await context.bot.send_document(
                chat_id=query.message.chat_id, document=chapter["file_id"],
                filename=chapter["filename"], caption=f"📚 {chapter['name']} teaching material",
            )
            text, markup = _chapter_view(chapter, page)
            await query.edit_message_text(text, reply_markup=markup)
        except TelegramError:
            await query.edit_message_text(
                "⚠️ Telegram could not retrieve that saved file. Please upload it again to replace the attachment.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("♻️ Replace Material", callback_data=f"chapter:replace:{chapter_id}:{page}")],
                    [InlineKeyboardButton("⬅️ Back to Chapters", callback_data=f"chapter:list:{page}")],
                ]),
            )
        return True
    return True


async def handle_chapter_text(update, context, teacher_id, timezone):
    """Handle only the chapter-name prompt and cancel stray text-upload flows safely."""
    message = update.effective_message
    sender = update.effective_user
    if not message or not sender or sender.id != teacher_id:
        return False
    if not update.effective_chat or update.effective_chat.type != "private":
        return False
    upload = context.user_data.get("chapter_upload")
    if upload:
        context.user_data.pop("chapter_upload", None)
        await message.reply_text("❌ Chapter upload cancelled because text was sent. Reopen the chapter and choose Upload Material when ready.")
        return True
    pending = context.user_data.get("chapter_add")
    if not pending or not message.text:
        return False
    name = " ".join(message.text.split())
    if not name:
        await message.reply_text("⚠️ Chapter name cannot be empty. Enter a name or tap Cancel.")
        return True
    if len(name) > MAX_CHAPTER_NAME:
        await message.reply_text(f"⚠️ Chapter names must be {MAX_CHAPTER_NAME} characters or fewer. Try again or tap Cancel.")
        return True
    try:
        chapter = database.add_teacher_chapter(teacher_id, name, _now(timezone))
    except ValueError as exc:
        await message.reply_text(f"⚠️ {exc} Enter another name or tap Cancel.")
        return True
    except Exception:
        logging.getLogger(__name__).exception("Could not save a chapter name.")
        await message.reply_text("⚠️ I couldn't save that chapter because of a database problem. Please try again.")
        return True
    context.user_data.pop("chapter_add", None)
    total = database.list_teacher_chapters(teacher_id, limit=1, offset=0)["total"]
    page = max(0, (total - 1) // CHAPTERS_PER_PAGE)
    text, markup, _ = _list_view(teacher_id, page)
    chat_id, message_id = pending.get("chat_id"), pending.get("message_id")
    if chat_id and message_id:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)
    await message.reply_text(f"✅ Added {chapter['name']} as an empty chapter.")
    return True


async def handle_chapter_document(update, context, teacher_id, timezone, max_upload_bytes):
    message = update.effective_message
    sender = update.effective_user
    if not message or not sender or not message.document:
        return False
    if sender.id != teacher_id:
        await message.reply_text("🔒 Only the authorized teacher can manage chapter material.")
        return True
    pending = context.user_data.get("chapter_upload")
    if not pending:
        return False
    if time.time() > float(pending.get("expires_at", 0)):
        context.user_data.pop("chapter_upload", None)
        await message.reply_text("⌛ Chapter selection expired. Open Chapter Management and select the chapter again before uploading.")
        return True
    chat = update.effective_chat
    if not chat or chat.type != "private" or chat.id != pending.get("chat_id"):
        await message.reply_text("💬 Upload this file in the same private chat where you selected the chapter.")
        return True
    chapter = database.get_teacher_chapter(pending["chapter_id"], teacher_id)
    if not chapter:
        context.user_data.pop("chapter_upload", None)
        await message.reply_text("⚠️ That chapter is no longer available in your account. Open Chapter Management again.")
        return True
    document = message.document
    filename = _safe_filename(document.file_name)
    suffix = Path(filename).suffix.casefold()
    if suffix not in MATERIAL_MIME_TYPES:
        await message.reply_text("📎 Chapter material must be a PDF or DOCX file. Your chapter selection is still active.")
        return True
    mime_type = (document.mime_type or "").split(";", 1)[0].strip().lower()
    if mime_type and mime_type not in MATERIAL_MIME_TYPES[suffix]:
        await message.reply_text("⚠️ The file type does not match its extension. Please send a valid PDF or DOCX.")
        return True
    if document.file_size is not None and document.file_size > max_upload_bytes:
        await message.reply_text(f"⚠️ This file is larger than the {max_upload_bytes // (1024 * 1024)} MB upload limit.")
        return True
    if document.file_size is None:
        await message.reply_text("⚠️ Telegram didn't provide the file size, so I can't safely accept it. Please upload it again.")
        return True
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="classflow_chapter_", suffix=suffix, delete=False) as stream:
            temporary_path = Path(stream.name)
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=temporary_path)
        if temporary_path.stat().st_size > max_upload_bytes:
            await message.reply_text(f"⚠️ This file is larger than the {max_upload_bytes // (1024 * 1024)} MB upload limit.")
            return True
        _validate_material_file(temporary_path, suffix)
    except ValueError as exc:
        await message.reply_text(f"⚠️ {exc}")
        return True
    except TelegramError as exc:
        logging.getLogger(__name__).warning("Chapter material download failed (%s).", type(exc).__name__)
        await message.reply_text("⚠️ Telegram could not validate the uploaded file. Please try sending it again.")
        return True
    except OSError:
        logging.getLogger(__name__).warning("Temporary chapter material validation failed.")
        await message.reply_text("⚠️ I couldn't validate this file on the server. Please try again later.")
        return True
    finally:
        if temporary_path and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                logging.getLogger(__name__).warning("Could not remove temporary chapter material file.")
    # Recheck ownership at commit time. Telegram file IDs persist the attachment
    # without keeping a duplicate local copy of the teacher's document.
    try:
        expected_mime = "application/pdf" if suffix == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        stored_mime = expected_mime if mime_type in {"", "application/octet-stream"} else mime_type
        saved = database.attach_chapter_material(
            chapter["id"], teacher_id, document.file_id, document.file_unique_id,
            filename, stored_mime,
            _now(timezone),
        )
    except ValueError as exc:
        context.user_data.pop("chapter_upload", None)
        await message.reply_text(f"⚠️ {exc}")
        return True
    except Exception:
        logging.getLogger(__name__).exception("Could not attach chapter material metadata.")
        await message.reply_text("⚠️ I couldn't save the attachment metadata. No success was recorded; please try again.")
        return True
    context.user_data.pop("chapter_upload", None)
    text, markup = _chapter_view(saved, pending["page"])
    await message.reply_text(f"✅ Material attached to {saved['name']}.")
    await message.reply_text(text, reply_markup=markup)
    return True
