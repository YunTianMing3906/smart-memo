"""Single-request DeepSeek reminder interpretation, using Python stdlib only.

Importing this module does not read credentials or contact any service. The
caller explicitly invokes load() / interpret(). No user content or keys are
printed or logged. Interpretation never writes a calendar or schedules a task.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import ssl
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from icloud_client import ICloudError, _dpapi
from app_paths import app_dir


API_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_SETTINGS_PATH = app_dir() / "deepseek-credentials.dpapi.json"
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_INPUT_CHARS = 10000
PLAN_FIELDS = {
    "title", "event_at", "recurrence", "remind_before_minutes", "priority",
    "followup_offsets_minutes", "questions", "assumptions", "reason",
}
BUNDLE_FIELDS = {"events", "questions", "assumptions", "reason"}
EVENT_FIELDS = {"id", "title", "event_at", "recurrence", "remind_before_minutes",
                "priority", "followup_offsets_minutes", "count", "until", "exceptions",
                "numbering_start"}
EVENT_OPTIONAL_FIELDS = {"event_date", "source_text", "relation"}
REPEATING_RULES = {"daily", "weekly", "weekdays", "monthly"}
MAX_BUNDLE_EVENTS = 12
MAX_EVENT_EXCEPTIONS = 10
ISO_PATTERN = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?\+08:00"


class DeepSeekError(Exception):
    """Safe-to-display error without keys, URLs, model output or input text."""

    def __init__(self, message: str, *, code: str = "deepseek_error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class PlanValidationError(DeepSeekError):
    def __init__(self, message: str = "AI 返回的提醒方案格式不完整或不符合限制，请修改输入后自行重试。"):
        super().__init__(message, code="invalid_plan")


def _model_name(model: str) -> str:
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", model.strip()):
        raise DeepSeekError("模型名称无效，请填写 DeepSeek 官方支持的模型名称。", code="invalid_model")
    return model.strip()


def _key(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or not value.strip().isascii() or any(char.isspace() for char in value.strip()):
        raise DeepSeekError("请填写有效的 DeepSeek API Key。", code="invalid_api_key")
    return value.strip()


def _text(value: Any, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise PlanValidationError()
    value = value.strip()
    if not minimum <= len(value) <= maximum or any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise PlanValidationError()
    return value


def _integer(value: Any, minimum: int, maximum: int) -> int:
    # bool is an int subclass; accepting True as "1 minute" would hide bad output.
    if type(value) is not int or not minimum <= value <= maximum:
        raise PlanValidationError()
    return value


def _strings(value: Any, maximum_items: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise PlanValidationError()
    return [_text(item, 1, 300) for item in value]


def _offsets(value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) > 3:
        raise PlanValidationError()
    result = [_integer(item, 1, 10080) for item in value]
    if result != sorted(set(result)):
        raise PlanValidationError()
    return result


def _now(now: datetime | int | float | str | None = None) -> datetime:
    if now is None:
        return datetime.now(SHANGHAI)
    try:
        if isinstance(now, str):
            value = datetime.fromisoformat(now)
        elif isinstance(now, datetime):
            value = now
        elif type(now) in (int, float):
            value = datetime.fromtimestamp(now, SHANGHAI)  # Unix seconds, not milliseconds.
        else:
            raise ValueError
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI)
        return value.astimezone(SHANGHAI)
    except (ValueError, TypeError, OverflowError, OSError):
        raise DeepSeekError("当前时间参数无效。", code="invalid_now") from None


def normalize_preferences(preferences: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Send only recognized reminder preferences, never arbitrary app settings."""
    if preferences is None:
        return {}
    if not isinstance(preferences, Mapping):
        raise DeepSeekError("提醒偏好需要使用配置对象。", code="invalid_preferences")
    result: dict[str, Any] = {}
    try:
        for name in ("guidance", "notes"):
            if name in preferences:
                result[name] = _text(preferences[name], 0, 1000)
        if "default_event_time" in preferences:
            value = preferences["default_event_time"]
            if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                raise PlanValidationError()
            result["default_event_time"] = value
        if "default_remind_before_minutes" in preferences:
            result["default_remind_before_minutes"] = _integer(preferences["default_remind_before_minutes"], 0, 10080)
        if "default_priority" in preferences:
            value = preferences["default_priority"]
            if value not in ("low", "normal", "high"):
                raise PlanValidationError()
            result["default_priority"] = value
        if "followup_offsets_minutes" in preferences:
            result["followup_offsets_minutes"] = _offsets(preferences["followup_offsets_minutes"])
        if "quiet_hours" in preferences:
            value = preferences["quiet_hours"]
            if value is None:
                result["quiet_hours"] = None
            elif isinstance(value, str):
                result["quiet_hours"] = _text(value, 0, 100)
            elif isinstance(value, Mapping) and set(value) == {"start", "end"}:
                integer_hours = all(type(item) is int and 0 <= item <= 23 for item in value.values())
                clock_strings = all(isinstance(item, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item) for item in value.values())
                if not integer_hours and not clock_strings:
                    raise PlanValidationError()
                result["quiet_hours"] = dict(value)
            else:
                raise PlanValidationError()
    except (PlanValidationError, TypeError):
        raise DeepSeekError("提醒偏好格式无效，请检查时间、分钟数和跟进次数。", code="invalid_preferences") from None
    return result


