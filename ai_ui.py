"""DeepSeek draft/review UI. Model responses never write the calendar directly."""
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import math
import threading
import tkinter as tk
from tkinter import ttk

from parser import local_now, parse_editable_time, as_local
from deepseek_client import DeepSeekClient, AISettingsStore, validate_plan, DeepSeekError
from event_bundle import expand_bundle, EventExpansionError
from app_paths import app_dir

AI_FILE = app_dir() / "deepseek-credentials.dpapi.json"
REPEATS = {"不重复": "none", "每天": "daily", "每周": "weekly", "工作日": "weekdays", "每月": "monthly"}
PRIORITIES = {"普通": "normal", "重要": "high", "低": "low"}
DEFAULT_PREFERENCES = {
    "guidance": "事项没有明确时间时先问我。优先给出简洁、实用的提醒建议。",
    "default_remind_before_minutes": 30, "default_priority": "normal",
    "quiet_hours": {"start": 22, "end": 8},
}


def quiet_window(value):
    if not value:
        return None
    if not isinstance(value, dict):
        raise ValueError("静默时段格式无效，请在配置DeepSeek中重新保存偏好。")
    def minute(clock):
        if type(clock) is int and 0 <= clock <= 23:
            return clock * 60
        if isinstance(clock, str):
            parts = clock.split(":")
            if len(parts) == 2 and all(part.isdigit() for part in parts):
                hour, minutes = map(int, parts)
                if 0 <= hour <= 23 and 0 <= minutes <= 59:
                    return hour * 60 + minutes
        raise ValueError("静默时段格式无效，请重新保存偏好。")
    start, end = minute(value.get("start")), minute(value.get("end"))
    if start == end:
        raise ValueError("静默时段的开始与结束不能相同。")
    return start, end


