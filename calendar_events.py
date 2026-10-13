"""Build iCalendar resources without network access or application side effects.

The main UID is stable across edits. Daily snoozes use a separate single event
and an EXDATE on the original series, so tomorrow keeps the original clock time.
"""
from datetime import datetime, timedelta, timezone
import re

from parser import as_local, local_now


RECURRENCE_RULES = {
    "daily": "FREQ=DAILY",
    "weekly": "FREQ=WEEKLY",
    "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "monthly": "FREQ=MONTHLY",
}


def event_uid(task_id: str) -> str:
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise ValueError("事项编号无效。")
    return f"memo-{task_id}@diy-memo.local"


def snooze_event_uid(task_id: str, revision: int) -> str:
    event_uid(task_id)
    return f"memo-{task_id}-snooze-{int(revision)}@diy-memo.local"


def followup_event_uid(task_id: str, revision: int, index: int) -> str:
    """A stable UID per approved schedule revision and zero-based follow-up."""
    event_uid(task_id)
    if int(revision) < 0 or int(index) < 0:
        raise ValueError("跟进提醒版本或编号无效。")
    return f"memo-{task_id}-followup-{int(revision)}-{int(index)}@diy-memo.local"


def _minutes(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}必须是非负整数分钟。")
    return value


def followup_offsets(task: dict) -> list[int]:
    """Repeating tasks deliberately never produce an unbounded follow-up set."""
    if task.get("recurrence"):
        return []
    offsets = (task.get("smart") or {}).get("followup_offsets_minutes", [])
    if not isinstance(offsets, list):
        raise ValueError("跟进提醒间隔格式无效。")
    if len(offsets) > 3:
        raise ValueError("一次事项最多安排3次额外跟进。")
    result = [_minutes(value, "跟进提醒间隔") for value in offsets]
    if any(value == 0 for value in result):
        raise ValueError("跟进提醒必须安排在事件时间之后。")
    if any(value > 10080 for value in result):
        raise ValueError("额外跟进只能安排在事件后7天内。")
    if len(set(result)) != len(result):
        raise ValueError("跟进提醒间隔不能重复。")
    return result


def event_title(task: dict) -> str:
    text = str(task.get("text", ""))
    first_line = text.replace("\r\n", "\n").replace("\r", "\n").split("\n", 1)[0]
    return str((task.get("smart") or {}).get("title") or first_line)


def escape_text(value: str) -> str:
    value = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return (value.replace("\\", "\\\\").replace("\n", "\\n")
            .replace(";", "\\;").replace(",", "\\,"))


def fold_line(line: str) -> str:
    """Fold at 75 UTF-8 octets, including the continuation's leading space."""
    pieces, current, octets = [], [], 0
    for character in line:
        size = len(character.encode("utf-8"))
        if octets + size > 75:
            pieces.append("".join(current))
            current, octets = [" "], 1
        current.append(character)
        octets += size
    pieces.append("".join(current))
    return "\r\n".join(pieces)


