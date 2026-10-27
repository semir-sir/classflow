import json
from pathlib import Path
from datetime import datetime, date, timedelta

def load_timetable(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    entries = data.get("classes", [])
    for item in entries:
        item["duration_minutes"] = minutes_between(item["start"], item["end"])
    return entries

def minutes_between(start, end):
    fmt = "%H:%M"
    a = datetime.strptime(start, fmt)
    b = datetime.strptime(end, fmt)
    delta = int((b - a).total_seconds() // 60)
    if delta <= 0:
        raise ValueError(f"Invalid class interval: {start}–{end}")
    return delta

def get_regular_sessions(schedule):
    return [s for s in schedule if s.get("course_type") == "system_analysis_design"]

def most_recent_occurrence_date(day, today):
    """Return the latest calendar date on or before today matching a timetable weekday."""
    weekdays = {name: index for index, name in enumerate((
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
    ))}
    if day not in weekdays:
        raise ValueError(f"Unknown timetable weekday: {day}")
    if isinstance(today, str):
        today = date.fromisoformat(today)
    return today - timedelta(days=(today.weekday() - weekdays[day]) % 7)

def confirmation_matches_timetable_date(confirmation):
    """Reject legacy confirmation rows saved against the wrong calendar weekday."""
    try:
        saved_date = date.fromisoformat(confirmation["class_date"])
    except (KeyError, TypeError, ValueError):
        return False
    return saved_date.strftime("%A") == confirmation.get("day")

def find_conflicts(schedule, section, day, start, end, exclude_session_id=None):
    proposed_start = minutes_since_midnight(start)
    proposed_end = minutes_since_midnight(end)
    conflicts = []
    for item in schedule:
        if item["section"] != section or item["day"] != day:
            continue
        if exclude_session_id and item["id"] == exclude_session_id:
            continue
        existing_start = minutes_since_midnight(item["start"])
        existing_end = minutes_since_midnight(item["end"])
        if proposed_start < existing_end and existing_start < proposed_end:
            conflicts.append(item)
    return sorted(conflicts, key=lambda c: c["start"])

def minutes_since_midnight(value):
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute
