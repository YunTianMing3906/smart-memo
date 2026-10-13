"""Offline draft review regressions; no UI, keys, API or calendars are used."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock
from types import SimpleNamespace

from ai_ui import reviewed_plan, AIAssistantUI
from parser import SHANGHAI
from calendar_events import build_event
from icloud_client import events_equivalent
from deepseek_client import _conditional_guard

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=SHANGHAI)


def plan():
    return {"title": "开会", "event_at": "2026-10-14T15:00:00+08:00", "recurrence": "none",
            "remind_before_minutes": 30, "priority": "normal", "followup_offsets_minutes": [],
            "questions": [], "assumptions": [], "reason": "预留准备时间。"}


class ReviewTests(unittest.TestCase):
    def test_first_alert_is_distinct_from_event_time(self):
        event = datetime(2026, 10, 14, 15, 0, tzinfo=SHANGHAI)
        reviewed, first, followups = reviewed_plan(plan(), event, "开会", "none", 30, "normal", now=NOW)
        self.assertEqual(reviewed["event_at"], event.isoformat())
        self.assertEqual(first, event - timedelta(minutes=30))
        self.assertEqual(followups, [])

    def test_quiet_adjustments_deduplicate_and_round_into_allowed_window(self):
        event = datetime(2026, 10, 13, 21, 50, 30, tzinfo=SHANGHAI)
        reviewed, first, points = reviewed_plan(plan(), event, "检查事项", "none", 0, "high",
              "20,30", True, {"start": 22, "end": 8}, NOW)
        self.assertEqual(len(points), 1)
        self.assertEqual((points[0].hour, points[0].minute, points[0].second), (8, 0, 30))
        self.assertEqual(reviewed["followup_offsets_minutes"], [610])

    def test_past_first_alert_and_weekend_workday_are_rejected(self):
        with self.assertRaises(ValueError):
            reviewed_plan(plan(), NOW + timedelta(minutes=10), "事项", "none", 30, "normal", now=NOW)
        with self.assertRaises(ValueError):
            reviewed_plan(plan(), datetime(2026, 10, 17, 9, 0, tzinfo=SHANGHAI), "事项", "weekdays", 0, "normal", now=NOW)

    def test_explicit_fixed_time_clarification_does_not_loop(self):
        text = "只有收到邮件后才提醒我\n补充信息：改成明天15点固定检查"
        self.assertEqual(_conditional_guard(text, plan())["event_at"], plan()["event_at"])
        self.assertIsNone(_conditional_guard("审批通过后提醒我", plan())["event_at"])

    def test_changed_input_discards_old_model_response(self):
        ui = SimpleNamespace(ai_request_id=1, ai_busy=True, ai_button=Mock(),
             ai_input_snapshot=lambda: ("新的事项",),
             set_status=Mock(), show_ai_review=Mock(), show_ai_questions=Mock())
        AIAssistantUI.handle_ai_event(ui, {"request_id": 1, "snapshot": ("旧事项",),
                                        "plan": plan(), "preferences": {}}, None)
        ui.show_ai_review.assert_not_called()
        ui.show_ai_questions.assert_not_called()

    def test_alarm_units_may_normalize_but_trigger_relation_must_not_change(self):
        task = {"id": "example", "text": "开会", "due": plan()["event_at"], "recurrence": None,
                "calendar": {"revision": 1}, "smart": plan()}
        ics = build_event(task, NOW)
        self.assertTrue(events_equivalent(ics, ics.replace("TRIGGER:-PT30M", "TRIGGER:-PT1800S")))
        self.assertFalse(events_equivalent(ics, ics.replace("TRIGGER:-PT30M", "TRIGGER;RELATED=END:-PT30M")))


if __name__ == "__main__":
    unittest.main()
