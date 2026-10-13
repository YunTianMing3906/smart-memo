"""Persistent reminder state; scheduler decisions are deterministic and testable."""
from datetime import datetime, timedelta
from pathlib import Path
import json
import os
import uuid
import copy
from parser import as_local, next_daily


def new_state() -> dict:
    return {"version": 1, "tasks": [], "deliveries": []}


def load_state(path: Path) -> dict:
    if not path.exists():
        return new_state()
    state = json.loads(path.read_text(encoding="utf-8-sig"))
    if state.get("version") != 1 or not isinstance(state.get("tasks"), list) or not isinstance(state.get("deliveries"), list):
        raise ValueError("备忘录数据格式不受支持。原文件已保留。")
    for delivery in state["deliveries"]:
        if delivery["status"] == "sending":
            # A restart may happen during a request. Retry remains bounded.
            delivery["status"] = "waiting" if delivery["attempts"] < 3 else "failed"
            delivery["error"] = "程序在发送过程中退出；发送结果未知。"
    return state


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def add_task(state: dict, text: str, due: datetime, recurrence: str | None = None,
             smart: dict | None = None) -> dict:
    if recurrence not in (None, "daily", "weekly", "weekdays", "monthly"):
        raise ValueError("不支持这种重复提醒规则。")
    task = {"id": uuid.uuid4().hex, "text": text, "due": as_local(due).isoformat(), "daily_time": as_local(due).isoformat() if recurrence == "daily" else None, "recurrence": recurrence, "completed": False, "last_reminded": None}
    if smart is not None:
        if not isinstance(smart, dict):
            raise ValueError("智能提醒方案格式无效。")
        task["smart"] = copy.deepcopy(smart)
        # The editable, confirmed time is authoritative. An advance alarm never
        # changes the actual event time stored in due or the follow-up anchor.
        task["smart"]["event_at"] = task["due"]
    state["tasks"].append(task)
    return task


def collect_due(state: dict, now: datetime) -> list[dict]:
    """Catch overdue items after sleep. Coalesce missed daily occurrences into one."""
    now = as_local(now)
    fired = []
    for task in state["tasks"]:
        if ((task.get("calendar") or {}).get("status", "local_legacy") != "local_legacy"
                or task["completed"] or not task["due"]):
            continue
        due = as_local(datetime.fromisoformat(task["due"]))
        if due > now:
            continue
        occurrence = due.isoformat()
        if not any(d["task_id"] == task["id"] and d["occurrence"] == occurrence for d in state["deliveries"]):
            state["deliveries"].append({"id": uuid.uuid4().hex, "task_id": task["id"], "occurrence": occurrence, "status": "waiting", "attempts": 0, "next_attempt": now.isoformat(), "error": ""})
            task["last_reminded"] = now.isoformat()
            fired.append(task.copy())
        daily_base = datetime.fromisoformat(task.get("daily_time") or occurrence)
        task["due"] = next_daily(daily_base, now).isoformat() if task["recurrence"] == "daily" else None
    return fired


def ready_delivery(state: dict, now: datetime) -> dict | None:
    now = as_local(now)
    tasks = {task["id"]: task for task in state["tasks"]}
    for delivery in state["deliveries"]:
        task = tasks.get(delivery["task_id"])
        if (not task or (task.get("calendar") or {}).get("status", "local_legacy") != "local_legacy"
                or task["completed"] or delivery["status"] != "waiting" or delivery["attempts"] >= 3):
            continue
        if as_local(datetime.fromisoformat(delivery["next_attempt"])) <= now:
            return delivery
    return None


def finish_delivery(delivery: dict, now: datetime, error: str | None) -> None:
    if delivery["status"] == "cancelled":
        return
    if error is None:
        delivery.update(status="sent", error="")
    elif delivery["attempts"] >= 3:
        delivery.update(status="failed", error=error)
    else:
        delay = (30, 120)[min(max(delivery["attempts"] - 1, 0), 1)]
        delivery.update(status="waiting", error=error, next_attempt=(as_local(now) + timedelta(seconds=delay)).isoformat())


def complete_task(state: dict, task_id: str) -> None:
    for task in state["tasks"]:
        if task["id"] == task_id:
            task["completed"] = True
    for delivery in state["deliveries"]:
        if delivery["task_id"] == task_id and delivery["status"] in ("waiting", "failed"):
            delivery["status"] = "cancelled"


def snooze_task(state: dict, task_id: str, now: datetime) -> None:
    for task in state["tasks"]:
        if task["id"] == task_id:
            task.update(due=(as_local(now) + timedelta(minutes=10)).isoformat(), completed=False)
    for delivery in state["deliveries"]:
        if delivery["task_id"] == task_id and delivery["status"] in ("waiting", "failed"):
            delivery["status"] = "cancelled"
