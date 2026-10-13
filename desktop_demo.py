"""Run with Python 3.12. Importing this module does not open a window or send."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import ttk, messagebox
import copy
import json
import queue
import re
import threading
import tkinter as tk
import webbrowser
import uuid

from parser import local_now
from reminder_store import load_state, add_task
from icloud_client import ICloudClient, CredentialStore, ICloudError, ICloudConflictError, events_equivalent
from icloud_store import (migrate_state, queue_schedule, queue_complete,
                          ready_operation, begin_operation, record_action_success,
                          record_operation_failure, is_calendar_task, next_action,
                          begin_action, retry_operation, next_display_due, save_state_atomic)
from ai_ui import AIAssistantUI, REPEATS
from app_paths import app_dir

BASE = app_dir()
DATA = BASE / "memo-data.json"
ICLOUD_SETTINGS = BASE / "icloud-settings.json"
ICLOUD_CREDENTIALS = BASE / "icloud-credentials.dpapi.json"


class AlreadyRunningError(RuntimeError):
    pass


def acquire_instance_lock(path: Path = BASE / "memo-process.lock"):
    """A process-held file lock releases automatically if Windows terminates us."""
    import msvcrt
    handle = path.open("a+b")
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise AlreadyRunningError("备忘录已经运行，请查看已有窗口。") from exc
        return handle
    except Exception:
        handle.close()
        raise


def release_instance_lock(handle):
    import msvcrt
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


class MemoApp(AIAssistantUI):
    def __init__(self, root: tk.Tk):
        self.root = root
        self.state = load_state(DATA)
        migrate_state(self.state, recover_inflight=True)
        self.calendar_settings = self.read_calendar_settings()
        self.events: queue.Queue = queue.Queue()
        self.network_busy = False
        self.persistence_error = False
        self.root.title("我的备忘录 · DeepSeek智能提醒版")
        self.root.geometry("920x640")
        self.root.minsize(760, 540)
        self.root.configure(background="#f3f5f7")
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("App.TFrame", background="#f3f5f7")
        style.configure("Card.TFrame", background="white")
        style.configure("App.TLabel", background="#f3f5f7", foreground="#263443")
        style.configure("Header.TLabel", background="#f3f5f7", foreground="#172d43", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Card.TLabel", background="white", foreground="#314253")
        style.configure("Treeview", rowheight=36, font=("Microsoft YaHei UI", 10))
        container = ttk.Frame(root, style="App.TFrame", padding=24)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text="我的备忘录", style="Header.TLabel").pack(anchor="w")
        ttk.Label(container, text="DeepSeek理解事项 · 核对后同步 iCloud 日历 · 上海时间（UTC+8）", style="App.TLabel").pack(anchor="w", pady=(5, 8))
        connection_row = ttk.Frame(container, style="App.TFrame")
        connection_row.pack(fill="x", pady=(0, 12))
        self.connection_text = tk.StringVar()
        ttk.Label(connection_row, textvariable=self.connection_text, style="App.TLabel").pack(side="left")
        ttk.Button(connection_row, text="连接 iCloud 日历", command=self.configure_icloud).pack(side="right")
        self.update_connection_text()
        card = ttk.Frame(container, style="Card.TFrame", padding=16)
        card.pack(fill="x")
        ttk.Label(card, text="记下需要提醒的事", style="Card.TLabel").pack(anchor="w")
        self.memo = tk.Text(card, height=3, wrap="word", relief="solid", borderwidth=1, padx=10, pady=8, background="#fbfcfd", foreground="#243344")
        self.memo.pack(fill="x", pady=(7, 10))
        ai_row = ttk.Frame(card, style="Card.TFrame")
        ai_row.pack(fill="x")
        self.parse_hint = tk.StringVar(value="写下一段话后点“AI识别”，逐条核对后再保存。例：明天15点开会，提前30分钟提醒；接下来10周每周一15点开例会。")
        ttk.Label(ai_row, textvariable=self.parse_hint, style="Card.TLabel", wraplength=700).pack(side="left", pady=(0, 4))
        self.install_ai(connection_row, ai_row)
        toolbar = ttk.Frame(container, style="App.TFrame")
        toolbar.pack(fill="x", pady=(18, 9))
        ttk.Label(toolbar, text="我的事项", style="App.TLabel", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        self.show_done = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="显示已完成", variable=self.show_done, command=self.refresh).pack(side="right")
        self.tree = ttk.Treeview(container, columns=("text", "due", "alert", "status"), show="headings", selectmode="browse", height=4)
        for key, label, width in (("text", "事项", 300), ("due", "事件/截止时间", 170), ("alert", "下次提醒", 170), ("status", "提醒状态", 220)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=100, stretch=key == "text")
        # Reserve the complete footer before allocating remaining space to the list.
        # This keeps actions and network errors visible at small window heights.
        footer = ttk.Frame(container, style="App.TFrame")
        footer.pack(side="bottom", fill="x")
        buttons = ttk.Frame(footer, style="App.TFrame")
        buttons.pack(fill="x", pady=10)
        ttk.Button(buttons, text="标记完成", command=self.complete).pack(side="left")
        ttk.Button(buttons, text="重试同步", command=self.retry).pack(side="left", padx=8)
        ttk.Button(buttons, text="取消整批计划", command=self.cancel_batch).pack(side="left")
        ttk.Label(footer, text="新事项会上传到选中的 iCloud 日历。等“iCloud已保存”后可关闭电脑；首次请确认手机日历已出现日程和提醒。", style="App.TLabel", wraplength=880).pack(anchor="w")
        self.status = tk.StringVar(value="日历已连接。写下一段话点“AI识别”，确认后等待“iCloud已保存”。" if self.calendar_settings else "先连接iCloud日历；配置DeepSeek后可AI识别。")
        self.status_label = tk.Label(footer, textvariable=self.status, background="#f3f5f7", foreground="#42596c", anchor="w", justify="left", wraplength=880)
        self.status_label.pack(fill="x", pady=(7, 0))
        self.tree.pack(side="top", fill="both", expand=True)
        self.refresh()
        self.root.after(500, self.tick)

    def set_status(self, text: str, error: bool = False):
        self.status.set(text)
        self.status_label.configure(foreground="#b42318" if error else "#42596c")

    def read_calendar_settings(self):
        try:
            credentials = CredentialStore(ICLOUD_CREDENTIALS).load()
            if credentials and credentials.get("calendar_url"):
                return {key: credentials[key] for key in ("username", "endpoint", "calendar_url", "calendar_name")}
        except Exception:
            pass
        if not ICLOUD_SETTINGS.exists():
            return {}
        try:
            return json.loads(ICLOUD_SETTINGS.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}

    def update_connection_text(self):
        name = self.calendar_settings.get("calendar_name")
        self.connection_text.set(f"日历：{name}" if name else "尚未连接 iCloud 日历")

    def calendar_client(self):
        if not self.calendar_settings.get("calendar_url"):
            raise ValueError("请先点击“连接 iCloud 日历”，选择专用日历并保存配置。")
        credentials = CredentialStore(ICLOUD_CREDENTIALS).load()
        if not credentials or not credentials.get("password"):
            raise ValueError("尚未保存 Apple App 专用密码，请重新连接日历。")
        if credentials.get("username") != self.calendar_settings.get("username"):
            raise ValueError("日历配置与 Apple 账户不一致，请重新选择日历。")
        return ICloudClient(self.calendar_settings, credentials["password"])

    def configure_icloud(self):
        if self.network_busy:
            self.set_status("正在同步，请稍后再修改连接配置。")
            return
        if getattr(self, "icloud_dialog", None) and self.icloud_dialog.winfo_exists():
            self.icloud_dialog.lift()
            return
        dialog = self.icloud_dialog = tk.Toplevel(self.root)
        self.discovery_generation = getattr(self, "discovery_generation", 0) + 1
        dialog.title("连接 iCloud 日历")
        dialog.geometry("610x440")
        dialog.minsize(570, 420)
        box = ttk.Frame(dialog, padding=20)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="先在 iPhone 的 iCloud 日历中新建私人日历“智能备忘录”。", wraplength=550).pack(anchor="w")
        ttk.Label(box, text="Apple 账户邮箱").pack(anchor="w", pady=(14, 4))
        self.apple_account = tk.StringVar(value=self.calendar_settings.get("username", ""))
        ttk.Entry(box, textvariable=self.apple_account).pack(fill="x")
        ttk.Label(box, text="App 专用密码（已保存时可留空）").pack(anchor="w", pady=(12, 4))
        self.apple_password = tk.StringVar()
        ttk.Entry(box, textvariable=self.apple_password, show="●").pack(fill="x")
        ttk.Button(box, text="打开 Apple 专用密码说明", command=lambda: webbrowser.open("https://support.apple.com/zh-cn/102654")).pack(anchor="w", pady=8)
        self.discovery_status = tk.StringVar(value="专用密码只在本机加密保存。不要填写 Apple 主密码。")
        ttk.Label(box, textvariable=self.discovery_status, wraplength=550).pack(anchor="w")
        self.discover_button = ttk.Button(box, text="读取可用日历", command=self.discover_calendars)
        self.discover_button.pack(anchor="w", pady=(12, 8))
        self.calendar_picker = ttk.Combobox(box, state="readonly")
        self.calendar_picker.pack(fill="x")
        self.discovered_calendars = []
        self.discovered_credentials = None
        ttk.Label(box, text="只选择刚建的私人专用日历；原有事项不会自动上传。", wraplength=550).pack(anchor="w", pady=8)
        ttk.Button(box, text="保存日历连接", command=self.save_calendar_connection).pack(anchor="e")

    def discover_calendars(self):
        username = self.apple_account.get().strip()
        password = self.apple_password.get().strip()
        if not username:
            self.discovery_status.set("请填写 Apple 账户邮箱。")
            return
        if not password:
            try:
                stored = CredentialStore(ICLOUD_CREDENTIALS).load()
                if stored and stored.get("username") == username:
                    password = stored.get("password", "")
            except Exception:
                pass
        if not password:
            self.discovery_status.set("请填写 Apple App 专用密码。")
            return
        config = {"username": username, "endpoint": "https://caldav.icloud.com/"}
        self.discovered_calendars = []
        self.discovered_credentials = None
        self.calendar_picker.configure(values=[])
        self.calendar_picker.set("")
        self.discover_button.configure(state="disabled")
        self.discovery_status.set("正在连接 Apple 并读取日历……")
        self.discovery_generation = getattr(self, "discovery_generation", 0) + 1
        generation = self.discovery_generation
        def worker():
            try:
                calendars = ICloudClient(config, password).discover_calendars()
                self.events.put(("calendar_discovery", {"generation": generation, "calendars": calendars, "config": config, "password": password}, None))
            except Exception as exc:
                self.events.put(("calendar_discovery", {"generation": generation}, self.safe_error(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def save_calendar_connection(self):
        if self.network_busy:
            self.discovery_status.set("正在同步，请等待当前同步结束后保存连接。")
            return
        selected = self.calendar_picker.current()
        if selected < 0 or not self.discovered_credentials:
            self.discovery_status.set("先读取日历，再明确选择一个专用日历。")
            return
        calendar = self.discovered_calendars[selected]
        config, password = self.discovered_credentials
        if self.apple_account.get().strip() != config["username"]:
            self.discovery_status.set("账户已改变，请重新读取日历后选择。")
            return
        settings = dict(config, calendar_url=calendar["url"], calendar_name=calendar["display_name"])
        try:
            # Commit credentials and their chosen collection together in one
            # atomic file, preventing a new-password/old-calendar split.
            CredentialStore(ICLOUD_CREDENTIALS).save(config["username"], password, config["endpoint"],
                                                    settings["calendar_url"], settings["calendar_name"])
        except Exception as exc:
            self.discovery_status.set(f"配置保存失败：{self.safe_error(exc)}")
            return
        self.calendar_settings = settings
        self.update_connection_text()
        self.apple_password.set("")
        self.discovered_credentials = None
        self.icloud_dialog.destroy()
        self.set_status("日历连接已保存。新事项将上传；请开启 iPhone 系统日历通知和这个日历的“日程提醒”。")

    def safe_error(self, exc):
        text = str(exc)
        credentials = getattr(self, "discovered_credentials", None)
        if credentials:
            text = text.replace(credentials[1], "[已隐藏]")
        # This may be called by a worker: never query Tk variables here.
        return text[:400]

    def mutate(self, action) -> bool:
        old = copy.deepcopy(self.state)
        try:
            action()
            backup = BASE / "memo-data.before-icloud.json"
            if DATA.exists() and not backup.exists():
                with backup.open("xb") as handle:
                    handle.write(DATA.read_bytes())
            save_state_atomic(DATA, self.state)
            self.persistence_error = False
            self.refresh()
            return True
        except Exception as exc:
            self.state = old
            self.persistence_error = True
            self.set_status(f"本地保存失败，操作未保存：{exc}。原数据已保留。", True)
            return False

    def queue_new_calendar_bundle(self, text, entries):
        """Create one task per reviewed event, then persist all in one save.

        ``entries`` is a list of {"due": datetime, "recurrence": str|None,
        "smart": dict}. Each entry becomes its own calendar task, so finishing
        one occurrence cancels only that occurrence's event.
        """
        if not entries:
            self.set_status("没有可保存的日程。", True)
            return False
        try:
            self.calendar_client()
            for entry in entries:
                if entry["recurrence"] == "weekdays" and entry["due"].weekday() >= 5:
                    raise ValueError("工作日事项的首个日期应为周一至周五。")
                first_alert = entry["due"] - timedelta(minutes=(entry["smart"] or {}).get("remind_before_minutes", 0))
                if first_alert <= local_now():
                    raise ValueError("“{}”的首次提醒时间已经过去，请调整事件时间或提前量。".format(
                        (entry["smart"] or {}).get("title") or text[:30]))
        except Exception as exc:
            self.set_status(self.safe_error(exc), True)
            return False
        def change():
            batch_id = uuid.uuid4().hex
            for entry in entries:
                task = add_task(self.state, text, entry["due"], entry["recurrence"], smart=entry["smart"])
                task["batch_id"] = batch_id
                queue_schedule(self.state, task["id"], calendar_url=self.calendar_settings["calendar_url"],
                               username=self.calendar_settings["username"])
        return self.mutate(change)

    def selected(self) -> str | None:
        selected = self.tree.selection()
        if not selected:
            self.set_status("请先选中一条事项。", True)
            return None
        return selected[0]

    def complete(self):
        task_id = self.selected()
        if not task_id:
            return
        task = self.task_by_id(task_id)
        if not is_calendar_task(task):
            self.set_status("这是旧版本地事项，没有云端日程，无需处理。")
            return
        if not self.can_modify_calendar(task_id, allow_uncertain=True):
            return
        if self.mutate(lambda: queue_complete(self.state, task_id)):
            self.set_status("已标记完成，正在取消 iCloud 日程；等“云端已取消”后才确认停止提醒。")

    def task_by_id(self, task_id):
        return next(task for task in self.state["tasks"] if task["id"] == task_id)

    def cancel_batch(self):
        task_id = self.selected()
        if not task_id:
            return
        batch_id = self.task_by_id(task_id).get("batch_id")
        if not batch_id:
            self.complete()
            return
        tasks = [task for task in self.state["tasks"] if task.get("batch_id") == batch_id
                 and is_calendar_task(task) and task["calendar"]["status"] != "cancelled"]
        if not tasks:
            self.set_status("这批计划已取消。")
            return
        if any(not self.can_modify_calendar(task["id"], allow_uncertain=True) for task in tasks):
            return
        if not messagebox.askyesno("取消整批计划", f"将取消这次确认保存的全部{len(tasks)}条日程，包括关联事项。继续吗？", parent=self.root):
            return
        def cancel():
            for task in tasks:
                task["batch_cancel_requested"] = True
                queue_complete(self.state, task["id"])
        if self.mutate(cancel):
            self.set_status("已提交整批取消，等待各条日程的云端取消确认。")

    def batch_status(self, task_id, cancelling=False):
        task = self.task_by_id(task_id)
        batch_id = task.get("batch_id")
        if not batch_id:
            return "这条日程已取消。" if cancelling else "这条日程iCloud已保存。"
        items = [item for item in self.state["tasks"] if item.get("batch_id") == batch_id]
        if cancelling and not any(item.get("batch_cancel_requested") for item in items):
            return "这条日程已取消；本批其他日程仍保留。"
        desired = "cancelled" if cancelling else "confirmed"
        count = sum((item.get("calendar") or {}).get("status") == desired for item in items)
        return (f"本批已{'取消' if cancelling else '保存'}{count}/{len(items)}条。" +
                ("全部完成；首次请确认手机日历已同步后再关机。" if count == len(items)
                 else "其余仍待确认，请查看每条状态。"))

    def can_modify_calendar(self, task_id, allow_uncertain=False):
        pending = [op for op in self.state.get("calendar_outbox", []) if op["task_id"] == task_id]
        blocking = ("sending",) if allow_uncertain else ("sending", "uncertain")
        if any(op["status"] in blocking for op in pending):
            self.set_status("这条事项正在同步或上次结果待确认，请先等待或点击“重试同步”确认后再修改。", True)
            return False
        return True

    def retry(self):
        task_id = self.selected()
        if not task_id:
            return
        if not is_calendar_task(self.task_by_id(task_id)):
            self.set_status("这条事项没有需要重试的同步操作。")
            return
        failed_ops = [op for op in self.state["calendar_outbox"]
                      if op["task_id"] == task_id and op["status"] in ("failed", "uncertain")]
        if not failed_ops:
            self.set_status("这条事项没有失败或待确认的同步操作。")
            return
        if self.mutate(lambda: retry_operation(self.state, failed_ops[0]["id"])):
            self.set_status("已加入同步队列，将先检查 iCloud 中的原事件，避免重复创建。")

    def refresh(self):
        selection = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for task in self.state["tasks"]:
            calendar_status = (task.get("calendar") or {}).get("status", "local_legacy")
            unsettled_cancel = calendar_status in ("cancel_pending", "cancel_failed", "cancel_uncertain")
            if task["completed"] and not self.show_done.get() and not unsettled_cancel:
                continue
            if is_calendar_task(task):
                labels = {"pending": "等待 iCloud 保存", "confirmed": "iCloud已保存",
                          "failed": "同步失败 · 请重试", "uncertain": "保存结果待确认 · 请重试",
                          "cancel_pending": "完成 · 等待云端取消", "cancelled": "完成 · 云端已取消",
                          "cancel_failed": "完成 · 云端取消失败", "cancel_uncertain": "完成 · 取消结果待确认"}
                status = labels.get(calendar_status, "同步状态待确认")
                value = next_display_due(task)
                due = datetime.fromisoformat(value).strftime("%m-%d %H:%M:%S") if value else "—"
                repeat_label = next((label for label, value in REPEATS.items() if value == task.get("recurrence")), None)
                if repeat_label:
                    due += "（" + repeat_label + "）"
                task_title = (task.get("smart") or {}).get("title") or task["text"]
                smart = task.get("smart") or {}
                before = timedelta(minutes=smart.get("remind_before_minutes", 0))
                candidates = []
                alert_event = next_display_due(task, now=local_now() + before)
                if alert_event and not task.get("completed"):
                    candidate = datetime.fromisoformat(alert_event) - before
                    if candidate > local_now():
                        candidates.append(candidate)
                if smart.get("event_at") and not task.get("completed"):
                    base = datetime.fromisoformat(smart["event_at"])
                    candidates.extend(base + timedelta(minutes=offset) for offset in smart.get("followup_offsets_minutes", [])
                                      if base + timedelta(minutes=offset) > local_now())
                alert = min(candidates).strftime("%m-%d %H:%M:%S") if candidates else "—"
                self.tree.insert("", "end", iid=task["id"], values=(task_title.replace("\n", " "), due, alert, status))
                continue
            deliveries = [d for d in self.state["deliveries"] if d["task_id"] == task["id"]]
            active = [d for d in deliveries if d["status"] in ("failed", "waiting", "sending")]
            last = active[-1] if active else deliveries[-1] if deliveries else None
            status = "已完成" if task["completed"] else "等待提醒" if not last else {"waiting": f"手机等待重试（{last['attempts']}/3）" if last["attempts"] else "手机等待发送", "sending": "手机发送中", "failed": "手机失败 · 可手动重试", "sent": "服务器已接收", "cancelled": "旧提醒已取消"}[last["status"]]
            if task["last_reminded"] and not task["completed"]:
                status = "本地已提醒 · " + status
            status = "旧本地 · " + status
            due = datetime.fromisoformat(task["due"]).strftime("%m-%d %H:%M:%S") if task["due"] else "等待处理"
            if task["recurrence"] == "daily":
                due += "（每天）"
            self.tree.insert("", "end", iid=task["id"], values=(task["text"].replace("\n", " "), due, due, status))
        if selection and self.tree.exists(selection[0]):
            self.tree.selection_set(selection[0])

    def start_calendar_action(self, operation, action):
        task = self.task_by_id(operation["task_id"])
        resource = copy.deepcopy(task["calendar"]["resources"].get(action["uid"]))
        prior_contents = [candidate["content"] for old in self.state.get("calendar_outbox", [])
                          if old["task_id"] == operation["task_id"]
                          for candidate in old["actions"]
                          if candidate["uid"] == action["uid"] and candidate["method"] == "PUT"]
        try:
            client = self.calendar_client()
            if operation["username"] != self.calendar_settings.get("username"):
                raise ValueError("这条事项属于原来的 Apple 账户，请换回原账户后处理。")
        except Exception as exc:
            self.mutate(lambda: record_operation_failure(self.state, operation["id"], self.safe_error(exc)))
            self.set_status(self.safe_error(exc), True)
            return
        operation, action = copy.deepcopy(operation), copy.deepcopy(action)
        self.network_busy = True
        def worker():
            attempted_write = False
            try:
                target = client.event_url(operation["calendar_url"], action["uid"])
                current = client.get_event(target)
                etag = resource.get("etag") if resource else None
                if action["method"] == "PUT":
                    if current and events_equivalent(action["content"], current["ics"]):
                        receipt = {"confirmed": True, "href": current["url"], "etag": current["etag"]}
                    else:
                        if current and (not etag or current["etag"] != etag):
                            raise ICloudConflictError()
                        if current is None and etag:
                            raise ICloudConflictError("原日程已在其他设备删除，请重新记录事项。")
                        event_block = action["content"].split("BEGIN:VEVENT\r\n", 1)[1]
                        start_line = next(line for line in event_block.split("\r\n") if line.startswith("DTSTART"))
                        name, value = start_line.split(":", 1)
                        if value.endswith("Z"):
                            start = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                        elif name == "DTSTART;TZID=Asia/Shanghai":
                            start = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=timezone(timedelta(hours=8)))
                        else:
                            raise ValueError("首次提醒的时区无法确认，请重新记录事项。")
                        advance = re.search(r"(?:^|\r\n)TRIGGER:-PT(\d+)M(?:\r\n|$)", event_block)
                        first_alert = start - timedelta(minutes=int(advance[1]) if advance else 0)
                        if first_alert <= local_now():
                            raise ValueError("首次提醒时间已经过去，请重新录入未来时间或减小提前量。")
                        attempted_write = True
                        saved = client.upsert_event(operation["calendar_url"], action["uid"], action["content"], etag)
                        receipt = {"confirmed": bool(saved.get("verified")), "href": saved["url"], "etag": saved["etag"]}
                elif current is None:
                    receipt = {"confirmed": True, "href": target, "deleted": True}
                else:
                    if etag and etag != current["etag"]:
                        if any(events_equivalent(content, current["ics"]) for content in prior_contents):
                            etag = current["etag"]
                        else:
                            raise ICloudConflictError()
                    attempted_write = True
                    deleted = client.delete_event(target, etag, expected_uid=action["uid"])
                    receipt = {"confirmed": bool(deleted.get("deleted")), "href": deleted["url"], "deleted": True}
                self.events.put(("calendar_action", {"operation": operation["id"], "action": action["id"], "receipt": receipt}, None))
            except Exception as exc:
                known_rejection = getattr(exc, "code", "") in ("authentication_failed", "permission_denied", "calendar_conflict", "unsafe_url", "invalid_uid", "invalid_etag", "uid_mismatch")
                self.events.put(("calendar_action", {"operation": operation["id"], "action": action["id"], "uncertain": attempted_write and not known_rejection}, self.safe_error(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def advance_calendar_queue(self):
        if self.network_busy:
            return
        operation = next((op for op in self.state.get("calendar_outbox", []) if op["status"] == "sending"), None)
        if operation is None:
            operation = ready_operation(self.state)
            if not operation:
                return
            operation_id = operation["id"]
            if not self.mutate(lambda: begin_operation(self.state, operation_id)):
                return
            operation = next(op for op in self.state["calendar_outbox"] if op["id"] == operation_id)
        action = next_action(self.state, operation["id"])
        if not action:
            return
        action_id, operation_id = action["id"], operation["id"]
        if not self.mutate(lambda: begin_action(self.state, operation_id, action_id)):
            return
        operation = next(op for op in self.state["calendar_outbox"] if op["id"] == operation_id)
        action = next_action(self.state, operation_id)
        self.set_status("正在同步 iCloud 日程，请等列表显示“iCloud已保存”。")
        self.start_calendar_action(operation, action)

    def tick(self):
        try:
            while True:
                kind, payload, error = self.events.get_nowait()
                if kind == "ai_plan":
                    self.handle_ai_event(payload, error)
                    continue
                if kind == "calendar_discovery":
                    if not getattr(self, "icloud_dialog", None) or not self.icloud_dialog.winfo_exists():
                        continue
                    if payload["generation"] != self.discovery_generation:
                        continue
                    self.discover_button.configure(state="normal")
                    if error:
                        self.discovery_status.set(error)
                        continue
                    calendars = [c for c in payload["calendars"] if c.get("writable")]
                    self.discovered_calendars = calendars
                    self.discovered_credentials = (payload["config"], payload["password"])
                    self.calendar_picker.configure(values=[c["display_name"] for c in calendars])
                    self.calendar_picker.set("")
                    self.discovery_status.set("请选择专用日历，再保存连接。" if calendars else "没有找到可写日历，请先在 iPhone 新建一个私人 iCloud 日历。")
                    continue
                if kind == "calendar_action":
                    if error:
                        stored = self.mutate(lambda: record_operation_failure(self.state, payload["operation"], error, payload["uncertain"]))
                    else:
                        stored = self.mutate(lambda: record_action_success(self.state, payload["operation"], payload["action"], payload["receipt"]))
                    if not stored:
                        self.events.put((kind, payload, error))
                        break  # Keep the result, never resend while local ACK cannot be saved.
                    self.network_busy = False
                    if error:
                        self.set_status(f"iCloud 同步未确认：{error}。请选中事项重试同步；此时不要把它当作已安排好。", True)
                    else:
                        operation = next(op for op in self.state["calendar_outbox"] if op["id"] == payload["operation"])
                        if operation["status"] == "succeeded":
                            self.set_status(self.batch_status(operation["task_id"], operation["kind"] == "complete"))
                    continue
        except queue.Empty:
            pass
        self.advance_calendar_queue()
        self.root.after(1000, self.tick)


def main():
    try:
        instance_lock = acquire_instance_lock()
    except Exception as exc:
        root = tk.Tk()
        root.withdraw()
        if isinstance(exc, AlreadyRunningError):
            messagebox.showinfo("备忘录已运行", str(exc))
        else:
            messagebox.showerror("备忘录无法打开", f"无法创建进程锁：{exc}")
        root.destroy()
        return
    root = tk.Tk()
    try:
        try:
            MemoApp(root)
        except Exception as exc:
            messagebox.showerror("备忘录无法打开", f"{exc}\n\n原数据文件已保留，请勿删除。")
            root.destroy()
            return
        root.mainloop()
    finally:
        release_instance_lock(instance_lock)


if __name__ == "__main__":
    main()
