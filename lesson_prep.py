"""Safe, local-only syllabus extraction and outline editing helpers."""

from collections import Counter
import logging
from pathlib import Path
import re
import zipfile

from docx import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader


MAX_PDF_PAGES = 200
MAX_EXTRACTED_CHARACTERS = 250_000
MAX_DOCX_ZIP_ENTRIES = 2_000
MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024

_CHAPTER_RE = re.compile(
    r"^(?:chapter|unit|module|part)\s+([\w.-]+)\s*[:.)-]?\s*(.*?)\s*$", re.I
)
_NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+){0,5})[.)]?\s+(.+?)\s*$")
_PAGE_NUMBER_RE = re.compile(r"^(?:page\s+)?\d+(?:\s*(?:of|/)\s*\d+)?$", re.I)
_BULLET_RE = re.compile(r"^(?:[•●▪◦‣*-]+|\(?[a-zA-Z]\))\s*")


class SyllabusDocumentError(ValueError):
    """A user-displayable document validation or extraction failure."""


def _paragraph_heading_level(paragraph):
    style_name = getattr(getattr(paragraph, "style", None), "name", "") or ""
    match = re.match(r"heading\s+(\d+)$", style_name.strip(), re.I)
    if match:
        return int(match.group(1))
    return 0


def _iter_docx_blocks(parent):
    for child in parent.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def _read_docx(path):
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_DOCX_ZIP_ENTRIES:
                raise SyllabusDocumentError("This DOCX contains too many internal items to process safely.")
            if sum(info.file_size for info in infos) > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise SyllabusDocumentError("This DOCX expands beyond the safe processing limit.")
        document = Document(str(path))
    except SyllabusDocumentError:
        raise
    except (OSError, zipfile.BadZipFile, ValueError, KeyError) as exc:
        raise SyllabusDocumentError("I couldn't read this DOCX. Please check that it opens in Word and upload it again.") from exc

    lines = []
    for block in _iter_docx_blocks(document):
        if isinstance(block, Paragraph):
            lines.append((block.text, _paragraph_heading_level(block), block.style.name == "Title" if block.style else False))
        else:
            for row in block.rows:
                text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if text:
                    lines.append((text, 0, False))
    text_size = sum(len(line[0]) for line in lines)
    if text_size > MAX_EXTRACTED_CHARACTERS:
        raise SyllabusDocumentError("This DOCX contains more text than the safe review limit.")
    return lines, (document.core_properties.title or "").strip()


