# ClassFlow — Regular Class and Makeup Tracking

This is an isolated first module, not a replacement for your existing project. It includes:
- teacher-only bot access (set `TEACHER_TELEGRAM_ID`);
- posts a notification to each selected section's configured Telegram group after a successful confirmation;
- choose a regular System Analysis and Design session from the timetable;
- confirm the selected section alone, or combine A + B;
- combined confirmation checks **every timetable row** for the other section during the same time interval;
- duplicate-confirmation protection for the same session on the same real local calendar date;
- confirms only before a class starts in the configured local timezone; an unconfirmed class selected after it ends becomes a makeup obligation and the next weekly occurrence is offered;
- persistent SQLite storage;
- transactional credit deductions, one duration deduction per confirmed section;
- prevents confirmation if a section has insufficient credit hours.

## Makeup Class Tracking
The teacher can select **Cancel Regular Class** from `/start` to cancel an upcoming System Analysis and Design occurrence from the verified timetable, whether or not it was confirmed first. The menu excludes elapsed classes and occurrences already cancelled. It creates one persistent makeup obligation for the timetable date and immediately sends a cancellation notice to the affected section's configured group. Cancelling does not alter credit-hour balances. Repeating a cancellation action returns the existing obligation instead of creating a second record or sending another notice. Legacy confirmations with mismatched dates are preserved and are not treated as timetable occurrences.

Use **Makeup Obligations** or `/makeups` to review all Pending and Planned obligations. Choose **Plan a makeup**, select one of the next four calendar days, then choose a 1.0–3.0 hour duration in half-hour steps. Class suggestions avoid every timetable entry for the affected section(s), existing planned makeup classes, and the 06:30–07:30 local lunch window. On selection, the plan is saved and the selected duration is deducted once from each affected section's credit balance. `STUDENT_GROUP_CHAT_ID` receives the broadcast when configured; otherwise the bot sends it to the configured affected section groups. Completed and No Makeup Required remain available for updating obligations.

Each cancelled occurrence is one obligation row. When both sections attended the same occurrence, the row records `A,B` and the teaching duration once. Scheduling a makeup deducts the selected duration from each affected section; cancellation and status-only updates do not change balances. Makeup schedules and their selected duration persist across restarts.

The next-day reminder date is the calendar day after the original class date in the stored confirmation. A background task checks due obligations immediately on bot startup and then every 60 seconds, using `TIMEZONE`; reminders missed while the bot is stopped are delivered after restart. A reminder is claimed in SQLite before sending, so it is attempted at most once and cannot be duplicated by a restart. If Telegram rejects the private message because the teacher has not started a private chat, the obligation remains visible with a delivery note. After the teacher opens the bot privately and sends `/start`, that specifically rejected reminder is retried. Telegram can require this private `/start` before the bot is permitted to send reminders. Unattempted cancellation group notices also remain queued in SQLite and are picked up by the background task after a restart.

Group and private notifications are mocked in the tests. No test sends a real Telegram message.

## Credit analysis
The teacher can select **Credit Check**, then Section A or Section B. The report compares the section's remaining balance with the 64.0-hour baseline, displays hours used and remaining, both percentages, a 20-cell used-hours bar, and active sessions logged. Active sessions count confirmed regular occurrences not recorded as cancelled plus makeup obligations marked Planned with a scheduled time or Completed. The calculation reads the existing SQLite balances and records; it does not change them.

## Courses, materials, and lesson tracking
The teacher opens **Courses & Lessons** to manage course outlines, chapter materials, topics, and course progress. A complete text-based PDF, DOCX, or PPTX can be imported and reviewed before confirmation. Alternatively, create a course manually, add chapters, add or rename topics, and attach any Telegram document type to each chapter. Existing files uploaded through the earlier Chapter Management menu can be linked to a course chapter without uploading them again. Course chapters, topic names, and attachment references are stored persistently in SQLite.

From an upcoming confirmed class, choose **Prepare Lesson** and select topics, or choose **Plan Entire Chapter**. ClassFlow remembers the selected course for later occurrences of that same scheduled course. After class, update each selected topic as Planned, Partially covered, Completed, or Not covered, with an optional note. **Teaching Progress** describes one class; **Course Progress** summarizes unique topics across the course. Syllabuses, chapter materials, class links, topic assignments, statuses, and notes persist in SQLite.

