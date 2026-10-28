import sqlite3
import json
from datetime import date, datetime, timedelta
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "classflow.sqlite3"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db(initial_balances=None):
    initial_balances = initial_balances or {"A": 64.0, "B": 64.0}
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS balances (
                section TEXT PRIMARY KEY CHECK(section IN ('A','B')),
                hours_remaining REAL NOT NULL CHECK(hours_remaining >= 0)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS confirmations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                day TEXT NOT NULL,
                class_date TEXT NOT NULL,
                course TEXT NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                sections TEXT NOT NULL,
                duration_hours REAL NOT NULL,
                confirmed_at TEXT NOT NULL,
                UNIQUE(session_id, class_date)
            )
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(confirmations)")}
        if "class_date" not in columns:
            # Upgrade the original session_id + weekday uniqueness to a real date.
            conn.execute("ALTER TABLE confirmations RENAME TO confirmations_legacy")
            conn.execute("""
                CREATE TABLE confirmations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    class_date TEXT NOT NULL,
                    course TEXT NOT NULL,
                    start TEXT NOT NULL,
                    end TEXT NOT NULL,
                    sections TEXT NOT NULL,
                    duration_hours REAL NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    UNIQUE(session_id, class_date)
                )
            """)
            legacy_columns = {row["name"] for row in conn.execute("PRAGMA table_info(confirmations_legacy)")}
            if "class_date" in legacy_columns:
                conn.execute("""INSERT INTO confirmations
                    (id, session_id, day, class_date, course, start, end, sections, duration_hours, confirmed_at)
                    SELECT id, session_id, day, class_date, course, start, end, sections, duration_hours, confirmed_at
                    FROM confirmations_legacy""")
            else:
                conn.execute("""INSERT INTO confirmations
                    (id, session_id, day, class_date, course, start, end, sections, duration_hours, confirmed_at)
                    SELECT id, session_id, day, substr(confirmed_at, 1, 10), course, start, end, sections, duration_hours, confirmed_at
                    FROM confirmations_legacy""")
            conn.execute("DROP TABLE confirmations_legacy")
        # Create makeup tables only after the legacy confirmations migration, so
        # their foreign key points at the final confirmations table.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS makeup_obligations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                confirmation_id INTEGER UNIQUE REFERENCES confirmations(id),
                session_id TEXT NOT NULL,
                course TEXT NOT NULL,
                sections TEXT NOT NULL,
                original_date TEXT NOT NULL,
                day TEXT NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
                reminder_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending'
                    CHECK(status IN ('Pending', 'Planned', 'Completed', 'No Makeup Required')),
                planned_datetime TEXT,
                planned_duration_minutes INTEGER,
                created_at TEXT NOT NULL,
                reminder_attempted_at TEXT,
                reminder_sent_at TEXT,
                reminder_error TEXT,
                UNIQUE(session_id, original_date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS makeup_group_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                obligation_id INTEGER NOT NULL REFERENCES makeup_obligations(id) ON DELETE CASCADE,
                section TEXT NOT NULL CHECK(section IN ('A','B')),
                attempted_at TEXT,
                sent_at TEXT,
                error TEXT,
                UNIQUE(obligation_id, section)
            )
        """)
        # Upgrade the original confirmed-only obligation schema in place.
        obligation_columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(makeup_obligations)")}
        has_occurrence_unique = any(
            {idx_row["name"] for idx_row in conn.execute(f"PRAGMA index_info('{index[2]}')")} == {"session_id", "original_date"}
            for index in conn.execute("PRAGMA index_list(makeup_obligations)")
            if index[2]
        )
        if obligation_columns.get("confirmation_id") and obligation_columns["confirmation_id"]["notnull"]:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("ALTER TABLE makeup_group_notifications RENAME TO makeup_group_notifications_old")
            conn.execute("ALTER TABLE makeup_obligations RENAME TO makeup_obligations_old")
            conn.execute("""
                CREATE TABLE makeup_obligations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    confirmation_id INTEGER UNIQUE REFERENCES confirmations(id),
                    session_id TEXT NOT NULL, course TEXT NOT NULL, sections TEXT NOT NULL,
                    original_date TEXT NOT NULL, day TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                    duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0), reminder_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Pending'
                      CHECK(status IN ('Pending', 'Planned', 'Completed', 'No Makeup Required')),
                    planned_datetime TEXT, created_at TEXT NOT NULL, reminder_attempted_at TEXT,
                    reminder_sent_at TEXT, reminder_error TEXT, UNIQUE(session_id, original_date)
                )
            """)
            conn.execute("""
                CREATE TABLE makeup_group_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    obligation_id INTEGER NOT NULL REFERENCES makeup_obligations(id) ON DELETE CASCADE,
                    section TEXT NOT NULL CHECK(section IN ('A','B')), attempted_at TEXT,
                    sent_at TEXT, error TEXT, UNIQUE(obligation_id, section)
                )
            """)
            conn.execute("INSERT INTO makeup_obligations SELECT * FROM makeup_obligations_old")
            conn.execute("INSERT INTO makeup_group_notifications SELECT * FROM makeup_group_notifications_old")
            conn.execute("DROP TABLE makeup_group_notifications_old")
            conn.execute("DROP TABLE makeup_obligations_old")
            conn.execute("PRAGMA foreign_keys = ON")
        obligation_columns = {row["name"] for row in conn.execute("PRAGMA table_info(makeup_obligations)")}
        if "planned_duration_minutes" not in obligation_columns:
            conn.execute("ALTER TABLE makeup_obligations ADD COLUMN planned_duration_minutes INTEGER")
        for section, hours in initial_balances.items():
            conn.execute(
                "INSERT OR IGNORE INTO balances(section, hours_remaining) VALUES (?, ?)",
                (section, float(hours))
            )
        # Additive lesson-preparation schema. Existing class and credit tables are
        # intentionally left intact; pending imports are separate from active data.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS syllabus_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                course_title TEXT NOT NULL DEFAULT '',
                outline_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(teacher_id, content_sha256)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS syllabuses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                source_filename TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(teacher_id, content_sha256)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS syllabus_chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                syllabus_id INTEGER NOT NULL REFERENCES syllabuses(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                title TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS syllabus_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chapter_id INTEGER NOT NULL REFERENCES syllabus_chapters(id) ON DELETE CASCADE,
                parent_topic_id INTEGER REFERENCES syllabus_topics(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                title TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS class_syllabuses (
                confirmation_id INTEGER NOT NULL REFERENCES confirmations(id) ON DELETE CASCADE,
                syllabus_id INTEGER NOT NULL REFERENCES syllabuses(id) ON DELETE CASCADE,
                teacher_id INTEGER NOT NULL,
                linked_at TEXT NOT NULL,
                PRIMARY KEY(confirmation_id, syllabus_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS class_topic_assignments (
                confirmation_id INTEGER NOT NULL REFERENCES confirmations(id) ON DELETE CASCADE,
                topic_id INTEGER NOT NULL REFERENCES syllabus_topics(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'Planned'
                    CHECK(status IN ('Planned', 'Partially covered', 'Completed', 'Not covered')),
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(confirmation_id, topic_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_syllabuses_teacher ON syllabuses(teacher_id, title)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_syllabus_topics_chapter ON syllabus_topics(chapter_id, parent_topic_id, position)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_class_topic_status ON class_topic_assignments(topic_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_class_syllabuses_teacher ON class_syllabuses(teacher_id, confirmation_id)")

def get_balances():
    with connect() as conn:
        rows = conn.execute("SELECT section, hours_remaining FROM balances").fetchall()
    values = {row["section"]: float(row["hours_remaining"]) for row in rows}
    return {"A": values.get("A", 0.0), "B": values.get("B", 0.0)}

def get_credit_analysis(section, baseline_hours=64.0):
    """Return balance, usage percentages, and active regular/makeup session count."""
    if section not in {"A", "B"}:
        raise ValueError("Section must be A or B.")
    baseline_hours = float(baseline_hours)
    with connect() as conn:
        balance = conn.execute(
            "SELECT hours_remaining FROM balances WHERE section = ?", (section,)
        ).fetchone()
        remaining = float(balance["hours_remaining"]) if balance else 0.0
        regular_count = conn.execute("""
            SELECT COUNT(*) AS total
            FROM confirmations c
            WHERE instr(',' || c.sections || ',', ',' || ? || ',') > 0
              AND NOT EXISTS (
                  SELECT 1 FROM makeup_obligations m
                  WHERE m.session_id = c.session_id AND m.original_date = c.class_date
                    AND instr(',' || m.sections || ',', ',' || ? || ',') > 0
              )
        """, (section, section)).fetchone()["total"]
        makeup_count = conn.execute("""
            SELECT COUNT(*) AS total FROM makeup_obligations m
            WHERE instr(',' || m.sections || ',', ',' || ? || ',') > 0
              AND (m.status = 'Completed' OR (m.status = 'Planned' AND m.planned_datetime IS NOT NULL))
        """, (section,)).fetchone()["total"]
    used = baseline_hours - remaining
    if baseline_hours > 0:
        percent_used = min(100.0, max(0.0, used / baseline_hours * 100))
        percent_remaining = min(100.0, max(0.0, remaining / baseline_hours * 100))
    else:
        percent_used = percent_remaining = 0.0
    return {
        "section": section,
        "baseline_hours": baseline_hours,
        "used_hours": used,
        "remaining_hours": remaining,
        "percent_used": percent_used,
        "percent_remaining": percent_remaining,
        "active_sessions": int(regular_count) + int(makeup_count),
    }

def get_confirmation(session_id, class_date):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM confirmations WHERE session_id = ? AND class_date = ?",
            (session_id, class_date)
        ).fetchone()
    return dict(row) if row else None

def get_confirmation_for_occurrence(session_id, class_date, day):
    """Find exact confirmation or a legacy row whose saved date mismatched its weekday."""
    exact = get_confirmation(session_id, class_date)
    if exact:
        return exact
    weekdays = {name: index for index, name in enumerate((
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
    ))}
    if day not in weekdays:
        return None
    target_date = date.fromisoformat(class_date)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM confirmations WHERE session_id = ? AND day = ? ORDER BY confirmed_at DESC",
            (session_id, day)
        ).fetchall()
    for row in rows:
        item = dict(row)
        if date.fromisoformat(item["class_date"]).strftime("%A") == day:
            continue
        try:
            confirmation_date = datetime.fromisoformat(item["confirmed_at"]).date()
        except ValueError:
            continue
        inferred_occurrence = confirmation_date - timedelta(
            days=(confirmation_date.weekday() - weekdays[day]) % 7
        )
        if inferred_occurrence == target_date:
            return item
    return None

def confirm_session(session_id, day, class_date, sections, duration_hours, confirmed_at, course, start, end):
    sections = sorted(set(sections))
    if not sections or not set(sections).issubset({"A", "B"}):
        raise ValueError("Invalid section selection.")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT sections FROM confirmations WHERE session_id = ? AND class_date = ?",
            (session_id, class_date)
        ).fetchone()
        if existing:
            raise ValueError("This regular class has already been confirmed. No credits were deducted.")
        rows = conn.execute(
            "SELECT section, hours_remaining FROM balances WHERE section IN (%s)" %
            ",".join("?" for _ in sections), sections
        ).fetchall()
        balance = {r["section"]: float(r["hours_remaining"]) for r in rows}
        for section in sections:
            if balance.get(section, 0) < duration_hours:
                raise ValueError(f"Section {section} does not have enough credit hours. No credits were deducted.")
        conn.execute(
            """INSERT INTO confirmations
               (session_id, day, class_date, course, start, end, sections, duration_hours, confirmed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, day, class_date, course, start, end, ",".join(sections), duration_hours, confirmed_at)
        )
        for section in sections:
            conn.execute(
                "UPDATE balances SET hours_remaining = hours_remaining - ? WHERE section = ?",
                (duration_hours, section)
            )
        conn.commit()