def _read_pdf(path):
    try:
        pdf_logger = logging.getLogger("pypdf")
        previous_level = pdf_logger.level
        # Malformed PDFs can cause pypdf to log snippets of the source bytes.
        # Keep parse diagnostics private and return a generic teacher-facing error.
        pdf_logger.setLevel(logging.CRITICAL + 1)
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise SyllabusDocumentError("This PDF is encrypted. Please upload an unlocked copy.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise SyllabusDocumentError(f"This PDF has more than {MAX_PDF_PAGES} pages, the current processing limit.")
        page_lines = []
        total_chars = 0
        for page in reader.pages:
            extracted = page.extract_text() or ""
            total_chars += len(extracted)
            if total_chars > MAX_EXTRACTED_CHARACTERS:
                raise SyllabusDocumentError("This PDF contains more text than the safe review limit.")
            page_lines.append([line.strip() for line in extracted.splitlines() if line.strip()])
        if not any(page_lines):
            raise SyllabusDocumentError(
                "I couldn't extract text from this PDF. It may be scanned or image-only; OCR isn't enabled yet."
            )
        repeated = Counter(line.casefold() for page in page_lines for line in set(page))
        minimum_repeats = max(2, (len(page_lines) + 1) // 2)
        lines = []
        for page in page_lines:
            for line in page:
                if _PAGE_NUMBER_RE.fullmatch(line):
                    continue
                if len(line) <= 160 and repeated[line.casefold()] >= minimum_repeats:
                    continue
                lines.append((line, 0, False))
        title = str(reader.metadata.title or "").strip() if reader.metadata else ""
        return lines, title
    except SyllabusDocumentError:
        raise
    except Exception as exc:
        # pypdf raises several parser-specific exceptions for malformed files.
        raise SyllabusDocumentError("I couldn't read this PDF. Please check that it opens correctly and upload it again.") from exc
    finally:
        if "previous_level" in locals():
            pdf_logger.setLevel(previous_level)


def _is_all_caps_heading(text):
    letters = [char for char in text if char.isalpha()]
    return (
        1 < len(text.split()) <= 10
        and len(text) <= 90
        and len(letters) >= 4
        and sum(char.isupper() for char in letters) / len(letters) >= 0.8
        and not text.endswith((".", ",", ";"))
    )


def _clean_heading(line):
    text = re.sub(r"\s+", " ", str(line or "")).strip()
    text = _BULLET_RE.sub("", text).strip()
    return text[:240]


def parse_outline(lines, document_title=""):
    """Identify likely headings, preserving order and exposing uncertain extraction."""
    course_title = str(document_title or "").strip()[:160]
    chapters = []
    current_chapter = None
    topic_stack = []
    warnings = []

    def new_chapter(title):
        nonlocal current_chapter, topic_stack
        current_chapter = {"title": title[:240], "topics": []}
        chapters.append(current_chapter)
        topic_stack = []

    def ensure_chapter():
        if current_chapter is None:
            new_chapter("Sections (chapter heading not detected)")
            if "Some headings were grouped because no chapter heading was detected." not in warnings:
                warnings.append("Some headings were grouped because no chapter heading was detected.")

    for raw_text, heading_level, is_title in lines:
        text = _clean_heading(raw_text)
        if not text or _PAGE_NUMBER_RE.fullmatch(text):
            continue
        if is_title and not course_title:
            course_title = text[:160]
            continue
        explicit_chapter = _CHAPTER_RE.match(text)
        numbered = _NUMBERED_RE.match(text)

        if explicit_chapter:
            if explicit_chapter.group(2).strip():
                new_chapter(text)
            continue
        if not chapters and not course_title and heading_level == 0 and _is_all_caps_heading(text):
            course_title = text[:160]
            warnings.append("A title-like first heading was proposed as the course title; review it.")
            continue

        if numbered:
            number = numbered.group(1)
            title = _clean_heading(numbered.group(2))
            depth = number.count(".")
            if depth == 0:
                new_chapter(text)
                continue
            ensure_chapter()
            while topic_stack and topic_stack[-1][0] >= depth:
                topic_stack.pop()
            parent = topic_stack[-1][1] if topic_stack else None
            target = parent["children"] if parent else current_chapter["topics"]
            node = {"title": text[:240], "children": []}
            target.append(node)
            topic_stack.append((depth, node))
            continue

        if heading_level == 1:
            new_chapter(text)
            continue

        if heading_level >= 2 or _is_all_caps_heading(text):
            ensure_chapter()
            depth = max(1, heading_level - 1) if heading_level else 1
            while topic_stack and topic_stack[-1][0] >= depth:
                topic_stack.pop()
            parent = topic_stack[-1][1] if topic_stack else None
            target = parent["children"] if parent else current_chapter["topics"]
            node = {"title": text, "children": []}
            target.append(node)
            topic_stack.append((depth, node))

    if chapters and not course_title:
        warnings.append("The course title was not clear in the document. Add or correct it before confirming.")
    if not chapters:
        warnings.append("No clear chapter headings were detected. Add chapters and topics manually, or cancel this import.")
    if any(not chapter["topics"] for chapter in chapters):
        warnings.append("Some chapters have no detected subtopics; review and add any missing topics.")
    if chapters:
        warnings.append("Please review the extracted outline. Headings may need correction.")
    return {"course_title": course_title, "chapters": chapters, "warnings": warnings}


def extract_syllabus(path, extension):
    extension = extension.lower()
    if extension == ".pdf":
        lines, document_title = _read_pdf(path)
    elif extension == ".docx":
        lines, document_title = _read_docx(path)
    else:
        raise SyllabusDocumentError("Upload a PDF or DOCX syllabus file.")
    if not any(str(item[0]).strip() for item in lines):
        raise SyllabusDocumentError("No readable text was found in this document.")
    return parse_outline(lines, document_title)


def flatten_outline(outline):
    result = []
    for chapter_index, chapter in enumerate(outline.get("chapters", [])):
        result.append({"kind": "chapter", "path": (chapter_index,), "title": chapter["title"]})

        def visit(topics, path, depth):
            for topic_index, topic in enumerate(topics):
                topic_path = path + (topic_index,)
                result.append({"kind": "topic", "path": topic_path, "title": topic["title"], "depth": depth})
                visit(topic.get("children", []), topic_path, depth + 1)

        visit(chapter.get("topics", []), (chapter_index,), 0)
    return result


def find_outline_node(outline, path):
    if not path:
        return None
    try:
        node = outline["chapters"][path[0]]
        for child_index in path[1:]:
            node = node["topics"][child_index] if "topics" in node else node["children"][child_index]
        return node
    except (IndexError, KeyError, TypeError):
        return None


def remove_outline_node(outline, path):
    if len(path) == 1:
        del outline["chapters"][path[0]]
        return
    parent = find_outline_node(outline, path[:-1])
    siblings = parent["topics"] if len(path) == 2 else parent["children"]
    del siblings[path[-1]]


def outline_path_text(path):
    return ".".join(str(index) for index in path)


def parse_outline_path(value):
    if not value:
        return ()
    parts = tuple(int(part) for part in value.split("."))
    if any(part < 0 for part in parts):
        raise ValueError("Invalid outline item.")
    return parts
