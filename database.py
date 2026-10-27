import sqlite3
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