def get_cancellable_confirmations():
    with connect() as conn:
        rows = conn.execute("""
            SELECT c.* FROM confirmations c
            LEFT JOIN makeup_obligations m ON m.confirmation_id = c.id
            WHERE m.id IS NULL
            ORDER BY c.class_date DESC, c.start, c.id DESC
        """).fetchall()
    return [dict(row) for row in rows]

def get_makeup_occurrence(session_id, original_date):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM makeup_obligations WHERE session_id = ? AND original_date = ?",
            (session_id, original_date)
        ).fetchone()
    return dict(row) if row else None

def get_confirmation_by_id(confirmation_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM confirmations WHERE id = ?", (confirmation_id,)).fetchone()
    return dict(row) if row else None

def cancel_confirmation_occurrence(confirmation_id, reminder_date, created_at):
    """Create one persistent makeup obligation and section notification rows."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        confirmation = conn.execute(
            "SELECT * FROM confirmations WHERE id = ?", (confirmation_id,)
        ).fetchone()
        if not confirmation:
            raise ValueError("That confirmed class occurrence no longer exists.")
        existing = conn.execute(
            "SELECT * FROM makeup_obligations WHERE confirmation_id = ?", (confirmation_id,)
        ).fetchone()
        if existing:
            return dict(existing), False
        sections = sorted(set(confirmation["sections"].split(",")))
        duration_minutes = round(float(confirmation["duration_hours"]) * 60)
        cursor = conn.execute("""
            INSERT INTO makeup_obligations
                (confirmation_id, session_id, course, sections, original_date, day, start, end,
                 duration_minutes, reminder_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            confirmation["id"], confirmation["session_id"], confirmation["course"],
            ",".join(sections), confirmation["class_date"], confirmation["day"],
            confirmation["start"], confirmation["end"], duration_minutes,
            reminder_date, created_at
        ))
        obligation_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO makeup_group_notifications(obligation_id, section) VALUES (?, ?)",
            [(obligation_id, section) for section in sections]
        )
        row = conn.execute(
            "SELECT * FROM makeup_obligations WHERE id = ?", (obligation_id,)
        ).fetchone()
        conn.commit()
    return dict(row), True

