"""Offline regression cases for later manual use. Not executed by this change.

Only pure state/ICS functions are used: no credentials, API, app, or notifications.
"""
import copy
from datetime import datetime, timedelta
import unittest

from calendar_events import (build_event, build_followup_event, event_uid,
                             followup_event_uid, followup_offsets)
from icloud_store import (migrate_state, queue_schedule, queue_complete, queue_snooze,
                         begin_operation, begin_action, record_action_success,
                         record_operation_failure, retry_operation, next_display_due)
from reminder_store import new_state, add_task, collect_due, ready_delivery


NOW = datetime.fromisoformat("2026-10-13T10:00:00+08:00")
DUE = NOW + timedelta(hours=3)
CALENDAR_URL = "https://example.invalid/calendar/"
USERNAME = "offline-example@example.invalid"


def task_with_plan(state, offsets=None, recurrence=None):
    return add_task(state, "原始事项;保留\\正文\n第二行", DUE, recurrence,
                    smart={"title": "确认标题", "event_at": DUE.isoformat(),
                           "remind_before_minutes": 30, "priority": "high",
                           "followup_offsets_minutes": offsets or [],
                           "reason": "离线用例", "assumptions": []})


def schedule(state, task):
    return queue_schedule(state, task["id"], now=NOW,
                          calendar_url=CALENDAR_URL, username=USERNAME)


def acknowledge(state, operation, action):
    begin_action(state, operation["id"], action["id"])
    record_action_success(state, operation["id"], action["id"],
                          {"confirmed": True, "href": CALENDAR_URL + action["uid"] + ".ics",
                           "etag": '"offline"'}, now=NOW)


