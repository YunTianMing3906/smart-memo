"""Pure calendar intent/outbox state plus an explicit atomic persistence helper.

No function sends a request, imports a UI, or opens an application. Keep the
existing memo file's version=1; calendar_schema=2 describes this optional layer.
The caller must persist before starting a request and after every confirmed
action. UI success must follow that second successful save, never HTTP alone.
"""
import copy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
from calendar import monthrange

from parser import as_local, local_now, next_daily
from calendar_events import (build_event, build_snooze_event, event_uid, snooze_event_uid,
                             build_followup_event, followup_event_uid, followup_offsets,
                             RECURRENCE_RULES)


def _now(now: datetime | None = None) -> datetime:
    return as_local(now or local_now()).replace(microsecond=0)


def _task(state: dict, task_id: str) -> dict:
    for task in state["tasks"]:
        if task["id"] == task_id:
            return task
    raise ValueError("找不到这条事项。")


def migrate_state(state: dict, recover_inflight: bool = False) -> bool:
    """Add optional fields; never upload or claim that a legacy task is hosted."""
    if state.get("version") != 1:
        raise ValueError("备忘录数据格式不受支持，原数据应予保留。")
    if state.get("calendar_schema", 2) != 2:
        raise ValueError("日历数据格式不受支持，原数据应予保留。")
    before = copy.deepcopy(state)
    state.setdefault("calendar_schema", 2)
    state.setdefault("calendar_outbox", [])
    for task in state["tasks"]:
        calendar = task.setdefault("calendar", {})
        calendar.setdefault("status", "local_legacy")
        calendar.setdefault("revision", 0)
        calendar.setdefault("resources", {})
        calendar.setdefault("exdates", [])
        calendar.setdefault("snooze_uids", [])
        calendar.setdefault("known_followup_uids", [])
        task.setdefault("followup_uids", [])
        calendar.setdefault("error", "")
    for operation in (state["calendar_outbox"] if recover_inflight else []):
        if operation["status"] == "sending":
            operation.update(status="uncertain", error="程序在日历保存过程中退出；结果待确认。")
            for action in operation["actions"]:
                if action["status"] == "sending":
                    action["status"] = "uncertain"
            task = _task(state, operation["task_id"])
            if task["calendar"]["revision"] == operation["revision"]:
                task["calendar"]["status"] = "cancel_uncertain" if operation["kind"] == "complete" else "uncertain"
                task["calendar"]["error"] = operation["error"]
    return state != before


def is_calendar_task(task: dict) -> bool:
    """Use in BOTH local due collection and local phone delivery selection."""
    return (task.get("calendar") or {}).get("status", "local_legacy") != "local_legacy"


def _cancel_local(state: dict, task_id: str) -> None:
    for delivery in state.get("deliveries", []):
        if delivery["task_id"] == task_id and delivery["status"] in ("waiting", "failed", "sending"):
            delivery.update(status="cancelled", error="事项已改用iCloud日历，本地手机发送已停用。")


def _put(snapshot: dict, now: datetime, snooze: bool = False) -> dict:
    uid = snapshot["calendar"]["snooze_uid"] if snooze else event_uid(snapshot["id"])
    return {"id": f"put:{uid}", "method": "PUT", "uid": uid,
            "content": build_snooze_event(snapshot, now) if snooze else build_event(snapshot, now),
            "status": "waiting", "receipt": None}


def _delete(uid: str) -> dict:
    return {"id": f"delete:{uid}", "method": "DELETE", "uid": uid,
            "content": None, "status": "waiting", "receipt": None}


def _known_followups(state: dict, task: dict) -> list[str]:
    """Include intent UIDs even when the PUT was accepted but its ACK was lost."""
    calendar = task["calendar"]
    prefix = f"memo-{task['id']}-followup-"
    uids = [*task.get("followup_uids", []), *calendar.get("known_followup_uids", [])]
    uids.extend(uid for uid in calendar.get("resources", {}) if uid.startswith(prefix))
    for operation in state["calendar_outbox"]:
        if operation["task_id"] == task["id"]:
            uids.extend(action["uid"] for action in operation["actions"]
                        if action["method"] == "PUT" and action["uid"].startswith(prefix))
    return list(dict.fromkeys(uids))