def cancel_scheduled_occurrence(session_id, original_date, day, course, sections, start, end,
                                duration_minutes, reminder_date, created_at):
    """Track cancellation by timetable occurrence, whether confirmed or not."""
    sections = sorted(set(sections))
    if not sections or not set(sections).issubset({"A", "B"}):
        raise ValueError("Invalid section selection.")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM makeup_obligations WHERE session_id = ? AND original_date = ?",
            (session_id, original_date)
        ).fetchone()
        if existing:
            return dict(existing), False
        confirmation = conn.execute(
            "SELECT id FROM confirmations WHERE session_id = ? AND class_date = ?",
            (session_id, original_date)
        ).fetchone()
        cursor = conn.execute("""
            INSERT INTO makeup_obligations
                (confirmation_id, session_id, course, sections, original_date, day, start, end,
                 duration_minutes, reminder_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (confirmation["id"] if confirmation else None, session_id, course,
              ",".join(sections), original_date, day, start, end, duration_minutes,
              reminder_date, created_at))
        obligation_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO makeup_group_notifications(obligation_id, section) VALUES (?, ?)",
            [(obligation_id, section) for section in sections]
        )
        row = conn.execute("SELECT * FROM makeup_obligations WHERE id = ?", (obligation_id,)).fetchone()
    return dict(row), True

def get_makeup_obligation(obligation_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM makeup_obligations WHERE id = ?", (obligation_id,)).fetchone()
    return dict(row) if row else None

def list_makeup_obligations(include_closed=False):
    query = "SELECT * FROM makeup_obligations"
    if not include_closed:
        query += " WHERE status IN ('Pending', 'Planned')"
    query += " ORDER BY original_date, id"
    with connect() as conn:
        rows = conn.execute(query).fetchall()
    return [dict(row) for row in rows]

def set_makeup_status(obligation_id, status, planned_datetime=None):
    allowed = {"Planned", "Completed", "No Makeup Required"}
    if status not in allowed:
        raise ValueError("Invalid makeup status.")
    if status == "Planned" and planned_datetime is None:
        # Planned can be saved before its date/time is entered, so it remains visible
        # if the bot restarts while waiting for the teacher's reply.
        pass
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE makeup_obligations SET status = ?, planned_datetime = ? "
            "WHERE id = ? AND status IN ('Pending', 'Planned')",
            (status, planned_datetime if status == "Planned" else None, obligation_id)
        )
        if cursor.rowcount == 0:
            raise ValueError("That makeup obligation was not found or is already closed.")
        row = conn.execute(
            "SELECT * FROM makeup_obligations WHERE id = ?", (obligation_id,)
        ).fetchone()
    return dict(row)

def get_available_makeup_slots(schedule, sections, day, class_date, duration_minutes,
                               now=None, step_minutes=30):
    """Return local-clock slots that avoid timetable, existing makeup plans, and lunch."""
    sections = sorted(set(sections))
    if not sections or not set(sections).issubset({"A", "B"}):
        raise ValueError("Invalid section selection.")
    duration_minutes = int(duration_minutes)
    if duration_minutes <= 0 or duration_minutes % step_minutes:
        raise ValueError("Duration must be in 30-minute increments.")
    # Bound suggestions to the teaching-day envelope shown by the supplied timetable.
    entries = [item for item in schedule if item.get("start") and item.get("end")]
    if not entries:
        return []
    day_start = min(_minutes(item["start"]) for item in entries)
    day_end = max(_minutes(item["end"]) for item in entries)
    busy = [(_minutes(item["start"]), _minutes(item["end"])) for item in entries
            if item.get("day") == day and item.get("section") in sections]
    with connect() as conn:
        planned = conn.execute("""
            SELECT sections, planned_datetime, planned_duration_minutes FROM makeup_obligations
            WHERE status = 'Planned' AND planned_datetime IS NOT NULL
        """).fetchall()
    for item in planned:
        try:
            booked = datetime.fromisoformat(item["planned_datetime"])
        except ValueError:
            continue
        if booked.date().isoformat() != class_date:
            continue
        if set(item["sections"].split(",")) & set(sections):
            start = booked.hour * 60 + booked.minute
            minutes = item["planned_duration_minutes"] or duration_minutes
            busy.append((start, start + minutes))
    lunch = (6 * 60 + 30, 7 * 60 + 30)
    if day_start < lunch[1] and lunch[0] < day_end:
        busy.append(lunch)
    slots = []
    for start in range(day_start, day_end - duration_minutes + 1, step_minutes):
        end = start + duration_minutes
        if any(start < occupied_end and occupied_start < end for occupied_start, occupied_end in busy):
            continue
        if now is not None and class_date == now.date().isoformat() and start <= now.hour * 60 + now.minute:
            continue
        slots.append({"start": f"{start // 60:02d}:{start % 60:02d}",
                      "end": f"{end // 60:02d}:{end % 60:02d}"})
    return slots

def _minutes(value):
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute

def schedule_makeup_class(obligation_id, planned_datetime, duration_minutes, schedule=None):
    """Persist the schedule and atomically deduct duration from each affected section."""
    duration_minutes = int(duration_minutes)
    hours = duration_minutes / 60
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        obligation = conn.execute(
            "SELECT * FROM makeup_obligations WHERE id = ?", (obligation_id,)
        ).fetchone()
        if not obligation or obligation["status"] not in ("Pending", "Planned"):
            raise ValueError("That makeup obligation is not available for scheduling.")
        if obligation["planned_datetime"]:
            raise ValueError("A makeup time is already scheduled for this obligation.")
        sections = obligation["sections"].split(",")
        if schedule is not None:
            planned = datetime.fromisoformat(planned_datetime)
            start = planned.hour * 60 + planned.minute
            end = start + duration_minutes
            if duration_minutes < 60 or duration_minutes > 180 or duration_minutes % 30:
                raise ValueError("Makeup duration must be between 1.0 and 3.0 hours in 30-minute steps.")
            day_entries = [item for item in schedule if item.get("start") and item.get("end")]
            if not day_entries or start < min(_minutes(row["start"]) for row in day_entries) or end > max(_minutes(row["end"]) for row in day_entries):
                raise ValueError("That time is outside the timetable's teaching-day hours.")
            busy = [(_minutes(item["start"]), _minutes(item["end"])) for item in schedule
                    if item.get("day") == planned.strftime("%A") and item.get("section") in sections]
            busy.append((390, 450))  # 06:30–07:30 local lunch
            for row in conn.execute("""
                SELECT id, sections, planned_datetime, planned_duration_minutes, duration_minutes
                FROM makeup_obligations WHERE status = 'Planned' AND planned_datetime IS NOT NULL
            """):
                if row["id"] == obligation_id:
                    continue
                other = datetime.fromisoformat(row["planned_datetime"])
                if other.date() != planned.date() or not (set(row["sections"].split(",")) & set(sections)):
                    continue
                other_start = other.hour * 60 + other.minute
                other_length = row["planned_duration_minutes"] or row["duration_minutes"]
                busy.append((other_start, other_start + other_length))
            if start % 30 or any(start < busy_end and busy_start < end for busy_start, busy_end in busy):
                raise ValueError("That time is no longer available. Choose another open slot.")
        balances = {r["section"]: float(r["hours_remaining"]) for r in conn.execute(
            "SELECT section, hours_remaining FROM balances WHERE section IN (%s)" %
            ",".join("?" for _ in sections), sections
        )}
        insufficient = [section for section in sections if balances.get(section, 0) < hours]
        if insufficient:
            raise ValueError("Insufficient credit hours for Section " + ", ".join(insufficient) + ".")
        conn.execute("""
            UPDATE makeup_obligations SET status = 'Planned', planned_datetime = ?, planned_duration_minutes = ?
            WHERE id = ? AND planned_datetime IS NULL
        """, (planned_datetime, duration_minutes, obligation_id))
        for section in sections:
            conn.execute("UPDATE balances SET hours_remaining = hours_remaining - ? WHERE section = ?",
                         (hours, section))
        updated = conn.execute("SELECT * FROM makeup_obligations WHERE id = ?", (obligation_id,)).fetchone()
    return dict(updated)

def list_due_makeup_reminders(local_date):
    with connect() as conn:
        rows = conn.execute("""
            SELECT * FROM makeup_obligations
            WHERE reminder_date <= ? AND status = 'Pending' AND reminder_attempted_at IS NULL
            ORDER BY reminder_date, id
        """, (local_date,)).fetchall()
    return [dict(row) for row in rows]

def claim_makeup_reminder(obligation_id, attempted_at):
    """Claim a reminder once before Telegram I/O to avoid duplicate sends on restart."""
    with connect() as conn:
        cursor = conn.execute("""
            UPDATE makeup_obligations SET reminder_attempted_at = ?
            WHERE id = ? AND reminder_attempted_at IS NULL
        """, (attempted_at, obligation_id))
        return cursor.rowcount == 1

def finish_makeup_reminder(obligation_id, sent_at=None, error=None):
    with connect() as conn:
        conn.execute("""
            UPDATE makeup_obligations SET reminder_sent_at = ?, reminder_error = ?
            WHERE id = ?
        """, (sent_at, error, obligation_id))

def reset_forbidden_makeup_reminders():
    """Retry only reminders rejected because the teacher had not opened private chat."""
    with connect() as conn:
        cursor = conn.execute("""
            UPDATE makeup_obligations
            SET reminder_attempted_at = NULL, reminder_error = NULL
            WHERE reminder_sent_at IS NULL AND reminder_error LIKE '%Forbidden%'
        """)
        return cursor.rowcount

def claim_group_notification(notification_id, attempted_at):
    with connect() as conn:
        cursor = conn.execute("""
            UPDATE makeup_group_notifications SET attempted_at = ?
            WHERE id = ? AND attempted_at IS NULL
        """, (attempted_at, notification_id))
        return cursor.rowcount == 1

def finish_group_notification(notification_id, sent_at=None, error=None):
    with connect() as conn:
        conn.execute("""
            UPDATE makeup_group_notifications SET sent_at = ?, error = ? WHERE id = ?
        """, (sent_at, error, notification_id))

def get_group_notifications(obligation_id):
    with connect() as conn:
        rows = conn.execute("""
            SELECT * FROM makeup_group_notifications WHERE obligation_id = ? ORDER BY section
        """, (obligation_id,)).fetchall()
    return [dict(row) for row in rows]

def list_pending_group_notifications():
    with connect() as conn:
        rows = conn.execute("""
            SELECT n.id AS notification_id, n.section, m.id AS obligation_id,
                   m.course, m.original_date, m.start, m.end, m.duration_minutes
            FROM makeup_group_notifications n
            JOIN makeup_obligations m ON m.id = n.obligation_id
            WHERE n.attempted_at IS NULL
            ORDER BY n.id
        """).fetchall()
    return [dict(row) for row in rows]


def _lesson_teacher_id(teacher_id):
    try:
        value = int(teacher_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("A valid authorized teacher ID is required.") from exc
    if value <= 0:
        raise ValueError("A valid authorized teacher ID is required.")
    return value


def _pending_syllabus_row(row):
    if not row:
        return None
    item = dict(row)
    item["outline"] = json.loads(item.pop("outline_json"))
    return item


def create_pending_syllabus_import(teacher_id, filename, content_sha256, outline, created_at):
    """Stage a teacher-reviewed import; no active syllabus is created here."""
    teacher_id = _lesson_teacher_id(teacher_id)
    serialized = json.dumps(outline, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > 500_000:
        raise ValueError("The extracted outline is too large to review safely.")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            "SELECT id, title FROM syllabuses WHERE teacher_id = ? AND content_sha256 = ?",
            (teacher_id, content_sha256),
        ).fetchone()
        if active:
            return {"status": "already_imported", "syllabus": dict(active)}
        pending = conn.execute(
            "SELECT * FROM syllabus_imports WHERE teacher_id = ? AND content_sha256 = ?",
            (teacher_id, content_sha256),
        ).fetchone()
        if pending:
            return {"status": "already_pending", "import": _pending_syllabus_row(pending)}
        cursor = conn.execute("""
            INSERT INTO syllabus_imports
                (teacher_id, filename, content_sha256, course_title, outline_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (teacher_id, filename, content_sha256,
              str(outline.get("course_title", "")).strip(), serialized, created_at))
        row = conn.execute(
            "SELECT * FROM syllabus_imports WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return {"status": "created", "import": _pending_syllabus_row(row)}


def get_pending_syllabus_import(import_id, teacher_id):
    teacher_id = _lesson_teacher_id(teacher_id)
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM syllabus_imports WHERE id = ? AND teacher_id = ?",
            (int(import_id), teacher_id),
        ).fetchone()
    return _pending_syllabus_row(row)


def update_pending_syllabus_import(import_id, teacher_id, outline):
    teacher_id = _lesson_teacher_id(teacher_id)
    serialized = json.dumps(outline, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > 500_000:
        raise ValueError("The edited outline is too large to store safely.")
    with connect() as conn:
        cursor = conn.execute("""
            UPDATE syllabus_imports
            SET course_title = ?, outline_json = ?
            WHERE id = ? AND teacher_id = ?
        """, (str(outline.get("course_title", "")).strip(), serialized,
              int(import_id), teacher_id))
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM syllabus_imports WHERE id = ?", (int(import_id),)
        ).fetchone()
    return _pending_syllabus_row(row)


def cancel_pending_syllabus_import(import_id, teacher_id):
    teacher_id = _lesson_teacher_id(teacher_id)
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM syllabus_imports WHERE id = ? AND teacher_id = ?",
            (int(import_id), teacher_id),
        )
    return cursor.rowcount == 1


