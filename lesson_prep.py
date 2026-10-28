"""Safe, local-only syllabus extraction and outline editing helpers."""

from collections import Counter
import logging
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET

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
MAX_PPTX_SLIDES = 300
MAX_PPTX_ZIP_ENTRIES = 4_000
MAX_PPTX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024

_PPT_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "dc": "http://purl.org/dc/elements/1.1/",
}

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


def _pptx_slide_paths(archive):
    """Return slide XML paths in presentation order, validating package relationships."""
    presentation_path = "ppt/presentation.xml"
    relationships_path = "ppt/_rels/presentation.xml.rels"
    presentation = ET.fromstring(archive.read(presentation_path))
    relationships = ET.fromstring(archive.read(relationships_path))
    relation_targets = {
        relation.attrib.get("Id"): relation.attrib.get("Target", "")
        for relation in relationships.findall("rel:Relationship", _PPT_NS)
    }
    paths = []
    for slide_id in presentation.findall(".//p:sldId", _PPT_NS):
        target = relation_targets.get(slide_id.attrib.get(f"{{{_PPT_NS['r']}}}id"), "")
        target = target.replace("\\", "/")
        if target.startswith("/") or ".." in target.split("/"):
            raise SyllabusDocumentError("This PowerPoint contains an invalid slide reference.")
        path = target if target.startswith("ppt/") else f"ppt/{target}"
        if not path.startswith("ppt/slides/slide") or not path.endswith(".xml"):
            raise SyllabusDocumentError("This PowerPoint contains an invalid slide reference.")
        paths.append(path)
    if not paths:
        raise SyllabusDocumentError("This PowerPoint does not contain any readable slides.")
    return paths


def _ppt_paragraph_text(paragraph):
    text = "".join(node.text or "" for node in paragraph.findall(".//a:t", _PPT_NS)).strip()
    if re.fullmatch(r"con[’'\ufffd]d", text, re.I):
        return "Continued"
    return text


def _ppt_paragraph_is_heading(paragraph):
    text = _ppt_paragraph_text(paragraph)
    if not text:
        return False
    run_properties = paragraph.findall(".//a:rPr", _PPT_NS) + paragraph.findall(".//a:endParaRPr", _PPT_NS)
    font_sizes = [int(prop.attrib["sz"]) for prop in run_properties if prop.attrib.get("sz", "").isdigit()]
    bold = any(prop.attrib.get("b") == "1" for prop in run_properties)
    return (
        bool(_CHAPTER_RE.match(text))
        or bool(_NUMBERED_RE.match(text))
        or _is_all_caps_heading(text)
        or (bold and max(font_sizes, default=0) >= 2000 and len(text) <= 160)
    )


def _read_pptx(path):
    """Extract slide titles and visibly emphasized headings from a PowerPoint package."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_PPTX_ZIP_ENTRIES:
                raise SyllabusDocumentError("This PowerPoint contains too many internal items to process safely.")
            if sum(info.file_size for info in infos) > MAX_PPTX_UNCOMPRESSED_BYTES:
                raise SyllabusDocumentError("This PowerPoint expands beyond the safe processing limit.")
            slide_paths = _pptx_slide_paths(archive)
            if len(slide_paths) > MAX_PPTX_SLIDES:
                raise SyllabusDocumentError(f"This PowerPoint has more than {MAX_PPTX_SLIDES} slides, the current processing limit.")

            lines = []
            total_chars = 0
            chapter_seen = False
            seen_headings = set()
            for slide_number, slide_path in enumerate(slide_paths):
                root = ET.fromstring(archive.read(slide_path))
                slide_paragraphs = []
                title_paragraphs = []
                for shape in root.findall(".//p:sp", _PPT_NS):
                    placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", _PPT_NS)
                    placeholder_type = placeholder.attrib.get("type", "") if placeholder is not None else ""
                    for paragraph in shape.findall("p:txBody/a:p", _PPT_NS):
                        text = _ppt_paragraph_text(paragraph)
                        if not text:
                            continue
                        total_chars += len(text)
                        if total_chars > MAX_EXTRACTED_CHARACTERS:
                            raise SyllabusDocumentError("This PowerPoint contains more text than the safe review limit.")
                        item = (text, placeholder_type, paragraph)
                        slide_paragraphs.append(item)
                        if placeholder_type in {"title", "ctrTitle"}:
                            title_paragraphs.append(item)

                # Explicit chapter labels may appear in subtitle placeholders on cover slides.
                for text, _placeholder_type, _paragraph in slide_paragraphs:
                    if _CHAPTER_RE.match(text):
                        key = text.casefold()
                        if key not in seen_headings:
                            lines.append((text, 1, False))
                            seen_headings.add(key)
                            chapter_seen = True

                title_candidates = title_paragraphs + [
                    item for item in slide_paragraphs
                    if item[1] == "subTitle" and _ppt_paragraph_is_heading(item[2])
                ]
                slide_title = next(
                    (text for text, _kind, _paragraph in title_candidates
                     if not _CHAPTER_RE.match(text)
                     and not re.search(r"\b(?:university|college|department|instructor|inst\.?|contents|continued)\b", text, re.I)),
                    "",
                )
                if slide_title and slide_title.casefold() not in {"thank you!!!", "thank you", "continued"}:
                    key = slide_title.casefold()
                    # A first cover title without a chapter label is the outline's first chapter.
                    level = 2 if chapter_seen or lines else 1
                    if key not in seen_headings:
                        lines.append((slide_title, level, False))
                        seen_headings.add(key)

                for text, placeholder_type, paragraph in slide_paragraphs:
                    if placeholder_type in {"title", "ctrTitle"}:
                        continue
                    if placeholder_type == "subTitle":
                        sizes = [int(prop.attrib["sz"]) for prop in (
                            paragraph.findall(".//a:rPr", _PPT_NS) + paragraph.findall(".//a:endParaRPr", _PPT_NS)
                        ) if prop.attrib.get("sz", "").isdigit()]
                        if max(sizes, default=0) < 3000 or re.search(
                            r"\b(?:university|college|department|instructor|inst\.?)\b", text, re.I
                        ):
                            continue
                    if not _ppt_paragraph_is_heading(paragraph):
                        continue
                    key = text.casefold()
                    if key in seen_headings:
                        continue
                    lines.append((text, 2 if _NUMBERED_RE.match(text) else 0, False))
                    seen_headings.add(key)

            if not lines:
                raise SyllabusDocumentError("I couldn't detect readable headings in this PowerPoint. Add headings or upload a text-based syllabus.")
            try:
                core = ET.fromstring(archive.read("docProps/core.xml"))
                metadata_title = (core.findtext("dc:title", default="", namespaces=_PPT_NS) or "").strip()
            except (KeyError, ET.ParseError):
                metadata_title = ""
            if metadata_title.casefold() in {"powerpoint presentation", "presentation", ""}:
                metadata_title = ""
            return lines, metadata_title
    except SyllabusDocumentError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError, ValueError) as exc:
        raise SyllabusDocumentError("I couldn't read this PowerPoint. Please check that it opens correctly and upload it again.") from exc


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
                if heading_level >= 2 and current_chapter is not None:
                    depth = 1
                else:
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
    elif extension == ".pptx":
        lines, document_title = _read_pptx(path)
    else:
        raise SyllabusDocumentError("Upload a PDF, DOCX, or PPTX syllabus file.")
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