def _schedule_actions(state: dict, snapshot: dict, now: datetime) -> list[dict]:
    calendar = snapshot["calendar"]
    old_followups = _known_followups(state, snapshot)
    offsets = followup_offsets(snapshot)
    new_uids = [followup_event_uid(snapshot["id"], calendar["revision"], index)
                for index in range(len(offsets))]
    snapshot["followup_uids"] = new_uids
    calendar["known_followup_uids"] = list(dict.fromkeys([*old_followups, *new_uids]))
    actions = [_put(snapshot, now)]
    for index, uid in enumerate(new_uids):
        actions.append({"id": f"put:{uid}", "method": "PUT", "uid": uid,
                        "content": build_followup_event(snapshot, index, now),
                        "status": "waiting", "receipt": None})
    actions.extend(_delete(uid) for uid in old_followups if uid not in new_uids)
    return actions


def _enqueue(state: dict, task: dict, snapshot: dict, kind: str,
             actions: list[dict], now: datetime) -> dict:
    revision = snapshot["calendar"]["revision"]
    for previous in state["calendar_outbox"]:
        supersedable = ("waiting", "failed", "uncertain") if kind == "complete" else ("waiting", "failed")
        if previous["task_id"] == task["id"] and previous["status"] in supersedable:
            previous["status"] = "superseded"
    task.clear()
    task.update(snapshot)
    task["calendar"].update(status="cancel_pending" if kind == "complete" else "pending", error="")
    operation = {"id": f"{task['id']}:{revision}:{kind}", "task_id": task["id"],
                 "revision": revision, "kind": kind, "status": "waiting", "attempts": 0,
                 "created_at": now.isoformat(), "error": "", "actions": actions,
                 "calendar_url": snapshot["calendar"].get("calendar_url"),
                 "username": snapshot["calendar"].get("username")}
    state["calendar_outbox"].append(operation)
    _cancel_local(state, task["id"])
    return operation


def queue_schedule(state: dict, task_id: str, now: datetime | None = None,
                   calendar_url: str | None = None, username: str | None = None) -> dict:
    """Queue a stable-UID create/update. Retry with retry_operation, not this."""
    migrate_state(state)
    now = _now(now)
    task = _task(state, task_id)
    snapshot = copy.deepcopy(task)
    calendar = snapshot["calendar"]
    if not calendar.get("calendar_url"):
        if not calendar_url or not username:
            raise ValueError("请先选择 iCloud 专用日历。")
        calendar.update(calendar_url=calendar_url, username=username)
    calendar["revision"] += 1
    snapshot["completed"] = False
    if snapshot.get("recurrence") in RECURRENCE_RULES:
        calendar["series_start"] = snapshot.get("daily_time") or snapshot.get("due")
    if snapshot.get("smart") is not None:
        snapshot["smart"]["event_at"] = snapshot.get("due")
    calendar["exdates"] = []
    actions = _schedule_actions(state, snapshot, now)
    actions.extend(_delete(uid) for uid in dict.fromkeys(calendar["snooze_uids"]))
    calendar.update(snooze_uid=None, snooze_due=None)
    return _enqueue(state, task, snapshot, "schedule", actions, now)


def queue_complete(state: dict, task_id: str, now: datetime | None = None) -> dict:
    """Complete locally now; calendar cancellation remains pending until ACK."""
    migrate_state(state)
    now = _now(now)
    task = _task(state, task_id)
    snapshot = copy.deepcopy(task)
    snapshot["completed"] = True
    snapshot["calendar"]["revision"] += 1
    uids = [event_uid(task_id), *snapshot["calendar"]["snooze_uids"],
            *_known_followups(state, snapshot)]
    return _enqueue(state, task, snapshot, "complete", [_delete(uid) for uid in dict.fromkeys(uids)], now)