def confirm_syllabus_import(import_id, teacher_id, created_at):
    """Atomically activate a reviewed outline and remove its staging row."""
    teacher_id = _lesson_teacher_id(teacher_id)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        staged = conn.execute(
            "SELECT * FROM syllabus_imports WHERE id = ? AND teacher_id = ?",
            (int(import_id), teacher_id),
        ).fetchone()
        if not staged:
            raise ValueError("This syllabus review has expired. Upload the document again.")
        existing = conn.execute(
            "SELECT id FROM syllabuses WHERE teacher_id = ? AND content_sha256 = ?",
            (teacher_id, staged["content_sha256"]),
        ).fetchone()
        if existing:
            raise ValueError("This syllabus has already been imported.")
        outline = json.loads(staged["outline_json"])
        title = str(staged["course_title"] or outline.get("course_title") or "").strip()
        chapters = outline.get("chapters")
        if not title:
            raise ValueError("Add a course or syllabus title before confirming the import.")
        if not isinstance(chapters, list) or not chapters:
            raise ValueError("Add at least one chapter before confirming the import.")
        cursor = conn.execute("""
            INSERT INTO syllabuses
                (teacher_id, title, source_filename, content_sha256, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (teacher_id, title, staged["filename"], staged["content_sha256"], created_at))
        syllabus_id = cursor.lastrowid

        def insert_topics(chapter_id, parent_id, topics):
            if not isinstance(topics, list):
                raise ValueError("The reviewed outline has an invalid topic list.")
            for position, topic in enumerate(topics):
                if not isinstance(topic, dict):
                    raise ValueError("The reviewed outline contains an invalid topic.")
                topic_title = str(topic.get("title", "")).strip()
                if not topic_title:
                    raise ValueError("Every topic needs a title before confirming the import.")
                topic_cursor = conn.execute("""
                    INSERT INTO syllabus_topics (chapter_id, parent_topic_id, position, title)
                    VALUES (?, ?, ?, ?)
                """, (chapter_id, parent_id, position, topic_title))
                insert_topics(chapter_id, topic_cursor.lastrowid, topic.get("children", []))

        for position, chapter in enumerate(chapters):
            if not isinstance(chapter, dict) or not str(chapter.get("title", "")).strip():
                raise ValueError("Every chapter needs a title before confirming the import.")
            chapter_cursor = conn.execute("""
                INSERT INTO syllabus_chapters (syllabus_id, position, title)
                VALUES (?, ?, ?)
            """, (syllabus_id, position, str(chapter["title"]).strip()))
            insert_topics(chapter_cursor.lastrowid, None, chapter.get("topics", []))
        conn.execute("DELETE FROM syllabus_imports WHERE id = ?", (int(import_id),))
        syllabus = conn.execute("SELECT * FROM syllabuses WHERE id = ?", (syllabus_id,)).fetchone()
    return dict(syllabus)


def list_syllabuses(teacher_id):
    teacher_id = _lesson_teacher_id(teacher_id)
    with connect() as conn:
        rows = conn.execute("""
            SELECT s.*, COUNT(t.id) AS topic_count
            FROM syllabuses s
            LEFT JOIN syllabus_chapters c ON c.syllabus_id = s.id
            LEFT JOIN syllabus_topics t ON t.chapter_id = c.id
            WHERE s.teacher_id = ?
            GROUP BY s.id
            ORDER BY s.title COLLATE NOCASE, s.id
        """, (teacher_id,)).fetchall()
    return [dict(row) for row in rows]


def get_syllabus(syllabus_id, teacher_id):
    teacher_id = _lesson_teacher_id(teacher_id)
    with connect() as conn:
        row = conn.execute("""
            SELECT s.*, COUNT(t.id) AS topic_count
            FROM syllabuses s
            LEFT JOIN syllabus_chapters c ON c.syllabus_id = s.id
            LEFT JOIN syllabus_topics t ON t.chapter_id = c.id
            WHERE s.id = ? AND s.teacher_id = ?
            GROUP BY s.id
        """, (int(syllabus_id), teacher_id)).fetchone()
    return dict(row) if row else None


def get_teacher_lessons(teacher_id, today, offset=0, limit=8):
    """Return all active confirmed classes available to the configured teacher."""
    _lesson_teacher_id(teacher_id)
    offset, limit = max(0, int(offset)), min(20, max(1, int(limit)))
    with connect() as conn:
        rows = conn.execute("""
            SELECT c.*,
                (SELECT COUNT(*) FROM class_syllabuses cs
                 WHERE cs.confirmation_id = c.id AND cs.teacher_id = ?) AS syllabus_count,
                (SELECT COUNT(*) FROM class_topic_assignments a
                 WHERE a.confirmation_id = c.id) AS planned_topic_count
            FROM confirmations c
            WHERE NOT EXISTS (
                  SELECT 1 FROM makeup_obligations m
                  WHERE m.session_id = c.session_id AND m.original_date = c.class_date
              )
            ORDER BY CASE WHEN c.class_date >= ? THEN 0 ELSE 1 END,
                     CASE WHEN c.class_date >= ? THEN c.class_date END ASC,
                     CASE WHEN c.class_date < ? THEN c.class_date END DESC,
                     c.start, c.id
            LIMIT ? OFFSET ?
        """, (int(teacher_id), today, today, today, limit, offset)).fetchall()
    return [dict(row) for row in rows]


def get_lesson_class(confirmation_id, teacher_id):
    _lesson_teacher_id(teacher_id)
    with connect() as conn:
        row = conn.execute("""
            SELECT c.* FROM confirmations c
            WHERE c.id = ? AND NOT EXISTS (
                SELECT 1 FROM makeup_obligations m
                WHERE m.session_id = c.session_id AND m.original_date = c.class_date
            )
        """, (int(confirmation_id),)).fetchone()
    return dict(row) if row else None


def get_class_syllabuses(confirmation_id, teacher_id):
    teacher_id = _lesson_teacher_id(teacher_id)
    with connect() as conn:
        rows = conn.execute("""
            SELECT s.id, s.title, COUNT(t.id) AS topic_count
            FROM class_syllabuses cs
            JOIN confirmations active_class ON active_class.id = cs.confirmation_id
            JOIN syllabuses s ON s.id = cs.syllabus_id AND s.teacher_id = cs.teacher_id
            LEFT JOIN syllabus_chapters c ON c.syllabus_id = s.id
            LEFT JOIN syllabus_topics t ON t.chapter_id = c.id
            WHERE cs.confirmation_id = ? AND cs.teacher_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM makeup_obligations m
                  WHERE m.session_id = active_class.session_id AND m.original_date = active_class.class_date
              )
            GROUP BY s.id
            ORDER BY s.title COLLATE NOCASE, s.id
        """, (int(confirmation_id), teacher_id)).fetchall()
    return [dict(row) for row in rows]


def link_syllabus_to_class(confirmation_id, syllabus_id, teacher_id, linked_at):
    teacher_id = _lesson_teacher_id(teacher_id)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        class_row = conn.execute("""
            SELECT c.id FROM confirmations c
            WHERE c.id = ? AND NOT EXISTS (
                SELECT 1 FROM makeup_obligations m
                WHERE m.session_id = c.session_id AND m.original_date = c.class_date
            )
        """, (int(confirmation_id),)
        ).fetchone()
        syllabus = conn.execute(
            "SELECT id FROM syllabuses WHERE id = ? AND teacher_id = ?",
            (int(syllabus_id), teacher_id),
        ).fetchone()
        if not class_row or not syllabus:
            return False
        conn.execute("""
            INSERT OR IGNORE INTO class_syllabuses
                (confirmation_id, syllabus_id, teacher_id, linked_at)
            VALUES (?, ?, ?, ?)
        """, (int(confirmation_id), int(syllabus_id), teacher_id, linked_at))
    return True


def get_syllabus_chapters(syllabus_id, teacher_id):
    syllabus = get_syllabus(syllabus_id, teacher_id)
    if not syllabus:
        return []
    with connect() as conn:
        rows = conn.execute("""
            SELECT id, syllabus_id, position, title
            FROM syllabus_chapters WHERE syllabus_id = ? ORDER BY position, id
        """, (int(syllabus_id),)).fetchall()
    return [dict(row) for row in rows]


def get_class_syllabus_topics(confirmation_id, syllabus_id, teacher_id):
    teacher_id = _lesson_teacher_id(teacher_id)
    with connect() as conn:
        allowed = conn.execute("""
            SELECT 1 FROM class_syllabuses cs
            JOIN confirmations active_class ON active_class.id = cs.confirmation_id
            JOIN syllabuses s ON s.id = cs.syllabus_id AND s.teacher_id = cs.teacher_id
            WHERE cs.confirmation_id = ? AND cs.syllabus_id = ? AND cs.teacher_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM makeup_obligations m
                  WHERE m.session_id = active_class.session_id AND m.original_date = active_class.class_date
              )
        """, (int(confirmation_id), int(syllabus_id), teacher_id)).fetchone()
        if not allowed:
            return []
        rows = conn.execute("""
            SELECT t.id, t.chapter_id, t.parent_topic_id, t.position, t.title,
                   c.title AS chapter_title, a.status, a.note
            FROM syllabus_topics t
            JOIN syllabus_chapters c ON c.id = t.chapter_id
            LEFT JOIN class_topic_assignments a
              ON a.topic_id = t.id AND a.confirmation_id = ?
            WHERE c.syllabus_id = ?
            ORDER BY c.position, t.parent_topic_id IS NOT NULL, t.parent_topic_id, t.position, t.id
        """, (int(confirmation_id), int(syllabus_id))).fetchall()
    return [dict(row) for row in rows]


def toggle_class_topic(confirmation_id, syllabus_id, topic_id, teacher_id, updated_at):
    teacher_id = _lesson_teacher_id(teacher_id)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        valid = conn.execute("""
            SELECT 1 FROM class_syllabuses cs
            JOIN confirmations active_class ON active_class.id = cs.confirmation_id
            JOIN syllabus_chapters c ON c.syllabus_id = cs.syllabus_id
            JOIN syllabus_topics t ON t.chapter_id = c.id
            WHERE cs.confirmation_id = ? AND cs.syllabus_id = ? AND cs.teacher_id = ? AND t.id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM makeup_obligations m
                  WHERE m.session_id = active_class.session_id AND m.original_date = active_class.class_date
              )
        """, (int(confirmation_id), int(syllabus_id), teacher_id, int(topic_id))).fetchone()
        if not valid:
            return False
        existing = conn.execute("""
            SELECT 1 FROM class_topic_assignments
            WHERE confirmation_id = ? AND topic_id = ?
        """, (int(confirmation_id), int(topic_id))).fetchone()
        if existing:
            conn.execute("DELETE FROM class_topic_assignments WHERE confirmation_id = ? AND topic_id = ?",
                         (int(confirmation_id), int(topic_id)))
            return False
        conn.execute("""
            INSERT INTO class_topic_assignments (confirmation_id, topic_id, status, updated_at)
            VALUES (?, ?, 'Planned', ?)
        """, (int(confirmation_id), int(topic_id), updated_at))
    return True


def set_chapter_topics_selected(confirmation_id, syllabus_id, chapter_id, teacher_id, selected, updated_at):
    teacher_id = _lesson_teacher_id(teacher_id)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        valid = conn.execute("""
            SELECT 1 FROM class_syllabuses cs
            JOIN confirmations active_class ON active_class.id = cs.confirmation_id
            JOIN syllabus_chapters c ON c.syllabus_id = cs.syllabus_id
            WHERE cs.confirmation_id = ? AND cs.syllabus_id = ? AND cs.teacher_id = ? AND c.id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM makeup_obligations m
                  WHERE m.session_id = active_class.session_id AND m.original_date = active_class.class_date
              )
        """, (int(confirmation_id), int(syllabus_id), teacher_id, int(chapter_id))).fetchone()
        if not valid:
            return False
        leaf_ids = [row[0] for row in conn.execute("""
            SELECT t.id FROM syllabus_topics t
            WHERE t.chapter_id = ? AND NOT EXISTS (
                SELECT 1 FROM syllabus_topics child WHERE child.parent_topic_id = t.id
            )
        """, (int(chapter_id),)).fetchall()]
        if selected:
            conn.executemany("""
                INSERT OR IGNORE INTO class_topic_assignments
                    (confirmation_id, topic_id, status, updated_at)
                VALUES (?, ?, 'Planned', ?)
            """, [(int(confirmation_id), topic_id, updated_at) for topic_id in leaf_ids])
        elif leaf_ids:
            conn.executemany(
                "DELETE FROM class_topic_assignments WHERE confirmation_id = ? AND topic_id = ?",
                [(int(confirmation_id), topic_id) for topic_id in leaf_ids],
            )
    return True


def get_class_topic_assignments(confirmation_id, teacher_id):
    teacher_id = _lesson_teacher_id(teacher_id)
    with connect() as conn:
        rows = conn.execute("""
            SELECT a.confirmation_id, a.topic_id, a.status, a.note, a.updated_at,
                   t.title AS topic_title, t.parent_topic_id, c.id AS chapter_id,
                   c.title AS chapter_title, s.id AS syllabus_id, s.title AS syllabus_title
            FROM class_topic_assignments a
            JOIN syllabus_topics t ON t.id = a.topic_id
            JOIN syllabus_chapters c ON c.id = t.chapter_id
            JOIN syllabuses s ON s.id = c.syllabus_id
            JOIN class_syllabuses cs
              ON cs.confirmation_id = a.confirmation_id AND cs.syllabus_id = s.id
            WHERE a.confirmation_id = ? AND cs.teacher_id = ? AND s.teacher_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM confirmations active_class
                  JOIN makeup_obligations m
                    ON m.session_id = active_class.session_id AND m.original_date = active_class.class_date
                  WHERE active_class.id = a.confirmation_id
              )
            ORDER BY s.title, c.position, t.position, t.id
        """, (int(confirmation_id), teacher_id, teacher_id)).fetchall()
    return [dict(row) for row in rows]


def set_class_topic_status(confirmation_id, topic_id, teacher_id, status, updated_at, note=None):
    teacher_id = _lesson_teacher_id(teacher_id)
    allowed_statuses = {"Planned", "Partially covered", "Completed", "Not covered"}
    if status not in allowed_statuses:
        raise ValueError("Invalid lesson topic status.")
    with connect() as conn:
        cursor = conn.execute("""
            UPDATE class_topic_assignments
            SET status = ?, updated_at = ?, note = COALESCE(?, note)
            WHERE confirmation_id = ? AND topic_id = ?
              AND EXISTS (
                SELECT 1 FROM class_syllabuses cs
                JOIN confirmations active_class ON active_class.id = cs.confirmation_id
                JOIN syllabuses s ON s.id = cs.syllabus_id AND s.teacher_id = cs.teacher_id
                JOIN syllabus_chapters c ON c.syllabus_id = s.id
                JOIN syllabus_topics t ON t.chapter_id = c.id AND t.id = class_topic_assignments.topic_id
                WHERE cs.confirmation_id = class_topic_assignments.confirmation_id
                  AND cs.teacher_id = ? AND s.teacher_id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM makeup_obligations m
                    WHERE m.session_id = active_class.session_id AND m.original_date = active_class.class_date
                  )
              )
        """, (status, updated_at, note, int(confirmation_id), int(topic_id), teacher_id, teacher_id))
    return cursor.rowcount == 1


def set_class_topic_note(confirmation_id, topic_id, teacher_id, note, updated_at):
    note = str(note).strip()
    if len(note) > 500:
        raise ValueError("Notes can be at most 500 characters.")
    teacher_id = _lesson_teacher_id(teacher_id)
    with connect() as conn:
        cursor = conn.execute("""
            UPDATE class_topic_assignments SET note = ?, updated_at = ?
            WHERE confirmation_id = ? AND topic_id = ?
              AND EXISTS (
                SELECT 1 FROM class_syllabuses cs
                JOIN confirmations active_class ON active_class.id = cs.confirmation_id
                JOIN syllabuses s ON s.id = cs.syllabus_id AND s.teacher_id = cs.teacher_id
                WHERE cs.confirmation_id = class_topic_assignments.confirmation_id
                  AND cs.teacher_id = ? AND s.teacher_id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM makeup_obligations m
                    WHERE m.session_id = active_class.session_id AND m.original_date = active_class.class_date
                  )
              )
        """, (note, updated_at, int(confirmation_id), int(topic_id), teacher_id, teacher_id))
    return cursor.rowcount == 1


def get_syllabus_progress(syllabus_id, teacher_id):
    teacher_id = _lesson_teacher_id(teacher_id)
    syllabus = get_syllabus(syllabus_id, teacher_id)
    if not syllabus:
        return None
    with connect() as conn:
        total = conn.execute("""
            SELECT COUNT(*) FROM syllabus_topics t
            JOIN syllabus_chapters c ON c.id = t.chapter_id WHERE c.syllabus_id = ?
        """, (int(syllabus_id),)).fetchone()[0]
        completed = conn.execute("""
            SELECT COUNT(DISTINCT t.id) FROM syllabus_topics t
            JOIN syllabus_chapters c ON c.id = t.chapter_id
            JOIN class_topic_assignments a ON a.topic_id = t.id AND a.status = 'Completed'
            JOIN class_syllabuses cs ON cs.confirmation_id = a.confirmation_id
                AND cs.syllabus_id = c.syllabus_id AND cs.teacher_id = ?
            JOIN confirmations active_class ON active_class.id = a.confirmation_id
            WHERE c.syllabus_id = ? AND NOT EXISTS (
                SELECT 1 FROM makeup_obligations m
                WHERE m.session_id = active_class.session_id AND m.original_date = active_class.class_date
            )
        """, (teacher_id, int(syllabus_id))).fetchone()[0]
        partial = conn.execute("""
            SELECT COUNT(DISTINCT t.id) FROM syllabus_topics t
            JOIN syllabus_chapters c ON c.id = t.chapter_id
            JOIN class_topic_assignments a ON a.topic_id = t.id AND a.status = 'Partially covered'
            JOIN class_syllabuses cs ON cs.confirmation_id = a.confirmation_id
                AND cs.syllabus_id = c.syllabus_id AND cs.teacher_id = ?
            JOIN confirmations active_class ON active_class.id = a.confirmation_id
            WHERE c.syllabus_id = ? AND NOT EXISTS (
                SELECT 1 FROM class_topic_assignments done
                JOIN class_syllabuses done_cs ON done_cs.confirmation_id = done.confirmation_id
                    AND done_cs.syllabus_id = c.syllabus_id AND done_cs.teacher_id = ?
                JOIN confirmations done_class ON done_class.id = done.confirmation_id
                WHERE done.topic_id = t.id AND done.status = 'Completed'
                  AND NOT EXISTS (
                    SELECT 1 FROM makeup_obligations m
                    WHERE m.session_id = done_class.session_id AND m.original_date = done_class.class_date
                  )
            )
              AND NOT EXISTS (
                  SELECT 1 FROM makeup_obligations m
                  WHERE m.session_id = active_class.session_id AND m.original_date = active_class.class_date
              )
        """, (teacher_id, int(syllabus_id), teacher_id)).fetchone()[0]
        planned = conn.execute("""
            SELECT COUNT(DISTINCT t.id) FROM syllabus_topics t
            JOIN syllabus_chapters c ON c.id = t.chapter_id
            JOIN class_topic_assignments a ON a.topic_id = t.id AND a.status = 'Planned'
            JOIN class_syllabuses cs ON cs.confirmation_id = a.confirmation_id
                AND cs.syllabus_id = c.syllabus_id AND cs.teacher_id = ?
            JOIN confirmations active_class ON active_class.id = a.confirmation_id
            WHERE c.syllabus_id = ? AND NOT EXISTS (
                SELECT 1 FROM class_topic_assignments done
                JOIN class_syllabuses done_cs ON done_cs.confirmation_id = done.confirmation_id
                    AND done_cs.syllabus_id = c.syllabus_id AND done_cs.teacher_id = ?
                JOIN confirmations done_class ON done_class.id = done.confirmation_id
                WHERE done.topic_id = t.id AND done.status IN ('Completed', 'Partially covered')
                  AND NOT EXISTS (
                    SELECT 1 FROM makeup_obligations m
                    WHERE m.session_id = done_class.session_id AND m.original_date = done_class.class_date
                  )
            )
              AND NOT EXISTS (
                  SELECT 1 FROM makeup_obligations m
                  WHERE m.session_id = active_class.session_id AND m.original_date = active_class.class_date
              )
        """, (teacher_id, int(syllabus_id), teacher_id)).fetchone()[0]
    total, completed, partial, planned = map(int, (total, completed, partial, planned))
    return {
        "total_topics": total,
        "completed_topics": completed,
        "partially_covered_topics": partial,
        "planned_topics": planned,
        "remaining_topics": max(0, total - completed),
        "percent_complete": (100.0 * completed / total) if total else 0.0,
    }