def validate_plan(data: Any, now: datetime | int | float | str | None = None, preferences: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate the complete plan contract; return a new normalized dictionary.

    `now`, if supplied, is used to reject a past event. `preferences` is validated
    for consistency but does not silently invent or overwrite plan fields.
    """
    if not isinstance(data, dict) or set(data) != PLAN_FIELDS:
        raise PlanValidationError()
    normalize_preferences(preferences)
    title = _text(data["title"], 1, 200)
    recurrence = data["recurrence"]
    priority = data["priority"]
    if recurrence not in ("none", "daily", "weekly", "weekdays", "monthly") or priority not in ("low", "normal", "high"):
        raise PlanValidationError()
    questions = _strings(data["questions"], 6)
    assumptions = _strings(data["assumptions"], 8)
    event_at = data["event_at"]
    if event_at is None:
        if not questions:
            raise PlanValidationError()
    else:
        if questions or not isinstance(event_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?\+08:00", event_at):
            raise PlanValidationError()
        try:
            event = datetime.fromisoformat(event_at)
            if event.utcoffset() != timedelta(hours=8):
                raise ValueError
        except ValueError:
            raise PlanValidationError() from None
        if now is not None and event <= _now(now):
            raise PlanValidationError("AI 识别出的时间已经过去，请补充未来的日期或时间后重试。")
        if recurrence == "weekdays" and event.weekday() >= 5:
            raise PlanValidationError("工作日事项的首个日期应为周一至周五，请补充起始日期后重试。")
        event_at = event.isoformat(timespec="seconds")
    return {
        "title": title,
        "event_at": event_at,
        "recurrence": recurrence,
        "remind_before_minutes": _integer(data["remind_before_minutes"], 0, 10080),
        "priority": priority,
        "followup_offsets_minutes": _offsets(data["followup_offsets_minutes"]),
        "questions": questions,
        "assumptions": assumptions,
        "reason": _text(data["reason"], 1, 800),
    }


def _iso_time(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(ISO_PATTERN, value):
        raise PlanValidationError()
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() != timedelta(hours=8):
            raise ValueError
        return result
    except ValueError:
        raise PlanValidationError() from None


def validate_bundle(data: Any, now: datetime | int | float | str | None = None,
                    preferences: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate the multi-event bundle contract; return a normalized bundle.

    Each event is also completed with the bundle-level questions/assumptions/
    reason so the legacy plan validator and the expansion planner can consume it.
    """
    if not isinstance(data, dict) or set(data) != BUNDLE_FIELDS:
        raise PlanValidationError()
    normalize_preferences(preferences)
    questions = _strings(data["questions"], 6)
    assumptions = _strings(data["assumptions"], 8)
    reason = _text(data["reason"], 1, 800)
    events = data["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_BUNDLE_EVENTS:
        raise PlanValidationError()
    current = _now(now) if now is not None else None
    seen_ids = set()
    normalized = []
    for event in events:
        if not isinstance(event, dict) or not EVENT_FIELDS <= set(event) or set(event) - EVENT_FIELDS - EVENT_OPTIONAL_FIELDS:
            raise PlanValidationError()
        series_id = event["id"]
        if not isinstance(series_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", series_id) or series_id in seen_ids:
            raise PlanValidationError()
        seen_ids.add(series_id)
        recurrence = event["recurrence"]
        priority = event["priority"]
        if recurrence not in ({"none"} | REPEATING_RULES) or priority not in ("low", "normal", "high"):
            raise PlanValidationError()
        count = event["count"]
        if count is not None and (type(count) is not int or not 1 <= count <= 100):
            raise PlanValidationError()
        until = event["until"]
        if until is not None:
            until = _iso_time(until).isoformat(timespec="seconds")
        if count is not None and until is not None:
            raise PlanValidationError("重复次数与结束时间不能同时指定，请保留一种明确的结束方式。")
        exceptions = event["exceptions"]
        if not isinstance(exceptions, list) or len(exceptions) > MAX_EVENT_EXCEPTIONS:
            raise PlanValidationError()
        normalized_exceptions = []
        for exception in exceptions:
            if not isinstance(exception, dict) or set(exception) != {"occurrence_at", "title"}:
                raise PlanValidationError()
            occurrence = _iso_time(exception["occurrence_at"])
            normalized_exceptions.append({"occurrence_at": occurrence.isoformat(timespec="seconds"),
                                          "title": _text(exception["title"], 1, 200)})
        offsets = _offsets(event["followup_offsets_minutes"])
        if recurrence != "none" and offsets:
            raise PlanValidationError()
        numbering_start = event["numbering_start"]
        if numbering_start is None:
            numbering_start = 1
        elif type(numbering_start) is not int or not 1 <= numbering_start <= 100:
            raise PlanValidationError()
        if recurrence == "none" and numbering_start != 1:
            raise PlanValidationError()
        if recurrence == "none" and (count is not None or until is not None or exceptions):
            raise PlanValidationError()
        event_at = event["event_at"]
        event_date = event.get("event_date")
        if event_date is not None:
            try:
                if not isinstance(event_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", event_date):
                    raise ValueError
                datetime.strptime(event_date, "%Y-%m-%d")
            except ValueError:
                raise PlanValidationError("事项日期格式无效。") from None
        if event_at is None:
            pass
        else:
            parsed = _iso_time(event_at)
            if recurrence == "weekdays" and parsed.weekday() >= 5:
                raise PlanValidationError("工作日事项的首个日期应为周一至周五，请补充起始日期后重试。")
            if current is not None and parsed <= current:
                raise PlanValidationError("AI 识别出的时间已经过去，请补充未来的日期或时间后重试。")
            event_at = parsed.isoformat(timespec="seconds")
            if event_date is not None and parsed.date().isoformat() != event_date:
                raise PlanValidationError("事项日期与具体时间不一致。")
            event_date = parsed.date().isoformat()
        relation = event.get("relation")
        if relation is not None:
            if not isinstance(relation, dict) or set(relation) != {"event_id", "occurrence_at", "offset_days"}:
                raise PlanValidationError("关联日期需要来源事项、具体场次和天数偏移。")
            relation = {"event_id": _text(relation["event_id"], 1, 20),
                        "occurrence_at": _iso_time(relation["occurrence_at"]).isoformat(timespec="seconds"),
                        "offset_days": _integer(relation["offset_days"], -365, 365)}
        normalized.append({
            "id": series_id,
            "title": _text(event["title"], 1, 200),
            "event_at": event_at,
            "recurrence": recurrence,
            "remind_before_minutes": _integer(event["remind_before_minutes"], 0, 10080),
            "priority": priority,
            "followup_offsets_minutes": offsets,
            "count": count,
            "until": until,
            "exceptions": normalized_exceptions,
            "numbering_start": numbering_start,
            "questions": [],
            "assumptions": assumptions,
            "reason": reason,
            "event_date": event_date,
            "source_text": _text(event.get("source_text", ""), 0, 1500),
            "relation": relation,
        })
    has_missing = any(item["event_at"] is None for item in normalized)
    has_concrete = any(item["event_at"] is not None for item in normalized)
    if has_missing and not questions:
        raise PlanValidationError()
    if questions and not has_missing:
        raise PlanValidationError()
    from event_bundle import expand_event, contains_occurrence
    by_id = {event["id"]: event for event in normalized}
    for event in normalized:
        if event["event_at"] is None:
            event["questions"] = questions
        else:
            try:
                expand_event(event, current)
            except ValueError as exc:
                raise PlanValidationError(str(exc)) from None
        relation = event["relation"]
        if relation:
            target = by_id.get(relation["event_id"])
            if target is None or target is event or target["event_at"] is None:
                raise PlanValidationError("关联的日程不存在或日期尚未明确。")
            occurrence = _iso_time(relation["occurrence_at"])
            if not contains_occurrence(target, occurrence):
                raise PlanValidationError("关联日期不属于来源日程的有效场次。")
            derived = (occurrence + timedelta(days=relation["offset_days"])).date().isoformat()
            if event["event_date"] and event["event_date"] != derived:
                raise PlanValidationError("关联事项日期与指定场次的天数偏移不符。")
            event["event_date"] = derived
    return {"events": normalized, "questions": questions, "assumptions": assumptions, "reason": reason}


SYSTEM_PROMPT = """你是中文日程规划助手，把当前记录完整拆成多个独立事项，返回一个JSON对象。
只输出JSON，不输出Markdown、思维链、代码或任何执行指令。不得访问账户、邮件、位置或历史事项。
根对象严格为events、questions、assumptions、reason。events最多12个逻辑事项；重复系列只返回一个模板，不逐条枚举。
每个event包含id,title,event_at,event_date,recurrence,remind_before_minutes,priority,followup_offsets_minutes,count,until,exceptions,numbering_start,source_text,relation。
id是全批唯一短编号；title保留用户的真实行动和关键词，不合并不同事项。source_text复制这个事项对应的原文片段。
event_at为完整ISO8601上海时间+08:00或null，event_date为YYYY-MM-DD或null。知道日期但没有钟点时保留event_date，event_at:null，并提问几点。
recurrence只能为none/daily/weekly/weekdays/monthly；priority为low/normal/high。提前量0..10080整数分钟。跟进为最多3个严格递增整数偏移1..10080，仅一次事项使用；未请求跟进时用[]。
count是有限重复总次数，包含首场，最多100。接下来10周每周一次必须count:10，不能返回无限重复。until是含末场的完整结束时间或null；count和until只能选一种。
numbering_start默认1，只有用户明确编号起点时才改变；编号不能改变日期、总次数或标题。
exceptions用于某次场次单独改变标题，每项为{occurrence_at:原场次上海时间,title:替换标题}。该场仍只存在一次，不另外生成同时间会议；有限系列支持例外，无限系列有例外时先问结束范围。
relation为null或{event_id:来源事项id,occurrence_at:来源系列中的具体场次,offset_days:前后天数整数}。日期必须从指定场次计算，不能把“那个周一的上周六”理解成当前周的周六。
独立行动必须另建event，例如领取资料不是会议提前两天的VALARM。不能把会议时间自动复制给日期明确但缺少钟点的独立事项。
有一个事项需要澄清时，保留其他已明确事项的日期和时间；questions统一列出需要用户补充的问题。程序会等整批澄清后才保存，不要遗漏未明确事项。
用户明确时间和规则优先。默认偏好可以建议提前量，但首次提醒必须未来，短期提醒通常提前量0。补齐的假设在assumptions展示。
缺少时间、外部条件触发（收到材料后、审批通过后、出门前）要追问；不能声称检测条件已经实现。补充信息是用户对问题的回答，明确改为固定时间时应采用。
工作日首场在周一至五；每月固定29/30/31号遇到无该日期月份会跳过，需要说明。每月月底或最后一个工作日不能偷偷当作每月31号。
时间以消息提供的当前Asia/Shanghai为基准，不机械复制示例。next/接下来从下一次未来发生开始，明确预览起止及总次数。

反例必须正确拆解：以2026-10-13为当前时间，用户说“接下来的10周，每周一的下午3.都要开例会。然后12.14那一个周一的前两天，也就是那个周一的上周六，提醒我跟负责人领资料，12.14主持例会”。
它包含：10次例会（10/19至12/21每周一15点）；其中12/14第9场标题替换为“主持例会”；12/12周六独立领取资料，钟点未给，必须提问，不能猜9点或继承15点。
示例JSON：
{"events":[{"id":"party","title":"例会","event_at":"2026-10-19T15:00:00+08:00","event_date":"2026-10-19","recurrence":"weekly","remind_before_minutes":0,"priority":"normal","followup_offsets_minutes":[],"count":10,"until":null,"exceptions":[{"occurrence_at":"2026-12-14T15:00:00+08:00","title":"主持例会"}],"numbering_start":1,"source_text":"接下来的10周，每周一的下午3.都要开例会","relation":null},{"id":"materials","title":"跟负责人领资料","event_at":null,"event_date":"2026-12-12","recurrence":"none","remind_before_minutes":0,"priority":"normal","followup_offsets_minutes":[],"count":null,"until":null,"exceptions":[],"numbering_start":1,"source_text":"12.14那一个周一的前两天，也就是那个周一的上周六，提醒我跟负责人领资料","relation":{"event_id":"party","occurrence_at":"2026-12-14T15:00:00+08:00","offset_days":-2}}],"questions":["12月12日周六几点提醒你跟负责人领资料？"],"assumptions":["从下一次周一2026-10-19开始共10次，最后一次2026-12-21。"],"reason":"保留10次会议、其中一次主持例会，以及独立的提前领资料事项；资料领取时间需要你补充。"}
"""

def _conditional_guard(text: str, plan: dict[str, Any]) -> dict[str, Any]:
    # Additional conservative gate: JSON validity alone cannot establish that a
    # model understood an external condition. We don't pretend to watch sensors.
    clarification = text.rsplit("补充信息：", 1)[-1] if "补充信息：" in text else ""
    fixed_choice = bool(clarification and re.search(r"固定|按时间|定时|改成|每天|每周", clarification))
    condition_text = clarification if fixed_choice else text
    conditional = re.search(r"(?:如果|只有|一旦).{0,40}(?:收到|出门|到家|到达|审批|通过)|收到.{0,30}(?:后|时|才)|一收到|(?:出门|到家|回家|到公司|到达)(?:前|后|时)|(?:审批|审核).{0,15}(?:通过|完成).{0,5}(?:后|时)|(?<!没)(?<!未)(?<!不)(?:做完|完成).{0,20}(?:后|再|才)|等.{0,30}(?:回复|答复|材料|图纸).{0,10}(?:再|才|后)", condition_text)
    fixed_month_day = re.search(r"(?:每月(?:的)?|固定(?:为)?(?:每月)?|补充信息[:：])\s*(?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*(?:号|日)", text)
    if plan["event_at"] is not None and plan["recurrence"] == "monthly" and not fixed_month_day and re.search(r"月底|月末|最后.{0,4}工作日", text):
        result = dict(plan)
        result.update(event_at=None, followup_offsets_minutes=[], questions=["每月月底或最后一个工作日需要特殊规则。此版可先按固定每月几号提醒，你想设为几号几点？"], reason="先确认固定日号，避免短月错过提醒。")
        return result
    if plan["event_at"] is not None and conditional:
        result = dict(plan)
        result.update(event_at=None, followup_offsets_minutes=[], questions=["当前日历不能自动检测这个条件。是否改为固定时间检查？请补充‘改成固定……时间提醒’。"], reason="这条记录包含条件触发，需先明确接受固定时间提醒。")
        return result
    return plan


def _conditional_guard_bundle(text: str, bundle: dict[str, Any], preferences=None) -> dict[str, Any]:
    # Same conservative gate, applied per event. One triggered condition means
    # every event waits for the clarification; no schedule is silently saved.
    result = copy.deepcopy(bundle)
    questions = list(result["questions"])
    for event in result["events"]:
        source = event.get("source_text") or text
        clarification = text.rsplit("补充信息：", 1)[-1] if "补充信息：" in text else ""
        guarded = _conditional_guard(source + ("\n补充信息：" + clarification if clarification else ""), event)
        if guarded["questions"]:
            questions.extend(guarded["questions"])
            event["event_at"] = None
        if event["relation"] and event["event_at"] is not None and event.get("source_text"):
            clock = re.search(r"\d{1,2}[:：]\d{2}|[0-9一二三四五六七八九十]{1,3}\s*[点时]|(?:上午|下午|中午|晚上|早上|凌晨)\s*[0-9一二三四五六七八九十]{1,3}(?:[.。]|$)", source + " " + clarification)
            if not clock and not (preferences or {}).get("default_event_time"):
                event["event_at"] = None
                questions.append(f"{event['event_date']} 的“{event['title']}”几点提醒？这个事项不能自动沿用会议时间。")
        if event["recurrence"] == "weekly":
            limit = re.search(r"接下来(?:的)?\s*(\d+|[一二三四五六七八九十]{1,3})\s*周", source)
            if not limit and sum(item["recurrence"] == "weekly" for item in result["events"]) == 1:
                limit = re.search(r"接下来(?:的)?\s*(\d+|[一二三四五六七八九十]{1,3})\s*周", text)
            if limit:
                from parser import chinese_number
                requested = chinese_number(limit[1])
                if event["count"] != requested and event["until"] is None:
                    questions.append(f"“{event['title']}”需要明确限定为{requested}次，当前方案未正确保留这个期限，请重新确认。")
    if questions:
        result["questions"] = list(dict.fromkeys(questions))
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("non-finite JSON value")


def _json(content: str) -> Any:
    return json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class DeepSeekClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        self._api_key = _key(api_key)
        self.model = _model_name(model)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirect(),
        )

    def interpret(self, text: str, now: datetime | int | float | str | None = None, preferences: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT_CHARS:
            raise DeepSeekError("请输入当前这条备忘录，最多10000字。", code="invalid_input")
        current = _now(now)
        prefs = normalize_preferences(preferences)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"current_time": current.isoformat(timespec="seconds"), "timezone": "Asia/Shanghai", "reminder_preferences": prefs, "current_note": text.strip()}, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "stream": False,
            "max_tokens": 6000,
            "temperature": 0.2,
        }
        request = urllib.request.Request(
            API_ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Memo-DeepSeek/1.0"},
            method="POST",
        )
        try:
            # Exactly one paid request. No retry loop, fallback request or redirect.
            response = self._opener.open(request, timeout=45)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()  # Deliberately do not read or echo the error response body.
            messages = {
                401: "DeepSeek API Key 验证失败，请重新检查或更换密钥。",
                402: "DeepSeek 余额不足，请在平台检查账户余额。",
                429: "DeepSeek 请求频率受限，请稍后自行重试。",
            }
            if status in (301, 302, 303, 307, 308):
                raise DeepSeekError("DeepSeek 请求发生跳转，已停止发送以保护密钥。", code="redirect_blocked", status=status) from None
            raise DeepSeekError(messages.get(status, "DeepSeek 暂时无法处理请求，请检查模型名称或稍后自行重试。"), code="http_error", status=status) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise DeepSeekError("无法连接 DeepSeek 或请求超时。没有自动重试，请你决定是否再次请求。", code="connection_failed") from None
        try:
            status = int(response.getcode())
            if status != 200:
                raise DeepSeekError("DeepSeek 返回异常状态，请稍后自行重试。", code="unexpected_status", status=status)
            try:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, TimeoutError):
                raise DeepSeekError("DeepSeek 响应读取失败，没有自动重试。", code="response_failed") from None
        finally:
            response.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DeepSeekError("DeepSeek 响应过大，已停止处理。", code="response_too_large")
        try:
            completion = _json(raw.decode("utf-8"))
            choices = completion["choices"]
            if not isinstance(choices, list) or len(choices) != 1 or choices[0].get("finish_reason") != "stop":
                raise ValueError
            message = choices[0]["message"]
            content = message["content"]
            if message.get("tool_calls") or not isinstance(content, str) or not content.strip():
                raise ValueError
            data = _json(content)
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError, AttributeError):
            raise DeepSeekError("DeepSeek 返回空内容、不完整结果或非JSON格式，没有自动重试。", code="invalid_response") from None
        bundle = validate_bundle(data, now=current, preferences=prefs)
        return _conditional_guard_bundle(text, bundle, prefs)