PDF, DOCX, and PPTX parsing happens locally; no OCR or external AI service is used. Text in PowerPoint slide shapes is extracted, but text embedded only in slide images cannot be read. Scanned/image-only PDFs are reported as unreadable until OCR is added in a future feature. The default upload limit is 10 MB and can be changed with `SYLLABUS_MAX_UPLOAD_MB` (1–19 MB, kept below Telegram's current 20 MB bot-download limit). Install the project dependencies with `python -m pip install -r requirements.txt`, then restart the existing bot with `python bot.py`.

### Existing Chapter Management files

Files attached before the course workflow remain in their existing SQLite records. Open **Courses & Lessons → course → chapter → Use a Previously Uploaded File** to link one of those files to the chapter. Linking copies its Telegram file reference and metadata; it leaves the original attachment intact. The earlier Chapter Management screen remains reachable from Courses & Lessons for reviewing those stored files.

The SQLite update is additive: it adds per-course-chapter material metadata and a course-to-syllabus default link. Existing course outlines, topic assignments, class confirmations, balances, and stored Chapter Management attachments are preserved.

## Database migration
At startup, `init_db()` creates the makeup tables if absent. For databases with the earlier confirmed-only schema, it rebuilds the two makeup tables to make `confirmation_id` optional and adds a unique `(session_id, original_date)` key. Existing obligations and group notification state are copied; confirmations and balances are preserved. The occurrence key prevents duplicate cancellation records even when no confirmation exists.

## Timetable and local time
`data/timetable.json` contains all 24 entries for Sections A and B from the updated 2nd Year Semester I schedule dated October 25, 2026. It supersedes the earlier schedule PDF. Displayed times are converted to Ethiopian local clock time by subtracting six hours (08:30 becomes 02:30, 13:30 becomes 07:30). The bot uses `Africa/Addis_Ababa` for dates and local-time handling. The updated image contains only this year and both sections.

The starting balance is 64 hours per section, inserted only on first database initialization. Existing saved balances are not overwritten on restart; adjustments to a running database must be applied separately.

## Run
1. Install Python 3.10 or newer.
2. From the project folder, create a virtual environment (recommended): `py -m venv .venv`.
3. Install dependencies: `.venv\\Scripts\\python.exe -m pip install -r requirements.txt`.
4. Copy `.env.example` to `.env`: `Copy-Item .env.example .env`.
5. Set `BOT_TOKEN` to a newly generated token in `.env`. Do not share the token in chat or commit `.env`.
6. Set `TEACHER_TELEGRAM_ID` to the teacher's personal numeric Telegram user ID, and set `TELEGRAM_GROUP_ID_A` and `TELEGRAM_GROUP_ID_B` to the respective groups' numeric chat IDs. Add the bot to both groups and allow it to post messages.
7. From the project folder, run `.venv\\Scripts\\python.exe bot.py` (Pterodactyl/Linux: `python bot.py`). `app.py` is also a supported entry point. Both initialize a main-thread asyncio loop before starting PTB polling.
8. Run the test suite with `.venv\\Scripts\\python.exe -m unittest discover -s tests -v`.

In Telegram, send `/start` to the bot.
In each destination group, send `/groupid` to display that group's actual chat ID. Before confirming, the bot checks it can access all selected sections' groups; if any is inaccessible, no confirmation or credit deduction is made. A combined confirmation is sent once to each distinct selected section group.

## Timetable conflict checks
- Section A's Monday System Analysis and Design overlaps Section B's Introduction to Information Systems and Society.
- Section A's Friday System Analysis and Design overlaps Section B's Introduction to Information Systems and Society.
- Section B's Tuesday System Analysis and Design overlaps Section A's Basic Computer Programming II.
- Section B's Thursday System Analysis and Design overlaps Section A's Introduction to Management.
- Therefore, the current timetable blocks combining A+B for each listed regular System Analysis and Design session.

## Scope decisions for this module
- A successful confirmation sends a message to each distinct selected section group. A Telegram send failure does not undo a confirmation or credit deduction already saved in SQLite.
- Regular class cancellation is available for upcoming timetable occurrences whether or not they have a confirmation; it does not reverse or deduct credit hours.
- The credit deduction is based on the selected regular class duration and is applied to each section included in the confirmation.
- The confirmation is recorded against the date in `TIMEZONE`; the database prevents confirming the same timetable session twice on that date. This allows the same weekly session to be confirmed on later calendar dates.