def _date(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return as_local(parsed).replace(microsecond=0)


def _utc(value: str | datetime) -> str:
    return _date(value).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _local_calendar_time(value: str | datetime) -> str:
    return _date(value).strftime("%Y%m%dT%H%M%S")


def _shanghai_timezone_lines() -> list[str]:
    """Fixed UTC+8 definition for this app's future Shanghai schedules."""
    return [
        "BEGIN:VTIMEZONE", "TZID:Asia/Shanghai", "X-LIC-LOCATION:Asia/Shanghai",
        "BEGIN:STANDARD", "DTSTART:19700101T000000", "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800", "TZNAME:CST", "END:STANDARD", "END:VTIMEZONE",
    ]


def _build(task: dict, uid: str, due: str | datetime, recurrence: str | None,
           now: datetime | None, title: str | None = None,
           description: str | None = None, before_minutes: int | None = None) -> str:
    text = str(task.get("text", "")) if description is None else description
    title = event_title(task) if title is None else title
    if recurrence and recurrence not in RECURRENCE_RULES:
        raise ValueError("不支持这种日历重复规则。")
    if before_minutes is None:
        before_minutes = _minutes((task.get("smart") or {}).get("remind_before_minutes", 0), "提前提醒间隔")
    trigger = "PT0S" if before_minutes == 0 else f"-PT{before_minutes}M"
    calendar = task.get("calendar") or {}
    revision = int(calendar.get("revision", task.get("revision", 0)))
    if revision < 0:
        raise ValueError("日历事件版本无效。")
    start = _date(due)
    # Recurrence BYDAY/BYMONTHDAY must be calculated in the user's timezone.
    # Otherwise Shanghai 06:00 is the previous UTC date and weekday/monthly
    # reminders drift. Keep legacy daily resources in their original UTC form.
    local_recurrence = (recurrence in ("weekdays", "monthly")
                        or bool(recurrence and task.get("smart")))
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//DIY Memo//iCloud Calendar//ZH",
        "CALSCALE:GREGORIAN",
    ]
    if local_recurrence:
        lines.extend(_shanghai_timezone_lines())
    lines.extend([
        "BEGIN:VEVENT", f"UID:{uid}",
        f"DTSTAMP:{_utc(now or local_now())}", f"SEQUENCE:{revision}",
    ])
    if local_recurrence:
        lines.extend([
            f"DTSTART;TZID=Asia/Shanghai:{_local_calendar_time(start)}",
            f"DTEND;TZID=Asia/Shanghai:{_local_calendar_time(start + timedelta(minutes=5))}",
        ])
    else:
        lines.extend([f"DTSTART:{_utc(start)}", f"DTEND:{_utc(start + timedelta(minutes=5))}"])
    lines.extend([
        f"SUMMARY:{escape_text(title)}", f"DESCRIPTION:{escape_text(text)}",
        "STATUS:CONFIRMED", "TRANSP:TRANSPARENT",
    ])
    if recurrence:
        lines.append("RRULE:" + RECURRENCE_RULES[recurrence])
        formatter = _local_calendar_time if local_recurrence else _utc
        exclusions = sorted({formatter(value) for value in calendar.get("exdates", [])})
        if exclusions:
            lines.append(("EXDATE;TZID=Asia/Shanghai:" if local_recurrence else "EXDATE:") + ",".join(exclusions))
    lines.extend([
        "BEGIN:VALARM", "ACTION:DISPLAY", f"TRIGGER:{trigger}",
        f"DESCRIPTION:{escape_text(title or '备忘录提醒')}",
        "END:VALARM", "END:VEVENT", "END:VCALENDAR",
    ])
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"


def build_event(task: dict, now: datetime | None = None) -> str:
    """Return a complete VCALENDAR for the task's primary CalDAV resource."""
    calendar = task.get("calendar") or {}
    recurrence = task.get("recurrence")
    due = (calendar.get("series_start") or task.get("daily_time") or task.get("due")) if recurrence else task.get("due")
    if not due:
        raise ValueError("事项缺少日历提醒时间。")
    return _build(task, event_uid(task["id"]), due, recurrence, now)


def build_snooze_event(task: dict, now: datetime | None = None) -> str:
    """Return the separate, non-repeating snooze event for a daily task."""
    calendar = task.get("calendar") or {}
    uid, due = calendar.get("snooze_uid"), calendar.get("snooze_due")
    if not uid or not due:
        raise ValueError("事项没有待安排的稍后提醒。")
    expected = snooze_event_uid(task["id"], calendar.get("revision", 0))
    if uid != expected:
        raise ValueError("稍后提醒编号与版本不一致。")
    return _build(task, uid, due, None, now, before_minutes=0)


def build_followup_event(task: dict, index: int, now: datetime | None = None) -> str:
    """Build one finite, opt-in single event; its reminder is at its own start."""
    offsets = followup_offsets(task)
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(offsets):
        raise ValueError("找不到这次跟进提醒。")
    smart = task.get("smart") or {}
    base = smart.get("event_at") or task.get("due")
    if not base:
        raise ValueError("跟进提醒缺少原事件时间。")
    due = _date(base) + timedelta(minutes=offsets[index])
    revision = (task.get("calendar") or {}).get("revision", task.get("revision", 0))
    uid = followup_event_uid(task["id"], revision, index)
    title = "待办跟进：" + event_title(task)
    description = (str(task.get("text", "")) + "\n\n这是已确认启用的待办跟进提醒。"
                   "如果事项已完成，请在电脑备忘录中标记完成，并确认同步取消成功，"
                   "以取消后续日历提醒。")
    return _build(task, uid, due, None, now, title=title,
                  description=description, before_minutes=0)
