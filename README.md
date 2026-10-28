# ClassFlow — Regular Class and Makeup Tracking

This is an isolated first module, not a replacement for your existing project. It includes:
- teacher-only bot access (set `TEACHER_TELEGRAM_ID`);
- posts a notification to each selected section's configured Telegram group after a successful confirmation;
- choose a regular System Analysis and Design session from the timetable;
- confirm the selected section alone, or combine A + B;
- combined confirmation checks **every timetable row** for the other section during the same time interval;
- duplicate-confirmation protection for the same session on the same real local calendar date;
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

## Syllabus upload and lesson topic tracking
In a private chat, the authorized teacher can upload a text-based PDF or DOCX. ClassFlow extracts likely chapters and numbered or styled headings locally, then saves only a pending outline for review. The teacher can set the course title, rename or remove extracted entries, and add chapters, topics, or nested subtopics. Nothing becomes an active syllabus until **Confirm Syllabus** is selected. Cancelling removes the pending outline; uploaded document files are held in a temporary file and deleted after extraction.

After confirming a regular class, choose **Prepare Lesson**, or open **Lesson Tracker** later. Choose the appropriate syllabus, expand a chapter, and select individual topics or all leaf subtopics in that chapter. Selected topics start as **Planned**. After class, each selected topic can be marked Planned, Partially covered, Completed, or Not covered, with an optional note. Syllabus progress counts unique topics, so assigning one topic to multiple classes does not inflate completion. Syllabuses, class links, topic assignments, statuses, and notes are stored in additive SQLite tables.

PDF and DOCX parsing happens locally; no OCR or external AI service is used. Scanned/image-only PDFs are reported as unreadable until OCR is added in a future feature. The default upload limit is 10 MB and can be changed with `SYLLABUS_MAX_UPLOAD_MB` (1–19 MB, kept below Telegram's current 20 MB bot-download limit). Install the new dependencies with `python -m pip install -r requirements.txt`, then restart the existing bot with `python bot.py`.

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