def reviewed_plan(plan, event_at, title, recurrence, before_minutes, priority,
                  followup_text="", enable_followups=False, quiet_hours=None, now=None):
    """Recompute all editable dates, with quiet hours limited to opted-in followups."""
    now = as_local(now or local_now())
    event_at = as_local(event_at).replace(microsecond=0)
    if not title.strip():
        raise ValueError("请填写事项标题。")
    if recurrence == "weekdays" and event_at.weekday() >= 5:
        raise ValueError("工作日事项的首个日期应为周一至周五，请调整事件日期。")
    if isinstance(before_minutes, bool) or not 0 <= before_minutes <= 10080:
        raise ValueError("提前量请填写0到10080之间的整数分钟。")
    first_alert = event_at - timedelta(minutes=before_minutes)
    if first_alert <= now:
        raise ValueError("首次提醒时间已经过去，请减小提前量或把事件时间改到将来。")
    offsets = []
    if enable_followups:
        if recurrence != "none":
            raise ValueError("此版只为一次事项安排额外跟进，重复事项按其重复规则提醒。")
        try:
            offsets = sorted(set(int(value.strip()) for value in followup_text.replace("，", ",").split(",") if value.strip()))
        except ValueError as exc:
            raise ValueError("跟进间隔请填写整数分钟，用逗号分隔，例如30,120。") from exc
        if not offsets or len(offsets) > 3 or any(value < 1 or value > 10080 for value in offsets):
            raise ValueError("跟进请填写1到3个时刻，每个在事件后1分钟至7天内。")
        shifted = []
        for offset in offsets:
            point = event_at + timedelta(minutes=offset)
            window = quiet_window(quiet_hours)
            if window:
                start, end = window
                clock = point.hour * 60 + point.minute
                if start > end and clock >= start:
                    point = (point + timedelta(days=1)).replace(hour=end // 60, minute=end % 60, second=0)
                elif (start > end and clock < end) or (start < end and start <= clock < end):
                    point = point.replace(hour=end // 60, minute=end % 60, second=0)
            adjusted = math.ceil((point - event_at).total_seconds() / 60)
            if not 1 <= adjusted <= 10080:
                raise ValueError("静默时段调整后的跟进超出7天，请缩短跟进间隔。")
            shifted.append(adjusted)
        offsets = sorted(set(shifted))
    source = plan.get("source")
    result = {key: value for key, value in plan.items() if key != "source"}
    result.update(title=title.strip(), event_at=event_at.isoformat(),
                  recurrence=recurrence, remind_before_minutes=before_minutes,
                  priority=priority, followup_offsets_minutes=offsets, questions=[])
    result = validate_plan(result, now=now)
    if source is not None:
        result["source"] = source
    return result, first_alert, [event_at + timedelta(minutes=offset) for offset in offsets]


class AIAssistantUI:
    def install_ai(self, connection_row, ai_row):
        self.ai_busy = False
        self.ai_request_id = 0
        self.ai_button = ttk.Button(ai_row, text="AI识别", command=self.ai_recognize)
        self.ai_button.pack(side="right")
        ttk.Button(connection_row, text="配置 DeepSeek", command=self.configure_ai).pack(side="right", padx=8)

    def load_ai_settings(self):
        return AISettingsStore(AI_FILE).load()

    def configure_ai(self):
        if self.ai_busy:
            self.set_status("AI正在识别，请等待结果后修改连接。")
            return
        if getattr(self, "ai_config_dialog", None) and self.ai_config_dialog.winfo_exists():
            self.ai_config_dialog.lift()
            return
        try:
            stored = self.load_ai_settings() or {}
        except Exception:
            stored = {}
        dialog = self.ai_config_dialog = tk.Toplevel(self.root)
        dialog.title("配置 DeepSeek")
        dialog.geometry("620x460")
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="只在点击“AI识别”时发送当前事项与必要时间信息。", wraplength=570).pack(anchor="w")
        ttk.Label(frame, text="DeepSeek API 密钥（已保存时可留空）").pack(anchor="w", pady=(14, 5))
        key = tk.StringVar()
        ttk.Entry(frame, textvariable=key, show="●").pack(fill="x")
        ttk.Label(frame, text="模型").pack(anchor="w", pady=(12, 5))
        model = tk.StringVar(value=stored.get("model", "deepseek-flash"))
        ttk.Combobox(frame, textvariable=model, values=("deepseek-flash", "deepseek-v4-pro")).pack(fill="x")
        preferences = dict(DEFAULT_PREFERENCES, **stored.get("preferences", {}))
        ttk.Label(frame, text="我的提醒偏好（可选）").pack(anchor="w", pady=(12, 5))
        guidance = tk.Text(frame, height=3, wrap="word")
        guidance.insert("1.0", preferences.get("guidance", ""))
        guidance.pack(fill="x")
        quiet = tk.BooleanVar(value=bool(preferences.get("quiet_hours")))
        ttk.Checkbutton(frame, text="额外跟进尽量避开22:00—08:00", variable=quiet).pack(anchor="w", pady=10)
        hint = tk.StringVar(value="密钥使用当前Windows用户加密保存。保存配置不会调用模型。")
        ttk.Label(frame, textvariable=hint, wraplength=570).pack(anchor="w")
        def save():
            if self.ai_busy:
                hint.set("AI正在识别，请等结果返回后再保存配置。")
                return
            secret = key.get().strip() or stored.get("api_key", "")
            if not secret:
                hint.set("请填写 DeepSeek API 密钥。")
                return
            prefs = dict(preferences, guidance=guidance.get("1.0", "end").strip(),
                         quiet_hours={"start": 22, "end": 8} if quiet.get() else None)
            try:
                AISettingsStore(AI_FILE).save(secret, model.get().strip(), prefs)
            except Exception as exc:
                hint.set(str(exc))
                return
            key.set("")
            dialog.destroy()
            self.set_status("DeepSeek 配置已保存。写下一件事后，点击“AI识别”。")
        ttk.Button(frame, text="保存配置", command=save).pack(anchor="e", pady=14)

    def ai_input_snapshot(self):
        return (self.memo.get("1.0", "end").strip(),)

    def ai_recognize(self):
        if self.ai_busy:
            return
        snapshot = self.ai_input_snapshot()
        if not snapshot[0]:
            self.set_status("先写下需要提醒的事项，再点击AI识别。", True)
            return
        try:
            settings = self.load_ai_settings()
            if not settings:
                raise ValueError("请先点击“配置 DeepSeek”，在程序里保存 API 密钥。")
        except Exception as exc:
            self.set_status(str(exc), True)
            return
        self.ai_busy = True
        self.ai_request_id += 1
        request_id = self.ai_request_id
        self.ai_button.configure(state="disabled")
        self.set_status("DeepSeek正在理解这条事项，结果会先给你核对……")
        now = local_now()
        submitted = snapshot[0]
        def worker():
            try:
                plan = DeepSeekClient(settings["api_key"], settings["model"]).interpret(submitted, now, settings["preferences"])
                self.events.put(("ai_plan", {"request_id": request_id, "snapshot": snapshot, "plan": plan,
                                            "preferences": settings["preferences"]}, None))
            except Exception as exc:
                self.events.put(("ai_plan", {"request_id": request_id, "snapshot": snapshot}, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def handle_ai_event(self, payload, error):
        if payload["request_id"] != self.ai_request_id:
            return
        self.ai_busy = False
        self.ai_button.configure(state="normal")
        if payload["snapshot"] != self.ai_input_snapshot():
            self.set_status("输入已改变，刚才的AI结果未应用。请重新识别。")
            return
        if error:
            self.set_status(error, True)
            return
        plan = payload["plan"]
        if plan["questions"]:
            self.show_ai_questions(plan, payload["snapshot"])
        else:
            self.show_ai_review(plan, payload["snapshot"][0], payload["preferences"])

    def show_ai_questions(self, plan, snapshot):
        dialog = tk.Toplevel(self.root)
        dialog.title("还需要补充一点信息")
        dialog.geometry("580x370")
        dialog.transient(self.root)
        dialog.grab_set()
        box = ttk.Frame(dialog, padding=20)
        box.pack(fill="both", expand=True)
        footer = ttk.Frame(box)
        footer.pack(side="bottom", fill="x", pady=8)
        question = tk.Text(box, height=5, wrap="word", relief="flat")
        known = [f"• {event['title']}：{event.get('event_at') or event.get('event_date') or '时间待确认'}" +
                 (f"，共{event['count']}次" if event.get("count") else "") for event in plan.get("events", [])]
        question.insert("1.0", ("已识别的事项：\n" + "\n".join(known) + "\n\n还需补充：\n" if known else "") + "\n".join(plan["questions"]))
        question.configure(state="disabled")
        question.pack(fill="x")
        answer = tk.Text(box, height=4, wrap="word")
        answer.pack(fill="x", pady=12)
        def apply():
            value = answer.get("1.0", "end").strip()
            if value:
                self.memo.delete("1.0", "end")
                self.memo.insert("1.0", snapshot[0] + "\n补充信息：" + value)
            dialog.destroy()
            self.set_status("补充后再点AI识别；目前没有保存任何日程。")
        ttk.Button(footer, text="返回并补充描述", command=apply).pack(anchor="e")

    def show_ai_review(self, bundle, original_text, preferences):
        try:
            expanded = expand_bundle(bundle["events"], now=local_now())
        except EventExpansionError as exc:
            self.set_status(str(exc), True)
            return
        totals = {}
        for plan in expanded:
            series_id = plan["source"]["series_id"]
            totals[series_id] = totals.get(series_id, 0) + 1
        repeat_labels = {value: label for label, value in REPEATS.items()}
        dialog = tk.Toplevel(self.root)
        dialog.title("AI识别结果 · 核对后保存")
        dialog.geometry("1060x720")
        dialog.minsize(920, 600)
        dialog.transient(self.root)
        dialog.grab_set()
        box = ttk.Frame(dialog, padding=16)
        box.pack(fill="both", expand=True)
        footer = ttk.Frame(box)
        footer.pack(side="bottom", fill="x", pady=(8, 0))
        notes = bundle["reason"]
        if bundle["assumptions"]:
            notes += "\n假设：" + "；".join(bundle["assumptions"])
        if any(plan["source"].get("title_overridden") for plan in expanded):
            notes += "\n个别场次的标题已按你的说明调整，请逐条核对。"
        header = tk.Text(box, height=3, wrap="word", relief="flat")
        header.insert("1.0", notes)
        header.configure(state="disabled")
        header.pack(fill="x", pady=(0, 8))
        heads = ttk.Frame(box)
        heads.pack(fill="x", pady=(0, 4))
        for text, width in (("保留", 4), ("来源", 16), ("标题", 28), ("事件时间", 19),
                            ("提前分钟", 7), ("重要程度", 6), ("跟进分钟(可选)", 14)):
            ttk.Label(heads, text=text, width=width).pack(side="left", padx=(0, 6))
        table = ttk.Frame(box)
        table.pack(fill="both", expand=True)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        canvas = tk.Canvas(table, highlightthickness=0, background="#f3f5f7")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=canvas.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=canvas.xview)
        rows_frame = ttk.Frame(canvas, style="App.TFrame")
        rows_frame.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")

        def wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Enter>", lambda event: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda event: canvas.unbind_all("<MouseWheel>"))
        dialog.bind("<Destroy>", lambda event: canvas.unbind_all("<MouseWheel>"))
        rows = []
        for plan in expanded:
            source = plan["source"]
            numbering_start = source.get("numbering_start", 1) or 1
            if source["series_recurrence"] == "none":
                caption = "单次"
            elif source["expanded"]:
                display = numbering_start + source["occurrence_index"] - 1
                total = numbering_start + totals[source["series_id"]] - 1
                caption = f"{source['series_title']}·第{display}/{total}场"
            else:
                caption = f"{source['series_title']}·{repeat_labels[source['series_recurrence']]}"
                if numbering_start > 1:
                    caption += f"·从第{numbering_start}场起"
            frame = ttk.Frame(rows_frame, style="App.TFrame")
            frame.pack(fill="x", pady=3)
            keep = tk.BooleanVar(value=True)
            ttk.Checkbutton(frame, variable=keep, takefocus=False).pack(side="left", padx=(0, 2))
            tk.Label(frame, text=caption[:19], width=18, anchor="w", background="#f3f5f7",
                     foreground="#214d75").pack(side="left", padx=(0, 6))
            title = tk.StringVar(value=plan["title"])
            ttk.Entry(frame, textvariable=title, width=28).pack(side="left", padx=(0, 6))
            event_time = tk.StringVar(value=datetime.fromisoformat(plan["event_at"]).strftime("%Y-%m-%d %H:%M:%S"))
            ttk.Entry(frame, textvariable=event_time, width=19).pack(side="left", padx=(0, 6))
            before = tk.StringVar(value=str(plan["remind_before_minutes"]))
            ttk.Entry(frame, textvariable=before, width=7).pack(side="left", padx=(0, 6))
            priority = tk.StringVar(value=next(name for name, value in PRIORITIES.items() if value == plan["priority"]))
            ttk.Combobox(frame, textvariable=priority, values=tuple(PRIORITIES), width=5, state="readonly").pack(side="left", padx=(0, 6))
            followups = tk.StringVar(value=",".join(str(value) for value in plan["followup_offsets_minutes"]))
            followup_entry = ttk.Entry(frame, textvariable=followups, width=13)
            followup_entry.pack(side="left")
            if plan["recurrence"] != "none":
                followup_entry.configure(state="disabled")
            rows.append(SimpleNamespace(plan=plan, keep=keep, title=title, event_time=event_time,
                                        before=before, priority=priority, followups=followups))
        preview = tk.StringVar(value=f"共{len(rows)}条日程，请逐条核对后保存。")
        ttk.Label(footer, textvariable=preview, wraplength=980, foreground="#42596c").pack(anchor="w", pady=(4, 4))
        controls = ttk.Frame(footer)
        controls.pack(fill="x")
        last_preview = {"signature": None}

        def draft_for(row):
            return reviewed_plan(row.plan, parse_editable_time(row.event_time.get()), row.title.get(),
                                 row.plan["recurrence"], int(row.before.get()), PRIORITIES[row.priority.get()],
                                 row.followups.get(), bool(row.followups.get().strip()),
                                 preferences.get("quiet_hours"))

        def refresh():
            kept, firsts, errors = [], [], []
            # Preserve an independent item's clock while recomputing a relative
            # date if its referenced meeting instance was edited in the preview.
            for row in rows:
                relation = row.plan["source"].get("relation")
                if not relation:
                    continue
                target = next((candidate for candidate in rows
                               if candidate.plan["source"]["series_id"] == relation["event_id"]
                               and candidate.plan["source"].get("reference_occurrence_at") == relation["occurrence_at"]), None)
                if target:
                    try:
                        anchor = parse_editable_time(target.event_time.get())
                        previous = parse_editable_time(row.event_time.get())
                        date = (anchor + timedelta(days=relation["offset_days"])).date()
                        updated = previous.replace(year=date.year, month=date.month, day=date.day)
                        row.event_time.set(updated.strftime("%Y-%m-%d %H:%M:%S"))
                    except ValueError:
                        errors.append("关联事项的日期需要先确认。")
            occurrences = set()
            for index, row in enumerate(rows, start=1):
                if not row.keep.get():
                    continue
                try:
                    reviewed, first, later = draft_for(row)
                    key = (row.plan["source"]["series_id"], reviewed["event_at"])
                    if key in occurrences:
                        raise ValueError("同一系列的两场日期时间重合，请调整。")
                    occurrences.add(key)
                    kept.append((row, reviewed))
                    firsts.append(first)
                except (ValueError, KeyError, DeepSeekError) as exc:
                    errors.append(f"第{index}条：{exc}")
            if errors:
                preview.set("\n".join(errors))
                return None
            if not kept:
                preview.set("请至少保留一条日程。")
                return None
            shown = "、".join(point.strftime("%m月%d日 %H:%M") for point in firsts[:6])
            if len(firsts) > 6:
                shown += "……"
            preview.set(f"保留{len(kept)}条。首次提醒：{shown}")
            last_preview["signature"] = tuple((row.keep.get(), row.title.get(), row.event_time.get(), row.before.get(),
                                               row.priority.get(), row.followups.get()) for row in rows)
            return kept

        def save():
            previous_signature = last_preview["signature"]
            kept = refresh()
            if kept is None:
                return
            if previous_signature != last_preview["signature"]:
                preview.set("日期或内容已改变，关联日期已重新计算。请核对更新后的清单，再点一次确认保存。")
                return
            entries = []
            for row, reviewed in kept:
                smart = {key: reviewed[key] for key in ("title", "event_at", "remind_before_minutes",
                                                        "priority", "followup_offsets_minutes",
                                                        "reason", "assumptions")}
                smart["source"] = row.plan["source"]
                entries.append({"due": datetime.fromisoformat(reviewed["event_at"]),
                                "recurrence": None if reviewed["recurrence"] == "none" else reviewed["recurrence"],
                                "smart": smart})
            if self.queue_new_calendar_bundle(original_text, entries):
                self.memo.delete("1.0", "end")
                dialog.destroy()
                self.set_status(f"已确认{len(entries)}条日程，正在逐条同步 iCloud；等每条显示“iCloud已保存”后再关机。")

        ttk.Button(controls, text="取消", command=dialog.destroy).pack(side="left")
        ttk.Button(controls, text="刷新提醒预览", command=refresh).pack(side="left", padx=10)
        ttk.Button(controls, text="确认并同步 iCloud", command=save).pack(side="right")
        refresh()
