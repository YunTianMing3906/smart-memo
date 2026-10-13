"""Small, explicit Chinese time parser. No AI, network, or system timezone needed."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


@dataclass(frozen=True)
class ParsedTime:
    due: datetime
    recurrence: str | None = None
    explanation: str = ""


def local_now() -> datetime:
    return datetime.now(SHANGHAI)


def as_local(value: datetime) -> datetime:
    return value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value.astimezone(SHANGHAI)


def chinese_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = dict(zip("零一二三四五六七八九", range(10)))
    digits["两"] = 2
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        if (left and left not in digits) or (right and right not in digits):
            raise ValueError("暂不支持这个中文数字，请改用阿拉伯数字。")
        return (digits[left] if left else 1) * 10 + (digits[right] if right else 0)
    if value in digits:
        return digits[value]
    raise ValueError("暂不支持这个中文数字，请改用阿拉伯数字。")


def _clock(text: str) -> tuple[int, int]:
    match = re.search(r"(?P<period>凌晨|早上|上午|中午|下午|晚上)?\s*(?P<hour>\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*(?::\s*(?P<minute>\d{1,2})|[点时](?:(?P<half>半)|(?P<cminute>\d{1,2}|[零一二两三四五六七八九十]{1,3})分?)?)", text)
    if not match:
        raise ValueError("请写出具体时间，例如“明天14:30”或“周五上午10点”。")
    hour = chinese_number(match["hour"])
    minute = int(match["minute"]) if match["minute"] else 30 if match["half"] else chinese_number(match["cminute"]) if match["cminute"] else 0
    period = match["period"]
    if period and not 1 <= hour <= 12:
        raise ValueError("上午/下午请使用1到12点，或直接写24小时格式。")
    if period in ("下午", "晚上") and hour < 12:
        hour += 12
    elif period in ("凌晨", "早上", "上午") and hour == 12:
        hour = 0
    elif period == "中午" and hour < 11:
        hour += 12
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("时间超出范围，请使用00:00到23:59。")
    return hour, minute


def parse_time_input(text: str, now: datetime | None = None) -> ParsedTime:
    now = as_local(now or local_now())
    text = text.strip().replace("：", ":")
    if not text:
        raise ValueError("请先填写提醒时间。")
    relative = re.search(r"(\d+|[零一二两三四五六七八九十]{1,3})\s*(秒|分钟|分|小时|天)\s*后", text)
    if relative:
        count = chinese_number(relative[1])
        if count <= 0:
            raise ValueError("请填写大于0的时间间隔。")
        seconds = count * {"秒": 1, "分": 60, "分钟": 60, "小时": 3600, "天": 86400}[relative[2]]
        return ParsedTime(now + timedelta(seconds=seconds), explanation="从当前时间开始计算。")
    hour, minute = _clock(text)
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if "每天" in text or "每日" in text:
        if due <= now:
            due += timedelta(days=1)
        return ParsedTime(due, "daily", "每天在上海时间的这个时刻提醒。")
    explicit = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if explicit:
        try:
            due = due.replace(year=int(explicit[1]), month=int(explicit[2]), day=int(explicit[3]))
        except ValueError as exc:
            raise ValueError("日期无效，请检查年月日。") from exc
        return ParsedTime(due, explanation="按填写的上海时间保存。")
    if "大后天" in text:
        due += timedelta(days=3)
    elif "后天" in text:
        due += timedelta(days=2)
    elif "明天" in text:
        due += timedelta(days=1)
    elif "今天" in text or "今日" in text:
        pass
    else:
        weekday = re.search(r"(下周|下星期|本周|这周|周|星期)([一二三四五六日天1-7])", text)
        if weekday:
            index = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}.get(weekday[2], int(weekday[2]) - 1 if weekday[2].isdigit() else -1)
            if weekday[1] in ("下周", "下星期"):
                offset = 7 - now.weekday() + index
            elif weekday[1] in ("本周", "这周"):
                offset = index - now.weekday()
            else:
                offset = (index - now.weekday()) % 7
                if offset == 0 and due <= now:
                    offset = 7
            due += timedelta(days=offset)
        elif due <= now:
            due += timedelta(days=1)
            return ParsedTime(due, explanation="只填写了时刻，今天已过，因此安排在明天。")
    return ParsedTime(due, explanation="请核对识别结果后再保存。")


def parse_editable_time(text: str) -> datetime:
    """The confirmation field uses an unambiguous, editable format."""
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
    except ValueError:
        try:
            return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
        except ValueError as exc:
            raise ValueError("确认时间请使用 YYYY-MM-DD HH:MM，例如2026-10-14 14:30。") from exc


def next_daily(due: datetime, now: datetime) -> datetime:
    due, now = as_local(due), as_local(now)
    candidate = now.replace(hour=due.hour, minute=due.minute, second=due.second, microsecond=due.microsecond)
    return candidate + timedelta(days=1) if candidate <= now else candidate