def queue_snooze(state: dict, task_id: str, now: datetime | None = None,
                 minutes: int = 10, occurrence: datetime | None = None) -> dict:
    """Snooze one occurrence; retain the daily series's original start time."""
    if minutes <= 0:
        raise ValueError("稍后提醒间隔必须大于0。")
    migrate_state(state)
    now = _now(now)
    task = _task(state, task_id)
    snapshot = copy.deepcopy(task)
    calendar = snapshot["calendar"]
    calendar["revision"] += 1
    due = now + timedelta(minutes=minutes)
    snapshot.update(due=due.isoformat(), completed=False)
    if snapshot.get("smart"):
        snapshot["smart"]["event_at"] = due.isoformat()
        # An explicit ten-minute snooze should ring in ten minutes; retaining
        # the original advance alarm could otherwise put its trigger in the past.
        snapshot["smart"]["remind_before_minutes"] = 0
    if snapshot.get("recurrence") and snapshot["recurrence"] != "daily":
        raise ValueError("重复事项暂不支持稍后提醒，请编辑原日程后重新安排。")
    if snapshot.get("recurrence") != "daily":
        actions = _schedule_actions(state, snapshot, now)
    else:
        if occurrence is None:
            raise ValueError("每日事项需要明确指定要延后的那一次日程；此版界面暂不支持。")
        anchor_value = calendar.get("series_start") or snapshot.get("daily_time")
        if not anchor_value:
            raise ValueError("每日事项缺少原始提醒时刻，无法可靠安排稍后提醒。")
        anchor = as_local(datetime.fromisoformat(anchor_value)).replace(microsecond=0)
        calendar["series_start"] = anchor.isoformat()
        occurrence = as_local(occurrence).replace(microsecond=0)
        if occurrence < anchor or (occurrence.hour, occurrence.minute, occurrence.second) != (anchor.hour, anchor.minute, anchor.second):
            raise ValueError("要延后的日程不是这个每日系列的有效发生时间。")
        excluded = set(calendar["exdates"])
        excluded.add(occurrence.isoformat())
        calendar["exdates"] = sorted(excluded)
        old_snoozes = list(dict.fromkeys(calendar["snooze_uids"]))
        calendar["snooze_uid"] = snooze_event_uid(task_id, calendar["revision"])
        calendar["snooze_due"] = due.isoformat()
        calendar["snooze_uids"].append(calendar["snooze_uid"])
        # Put the replacement first: a partial failure must not leave the user
        # with neither the original nor the new reminder.
        actions = [_put(snapshot, now, snooze=True), _put(snapshot, now)]
        actions.extend(_delete(uid) for uid in old_snoozes)
    return _enqueue(state, task, snapshot, "snooze", actions, now)


def _operation(state: dict, operation_id: str) -> dict:
    for operation in state["calendar_outbox"]:
        if operation["id"] == operation_id:
            return operation
    raise ValueError("找不到日历保存操作。")


def ready_operation(state: dict) -> dict | None:
    """Return one waiting operation; uncertain older writes block newer ones."""
    blocked = {op["task_id"] for op in state.get("calendar_outbox", [])
               if op["status"] in ("sending", "uncertain")}
    return next((op for op in state.get("calendar_outbox", [])
                 if op["status"] == "waiting" and op["task_id"] not in blocked), None)


def begin_operation(state: dict, operation_id: str) -> dict:
    operation = _operation(state, operation_id)
    if operation["status"] != "waiting":
        raise ValueError("这项日历操作当前不可发送。")
    if not operation.get("calendar_url") or not operation.get("username"):
        raise ValueError("这项日历操作缺少目标日历，请重新连接并安排。")
    operation.update(status="sending", attempts=operation["attempts"] + 1)
    return operation


def next_action(state: dict, operation_id: str) -> dict | None:
    return next((action for action in _operation(state, operation_id)["actions"]
                 if action["status"] != "succeeded"), None)


def begin_action(state: dict, operation_id: str, action_id: str) -> dict:
    operation = _operation(state, operation_id)
    if operation["status"] != "sending":
        raise ValueError("日历操作尚未开始。")
    action = next_action(state, operation_id)
    if not action or action["id"] != action_id:
        raise ValueError("日历操作顺序已改变。")
    action["status"] = "sending"
    return action


def record_action_success(state: dict, operation_id: str, action_id: str,
                          receipt: dict, now: datetime | None = None) -> None:
    """Caller must confirm the server result and persist this before proceeding."""
    if not receipt.get("confirmed") or not receipt.get("href"):
        raise ValueError("缺少已确认的iCloud保存收据。")
    operation = _operation(state, operation_id)
    action = next((item for item in operation["actions"] if item["id"] == action_id), None)
    if not action:
        raise ValueError("找不到这项日历动作。")
    action.update(status="succeeded", receipt=copy.deepcopy(receipt))
    task = _task(state, operation["task_id"])
    resources = task["calendar"]["resources"]
    if action["method"] == "DELETE":
        resources.pop(action["uid"], None)
        task["calendar"]["snooze_uids"] = [uid for uid in task["calendar"]["snooze_uids"] if uid != action["uid"]]
        task["followup_uids"] = [uid for uid in task.get("followup_uids", []) if uid != action["uid"]]
    else:
        resources[action["uid"]] = copy.deepcopy(receipt)
    if all(item["status"] == "succeeded" for item in operation["actions"]):
        operation.update(status="succeeded", error="", confirmed_at=_now(now).isoformat())
        if task["calendar"]["revision"] == operation["revision"]:
            task["calendar"].update(status="cancelled" if operation["kind"] == "complete" else "confirmed",
                                    error="", confirmed_revision=operation["revision"],
                                    confirmed_at=operation["confirmed_at"], confirmed_due=task.get("due"))
    _cancel_local(state, task["id"])