class AISettingsStore:
    """Separate DPAPI key file; never read/written through memo-data.json."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else DEFAULT_SETTINGS_PATH

    def save(self, api_key: str, model: str = DEFAULT_MODEL, preferences: Mapping[str, Any] | None = None) -> dict[str, Any]:
        key = _key(api_key)
        model = _model_name(model)
        prefs = normalize_preferences(preferences)
        try:
            protected = _dpapi(key.encode("utf-8"), decrypt=False)
        except ICloudError:
            raise DeepSeekError("无法使用 Windows 当前用户加密服务保存 API Key。", code="dpapi_failed") from None
        payload = {"version": 1, "protection": "windows-dpapi-current-user", "api_key_dpapi": base64.b64encode(protected).decode("ascii"), "model": model, "preferences": prefs}
        temp_path = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=self.path.parent, prefix=".deepseek-credential-", delete=False) as handle:
                temp_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except OSError:
            raise DeepSeekError("无法保存本机加密 AI 配置，请检查目录是否可写。", code="settings_write_failed") from None
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
        return {"model": model, "preferences": prefs}

    def load(self) -> dict[str, Any] | None:
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(65537)
        except FileNotFoundError:
            return None
        except OSError:
            raise DeepSeekError("无法读取本机加密 AI 配置。", code="settings_read_failed") from None
        try:
            if len(raw) > 65536:
                raise ValueError
            data = _json(raw.decode("utf-8"))
            if data.get("version") != 1 or data.get("protection") != "windows-dpapi-current-user":
                raise ValueError
            protected = base64.b64decode(data["api_key_dpapi"], validate=True)
            key = _key(_dpapi(protected, decrypt=True).decode("utf-8"))
            model = _model_name(data["model"])
            preferences = normalize_preferences(data.get("preferences", {}))
        except ICloudError:
            raise DeepSeekError("无法解密 API Key，请在原来的 Windows 用户中重新保存。", code="dpapi_failed") from None
        except (ValueError, TypeError, KeyError, UnicodeError, AttributeError):
            raise DeepSeekError("本机 AI 配置无效，请重新保存 API Key。", code="settings_invalid") from None
        return {"api_key": key, "model": model, "preferences": preferences}

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise DeepSeekError("无法移除本机 AI 配置。", code="settings_delete_failed") from None
