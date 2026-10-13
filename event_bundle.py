"""Expand reviewed finite event series into independent reminder plans.

Pure data only: no UI, files, credentials, API, or calendar writes. The old nine
plan fields are preserved and each result also carries ``source`` metadata.
``occurrence_index`` is one-based, matching the number shown to the user.
"""
from __future__ import annotations

import copy
from calendar import monthrange
from datetime import datetime, timedelta

from parser import as_local, local_now


PLAN_FIELDS = (
    "title", "event_at", "recurrence", "remind_before_minutes", "priority",
    "followup_offsets_minutes", "questions", "assumptions", "reason",
)
MAX_SERIES_COUNT = 100
MAX_BUNDLE_EVENTS = 200
REPEATING = {"daily", "weekly", "weekdays", "monthly"}


class EventExpansionError(ValueError):
    """A safe, user-visible expansion error; no event text is echoed."""


def plan_fields(plan: dict) -> dict:
    """Strip provenance before passing a result to the legacy plan validator."""
    try:
        return {key: copy.deepcopy(plan[key]) for key in PLAN_FIELDS}
    except (KeyError, TypeError):
        raise EventExpansionError("事项方案缺少必要字段，请重新核对。") from None


def _at(value: str | datetime, label: str) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() != timedelta(hours=8):
            raise ValueError
        return as_local(result).replace(microsecond=0)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise EventExpansionError(f"{label}需要完整的上海时间（+08:00）。") from None


def _year_after(start: datetime) -> datetime:
    try:
        day = min(start.day, monthrange(start.year + 1, start.month)[1])
        return start.replace(year=start.year + 1, day=day)
    except (ValueError, OverflowError):
        raise EventExpansionError("事项年份超出支持范围。") from None


def _next_occurrence(start: datetime, previous: datetime, recurrence: str) -> datetime:
    try:
        if recurrence == "daily":
            return previous + timedelta(days=1)
        if recurrence == "weekly":
            return previous + timedelta(days=7)
        if recurrence == "weekdays":
            candidate = previous + timedelta(days=1)
            while candidate.weekday() >= 5:
                candidate += timedelta(days=1)
            return candidate
        if recurrence == "monthly":
            year, month = previous.year, previous.month
            while True:
                month += 1
                if month == 13:
                    year, month = year + 1, 1
                # Keep the original day number. Jan 31 skips February; it
                # never turns into an ongoing series on the 28th.
                if start.day <= monthrange(year, month)[1]:
                    return start.replace(year=year, month=month)
    except (ValueError, OverflowError):
        raise EventExpansionError("重复事项日期超出支持范围。") from None
    raise EventExpansionError("不支持这种重复事项规则。")


def _exceptions(event: dict) -> dict[datetime, str]:
    values = event.get("exceptions", [])
    if not isinstance(values, list):
        raise EventExpansionError("单次例外格式无效，请重新核对。")
    result = {}
    for exception in values:
        if not isinstance(exception, dict) or set(exception) != {"occurrence_at", "title"}:
            raise EventExpansionError("单次例外需要原发生时间和替换标题。")
        occurrence = _at(exception["occurrence_at"], "例外发生时间")
        title = exception["title"]
        if not isinstance(title, str) or not title.strip():
            raise EventExpansionError("单次例外缺少替换标题。")
        if occurrence in result:
            raise EventExpansionError("同一次日程不能设置两个标题例外。")
        result[occurrence] = title.strip()
    return result


def _result(base: dict, series_id: str, occurrence: datetime | None,
            index: int, expanded: bool, title: str | None = None,
            numbering_start: int = 1) -> dict:
    plan = copy.deepcopy(base)
    metadata = plan.pop("_meta", {})
    if occurrence is not None:
        plan["event_at"] = occurrence.isoformat(timespec="seconds")
    if expanded:
        plan["recurrence"] = "none"
    if title is not None:
        plan["title"] = title
    plan["source"] = {
        "series_id": series_id,
        "occurrence_index": index,
        "occurrence_at": plan["event_at"],
        "series_title": base["title"],
        "series_recurrence": base["recurrence"],
        "expanded": expanded,
        "title_overridden": title is not None,
        "numbering_start": numbering_start,
        "source_text": metadata.get("source_text", ""),
        "relation": copy.deepcopy(metadata.get("relation")),
        "reference_occurrence_at": plan["event_at"],
    }
    return plan


