"""Offline regression cases. Prepared for later use; not executed this round."""
import copy
import unittest
from datetime import datetime, timedelta
from calendar_events import build_event
from icloud_client import events_equivalent
from icloud_store import (migrate_state, queue_schedule, queue_complete, begin_operation,
                          begin_action, next_action, record_action_success,
                          record_operation_failure, ready_operation)
from reminder_store import new_state, add_task, collect_due, ready_delivery
from parser import SHANGHAI

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=SHANGHAI)
CALENDAR = "https://p01-caldav.icloud.com/example/calendars/memo/"
ACCOUNT = "example@example.com"


class CalendarSemanticsTests(unittest.TestCase):
    def event(self):
        state = new_state()
        task = add_task(state, "交图纸；检查尺寸\n附注", NOW + timedelta(days=20), "daily")
        queue_schedule(state, task["id"], NOW, CALENDAR, ACCOUNT)
        return state["calendar_outbox"][0]["actions"][0]["content"]

    def test_time_rule_and_relative_alarm_must_survive(self):
        ics = self.event()
        self.assertTrue(events_equivalent(ics, ics.replace("TRIGGER:PT0S", "TRIGGER;RELATED=START:-PT0S")))
        self.assertFalse(events_equivalent(ics, ics.replace("TRIGGER:PT0S", "TRIGGER;RELATED=END:PT0S")))
        self.assertFalse(events_equivalent(ics, ics.replace("RRULE:FREQ=DAILY\r\n", "")))
        self.assertFalse(events_equivalent(ics, ics.replace("TRIGGER:PT0S", "TRIGGER:-PT5M")))
        self.assertFalse(events_equivalent(ics, ics.replace("BEGIN:VALARM", "BEGIN:X-NO-ALARM")))


class CalendarOutboxTests(unittest.TestCase):
    def state(self):
        state = new_state()
        task = add_task(state, "事项", NOW + timedelta(days=30))
        op = queue_schedule(state, task["id"], NOW, CALENDAR, ACCOUNT)
        return state, task, op

    def test_queueing_another_task_does_not_recover_live_write(self):
        state, task, op = self.state()
        begin_operation(state, op["id"])
        action = next_action(state, op["id"])
        begin_action(state, op["id"], action["id"])
        other = add_task(state, "另一个事项", NOW + timedelta(days=40))
        queue_schedule(state, other["id"], NOW, CALENDAR, ACCOUNT)
        self.assertEqual(op["status"], "sending")
        self.assertEqual(action["status"], "sending")

    def test_restart_needs_confirmation_and_keeps_frozen_destination(self):
        state, task, op = self.state()
        begin_operation(state, op["id"])
        begin_action(state, op["id"], next_action(state, op["id"])["id"])
        migrate_state(state, recover_inflight=True)
        self.assertEqual(op["status"], "uncertain")
        self.assertIsNone(ready_operation(state))
        self.assertEqual(op["calendar_url"], CALENDAR)
        self.assertEqual(op["username"], ACCOUNT)

    def test_calendar_tasks_never_fall_back_to_local_phone_delivery(self):
        state, task, op = self.state()
        record_operation_failure(state, op["id"], "连接失败")
        state["deliveries"].append({"id": "old", "task_id": task["id"], "occurrence": task["due"],
                                    "status": "waiting", "attempts": 0, "next_attempt": NOW.isoformat()})
        self.assertEqual(collect_due(state, NOW + timedelta(days=31)), [])
        self.assertIsNone(ready_delivery(state, NOW + timedelta(days=31)))

    def test_completion_can_cancel_a_write_with_unknown_result(self):
        state, task, op = self.state()
        record_operation_failure(state, op["id"], "超时", uncertain=True)
        cancellation = queue_complete(state, task["id"], NOW)
        self.assertEqual(op["status"], "superseded")
        self.assertEqual(ready_operation(state)["id"], cancellation["id"])
        self.assertEqual(cancellation["actions"][0]["method"], "DELETE")

    def test_http_ack_alone_is_not_calendar_confirmation(self):
        state, task, op = self.state()
        action_id = op["actions"][0]["id"]
        with self.assertRaises(ValueError):
            record_action_success(state, op["id"], action_id, {"href": CALENDAR + "event.ics"}, NOW)
        self.assertEqual(task["calendar"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