def record_operation_failure(state: dict, operation_id: str, error: str,
                             uncertain: bool = False) -> None:
    operation = _operation(state, operation_id)
    operation.update(status="uncertain" if uncertain else "failed", error=str(error))
    for action in operation["actions"]:
        if action["status"] == "sending":
            action["status"] = "uncertain" if uncertain else "waiting"
    task = _task(state, operation["task_id"])
    if task["calendar"]["revision"] == operation["revision"]:
        prefix = "cancel_" if operation["kind"] == "complete" else ""
        task["calendar"].update(status=prefix + ("uncertain" if uncertain else "failed"), error=str(error))


def retry_operation(state: dict, operation_id: str) -> dict:
    """Reuse UID, revision and exact ICS; GET/reconcile uncertain PUTs first."""
    operation = _operation(state, operation_id)
    if operation["status"] not in ("failed", "uncertain"):
        raise ValueError("这项日历操作无需重试。")
    operation["needs_reconcile"] = operation["status"] == "uncertain"
    operation.update(status="waiting", error="")
    for action in operation["actions"]:
        if action["status"] != "succeeded":
            action["status"] = "waiting"
    task = _task(state, operation["task_id"])
    if task["calendar"]["revision"] == operation["revision"]:
        task["calendar"].update(status="cancel_pending" if operation["kind"] == "complete" else "pending", error="")
    return operation


def _next_series_due(anchor: datetime, recurrence: str, now: datetime) -> datetime:
    if recurrence == "daily":
        return anchor if anchor > now else next_daily(anchor, now)
    candidate = now.replace(hour=anchor.hour, minute=anchor.minute, second=anchor.second,
                            microsecond=anchor.microsecond)
    if recurrence == "weekly":
        if anchor > now:
            return anchor
        candidate += timedelta(days=(anchor.weekday() - candidate.weekday()) % 7)
        return candidate + timedelta(days=7) if candidate <= now else candidate
    if recurrence == "weekdays":
        candidate = anchor if anchor > now else candidate
        if candidate <= now:
            candidate += timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate
    if recurrence == "monthly":
        year, month = (anchor.year, anchor.month) if anchor > now else (now.year, now.month)
        while True:
            # RFC monthly rules do not clamp Jan 31 to Feb 28; invalid dates
            # have no occurrence. Match that behavior in the displayed preview.
            if anchor.day <= monthrange(year, month)[1]:
                candidate = anchor.replace(year=year, month=month)
                if candidate > now and candidate >= anchor:
                    return candidate
            month += 1
            if month == 13:
                year, month = year + 1, 1
    raise ValueError("不支持这种日历重复规则。")


def next_display_due(task: dict, now: datetime | None = None) -> str | None:
    """Display helper only; never publishes or changes stored recurrence state."""
    if task.get("completed"):
        return None
    now = _now(now)
    calendar = task.get("calendar") or {}
    snooze = calendar.get("snooze_due")
    if snooze and as_local(datetime.fromisoformat(snooze)) > now:
        return snooze
    if task.get("recurrence") in RECURRENCE_RULES:
        anchor = calendar.get("series_start") or task.get("daily_time") or task.get("due")
        if not anchor:
            return None
        anchor = as_local(datetime.fromisoformat(anchor))
        candidate = _next_series_due(anchor, task["recurrence"], now)
        excluded = {as_local(datetime.fromisoformat(value)) for value in calendar.get("exdates", [])}
        while candidate in excluded:
            candidate = _next_series_due(anchor, task["recurrence"], candidate)
        return candidate.isoformat()
    return task.get("due")


def save_state_atomic(path: Path, state: dict) -> None:
    """Persist the complete memo state atomically, without storing credentials."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         prefix=path.name + ".", suffix=".tmp",
                                         dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