class CalendarSmartOfflineCases(unittest.TestCase):
    def test_advance_alarm_preserves_event_and_does_not_repeat(self):
        state = new_state()
        task = task_with_plan(state)
        calendar = build_event(task, NOW)
        self.assertEqual(task["due"], DUE.isoformat())
        self.assertIn("DTSTART:20261013T050000Z\r\n", calendar)
        self.assertIn("TRIGGER:-PT30M\r\n", calendar)
        self.assertEqual(calendar.count("BEGIN:VALARM"), 1)
        self.assertNotIn("RRULE:", calendar)
        self.assertIn("SUMMARY:确认标题\r\n", calendar)

    def test_legacy_no_smart_keeps_original_title_and_on_time_alarm(self):
        state = new_state()
        task = add_task(state, "原标题\n描述", DUE)
        calendar = build_event(task, NOW)
        self.assertIn("SUMMARY:原标题\r\n", calendar)
        self.assertIn("TRIGGER:PT0S\r\n", calendar)
        self.assertNotIn("smart", task)
        migrate_state(state)
        self.assertEqual(task["calendar"]["status"], "local_legacy")
        self.assertEqual(state["calendar_outbox"], [])

    def test_utf8_lines_and_text_escaping(self):
        state = new_state()
        task = add_task(state, "中文" * 100 + ";a,b\\c\n第二行", DUE)
        calendar = build_event(task, NOW)
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in calendar.split("\r\n")))
        unfolded = calendar.replace("\r\n ", "")
        self.assertIn("\\;a\\,b\\\\c\\n第二行", unfolded)

    def test_recurrences_use_one_relative_alarm_and_no_followups(self):
        rules = {"daily": "FREQ=DAILY", "weekly": "FREQ=WEEKLY",
                 "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "monthly": "FREQ=MONTHLY"}
        for recurrence, rule in rules.items():
            with self.subTest(recurrence=recurrence):
                state = new_state()
                task = task_with_plan(state, [60, 180], recurrence)
                calendar = build_event(task, NOW)
                self.assertIn("RRULE:" + rule + "\r\n", calendar)
                self.assertIn("TRIGGER:-PT30M\r\n", calendar)
                self.assertEqual(followup_offsets(task), [])
                operation = schedule(state, task)
                self.assertEqual(len(operation["actions"]), 1)

    def test_followups_are_single_events_at_offsets(self):
        state = new_state()
        task = task_with_plan(state, [60, 180])
        operation = schedule(state, task)
        self.assertEqual(len(task["followup_uids"]), 2)
        self.assertEqual(len(operation["actions"]), 3)
        first = build_followup_event(task, 0, NOW)
        self.assertIn("DTSTART:20261013T060000Z\r\n", first)
        self.assertIn("SUMMARY:待办跟进：确认标题\r\n", first)
        self.assertIn("TRIGGER:PT0S\r\n", first)
        self.assertNotIn("RRULE:", first)
        self.assertEqual(task["followup_uids"][0], followup_event_uid(task["id"], 1, 0))

    def test_all_actions_required_before_cloud_confirmation(self):
        state = new_state()
        task = task_with_plan(state, [60])
        operation = schedule(state, task)
        begin_operation(state, operation["id"])
        acknowledge(state, operation, operation["actions"][0])
        self.assertEqual(task["calendar"]["status"], "pending")
        acknowledge(state, operation, operation["actions"][1])
        self.assertEqual(task["calendar"]["status"], "confirmed")

    def test_retry_reuses_revision_uid_and_exact_content(self):
        state = new_state()
        task = task_with_plan(state, [60])
        operation = schedule(state, task)
        original = copy.deepcopy(operation["actions"])
        begin_operation(state, operation["id"])
        begin_action(state, operation["id"], operation["actions"][0]["id"])
        record_operation_failure(state, operation["id"], "响应丢失", uncertain=True)
        retry_operation(state, operation["id"])
        self.assertEqual(operation["revision"], 1)
        self.assertEqual([a["uid"] for a in operation["actions"]], [a["uid"] for a in original])
        self.assertEqual([a["content"] for a in operation["actions"]], [a["content"] for a in original])
        self.assertTrue(operation["needs_reconcile"])

    def test_complete_cancels_planned_unacknowledged_followups(self):
        state = new_state()
        task = task_with_plan(state, [60, 180])
        operation = schedule(state, task)
        planned = set(task["followup_uids"])
        begin_operation(state, operation["id"])
        begin_action(state, operation["id"], operation["actions"][0]["id"])
        record_operation_failure(state, operation["id"], "响应丢失", uncertain=True)
        cancellation = queue_complete(state, task["id"], NOW)
        self.assertTrue(planned.issubset({a["uid"] for a in cancellation["actions"]}))
        self.assertIn(event_uid(task["id"]), {a["uid"] for a in cancellation["actions"]})
        self.assertEqual(task["calendar"]["status"], "cancel_pending")

    def test_reschedule_creates_new_followups_then_deletes_old(self):
        state = new_state()
        task = task_with_plan(state, [60])
        schedule(state, task)
        old_uid = task["followup_uids"][0]
        task["due"] = (DUE + timedelta(hours=1)).isoformat()
        task["smart"]["event_at"] = task["due"]
        operation = schedule(state, task)
        self.assertNotEqual(task["followup_uids"][0], old_uid)
        self.assertEqual(operation["actions"][0]["method"], "PUT")
        self.assertEqual(operation["actions"][1]["method"], "PUT")
        self.assertIn(old_uid, {a["uid"] for a in operation["actions"] if a["method"] == "DELETE"})

    def test_calendar_task_never_enters_local_delivery_queue(self):
        state = new_state()
        task = task_with_plan(state, [60])
        operation = schedule(state, task)
        begin_operation(state, operation["id"])
        record_operation_failure(state, operation["id"], "离线", uncertain=True)
        self.assertEqual(collect_due(state, DUE + timedelta(days=1)), [])
        self.assertIsNone(ready_delivery(state, DUE + timedelta(days=1)))

    def test_next_display_dates_for_weekly_weekdays_and_monthly(self):
        for recurrence in ("weekly", "weekdays", "monthly"):
            state = new_state()
            task = task_with_plan(state, recurrence=recurrence)
            schedule(state, task)
            future = datetime.fromisoformat(next_display_due(task, DUE + timedelta(days=40)))
            self.assertGreater(future, DUE + timedelta(days=40))
        state = new_state()
        task = add_task(state, "每月31日", datetime.fromisoformat("2026-01-31T13:00:00+08:00"), "monthly")
        future = next_display_due(task, datetime.fromisoformat("2026-02-01T00:00:00+08:00"))
        self.assertEqual(future, "2026-03-31T13:00:00+08:00")

    def test_weekday_monday_at_six_uses_shanghai_recurrence_dates(self):
        state = new_state()
        due = datetime.fromisoformat("2026-10-19T06:00:00+08:00")
        task = add_task(state, "工作日早晨", due, "weekdays",
                        smart={"event_at": due.isoformat(), "remind_before_minutes": 0,
                               "followup_offsets_minutes": []})
        task["calendar"] = {"revision": 1, "series_start": due.isoformat(),
                            "exdates": [due.isoformat()]}
        calendar = build_event(task, NOW)
        self.assertIn("BEGIN:VTIMEZONE\r\nTZID:Asia/Shanghai\r\n", calendar)
        self.assertIn("DTSTART;TZID=Asia/Shanghai:20261019T060000\r\n", calendar)
        self.assertIn("DTEND;TZID=Asia/Shanghai:20261019T060500\r\n", calendar)
        self.assertIn("RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR\r\n", calendar)
        self.assertIn("EXDATE;TZID=Asia/Shanghai:20261019T060000\r\n", calendar)
        self.assertNotIn("DTSTART:20261018T220000Z", calendar)
        self.assertEqual(next_display_due(task, datetime.fromisoformat("2026-10-19T06:00:00+08:00")),
                         "2026-10-20T06:00:00+08:00")

    def test_monthly_first_at_six_does_not_recur_on_previous_utc_month_end(self):
        state = new_state()
        due = datetime.fromisoformat("2026-11-01T06:00:00+08:00")
        task = add_task(state, "每月1日早晨", due, "monthly",
                        smart={"event_at": due.isoformat(), "remind_before_minutes": 0,
                               "followup_offsets_minutes": []})
        calendar = build_event(task, NOW)
        self.assertIn("DTSTART;TZID=Asia/Shanghai:20261101T060000\r\n", calendar)
        self.assertIn("RRULE:FREQ=MONTHLY\r\n", calendar)
        self.assertNotIn("DTSTART:20261031T220000Z", calendar)
        self.assertEqual(next_display_due(task, datetime.fromisoformat("2026-11-02T00:00:00+08:00")),
                         "2026-12-01T06:00:00+08:00")

    def test_legacy_daily_remains_utc(self):
        state = new_state()
        due = datetime.fromisoformat("2026-10-19T06:00:00+08:00")
        task = add_task(state, "原有每日事项", due, "daily")
        calendar = build_event(task, NOW)
        self.assertIn("DTSTART:20261018T220000Z\r\n", calendar)
        self.assertNotIn("BEGIN:VTIMEZONE", calendar)


if __name__ == "__main__":
    unittest.main()