def expand_event(event: dict, now: datetime | None = None) -> list[dict]:
    """Expand a finite series; count includes its first occurrence, until is inclusive.

    Count is at most 100. An until date is at most one calendar year after the
    first occurrence. When both are supplied, whichever limit comes first wins.
    Infinite series without exceptions remain one ordinary recurring plan.
    This is an expansion planner, not the model-response schema validator.
    """
    if not isinstance(event, dict):
        raise EventExpansionError("事项方案格式无效。")
    base = plan_fields(event)
    base["_meta"] = {"source_text": event.get("source_text", ""), "relation": event.get("relation")}
    series_id = event.get("id")
    if not isinstance(series_id, str) or not series_id.strip():
        raise EventExpansionError("事项系列缺少稳定编号。")
    series_id = series_id.strip()
    recurrence = base["recurrence"]
    if recurrence not in ({"none"} | REPEATING):
        raise EventExpansionError("不支持这种重复事项规则。")
    count = event.get("recurrence_count", event.get("count"))
    if count is not None and (type(count) is not int or not 1 <= count <= MAX_SERIES_COUNT):
        raise EventExpansionError("有限重复次数需要在1到100次之间。")
    until_value = event.get("recurrence_until", event.get("until"))
    if count is not None and until_value is not None:
        raise EventExpansionError("重复次数与结束时间应只选择一种。")
    exceptions = _exceptions(event)
    numbering_start = event.get("numbering_start")
    if numbering_start is None:
        numbering_start = 1
    if type(numbering_start) is not int or not 1 <= numbering_start <= 100:
        raise EventExpansionError("场次编号起点需要在1到100之间。")
    if recurrence == "none" and numbering_start != 1:
        raise EventExpansionError("不重复事项不能设置场次编号起点。")
    if base["event_at"] is None:
        if recurrence != "none" or count not in (None, 1) or until_value is not None or exceptions:
            raise EventExpansionError("请先确认这组日程的起始日期和时间。")
        return [_result(base, series_id, None, 1, False, numbering_start=numbering_start)]
    start = _at(base["event_at"], "事件时间")
    current = as_local(now or local_now())
    if start <= current:
        raise EventExpansionError("首个事件时间已经过去，请修改起始日期。")
    if recurrence == "weekdays" and start.weekday() >= 5:
        raise EventExpansionError("工作日系列的首场必须在周一至周五，不能从周末开始。")
    until = _at(until_value, "重复结束时间") if until_value is not None else None
    if until is not None and (until < start or until > _year_after(start)):
        raise EventExpansionError("结束时间应不早于首场，且距离首场不超过一年。")
    if recurrence == "none":
        if count not in (None, 1):
            raise EventExpansionError("不重复事项不能设置多次发生。")
        if any(occurrence != start for occurrence in exceptions):
            raise EventExpansionError("单次例外时间不属于这个事项。")
        return [_result(base, series_id, start, 1, False, exceptions.get(start),
                        numbering_start=numbering_start)]
    finite = count is not None or until is not None
    if not finite:
        if exceptions:
            raise EventExpansionError("暂不支持无限重复系列的单次例外，请明确重复次数或结束日期。")
        return [_result(base, series_id, start, 1, False, numbering_start=numbering_start)]
    occurrences = []
    candidate = start
    while True:
        if until is not None and candidate > until:
            break
        if count is not None and len(occurrences) >= count:
            break
        if len(occurrences) >= MAX_SERIES_COUNT:
            raise EventExpansionError("这组日程超过100次，请缩短结束日期或减少次数。")
        occurrences.append(candidate)
        # Do not compute another occurrence beyond an already-reached count.
        if count is not None and len(occurrences) >= count:
            break
        candidate = _next_occurrence(start, candidate, recurrence)
    missing = set(exceptions) - set(occurrences)
    if missing:
        raise EventExpansionError("例外时间不在这组日程的发生范围内，请核对日期和重复规则。")
    return [_result(base, series_id, occurrence, index, True, exceptions.get(occurrence),
                    numbering_start=numbering_start)
            for index, occurrence in enumerate(occurrences, start=1)]


def expand_bundle(events: list[dict], now: datetime | None = None) -> list[dict]:
    """Expand at most 200 primary events; callers must confirm the whole preview."""
    if not isinstance(events, list) or not events:
        raise EventExpansionError("请先提供需要核对的事项列表。")
    series_ids = [event.get("id") for event in events if isinstance(event, dict)]
    if len(series_ids) != len(events) or len(set(series_ids)) != len(series_ids):
        raise EventExpansionError("事项系列编号缺失或重复，请重新识别。")
    current = as_local(now or local_now())
    result = []
    for event in events:
        expanded = expand_event(event, current)
        if len(result) + len(expanded) > MAX_BUNDLE_EVENTS:
            raise EventExpansionError("这批日程展开后超过200条，请分批安排。")
        result.extend(expanded)
    return result


def contains_occurrence(event: dict, occurrence: str | datetime) -> bool:
    """Check a dated reference without expanding an infinite series."""
    start, candidate = _at(event["event_at"], "起始时间"), _at(occurrence, "关联场次")
    if candidate < start:
        return False
    finite = event.get("count") is not None or event.get("until") is not None
    if finite:
        return any(_at(item["event_at"], "场次") == candidate for item in expand_event(event, start - timedelta(seconds=1)))
    if event["recurrence"] == "none":
        return candidate == start
    if candidate.time() != start.time():
        return False
    days = (candidate.date() - start.date()).days
    return {"daily": True, "weekly": days % 7 == 0,
            "weekdays": candidate.weekday() < 5, "monthly": candidate.day == start.day}.get(event["recurrence"], False)
