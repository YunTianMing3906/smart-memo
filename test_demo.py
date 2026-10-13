from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from parser import SHANGHAI, parse_time_input, parse_editable_time
from reminder_store import (new_state, add_task, collect_due, ready_delivery,
                            finish_delivery, complete_task, snooze_task, save_state, load_state)
from desktop_demo import acquire_instance_lock, release_instance_lock, AlreadyRunningError

NOW = datetime(2026, 10, 13, 11, 40, tzinfo=SHANGHAI)  # Tuesday
TEST_DIRECTORY = Path(__file__).resolve().parent


class TimeParserTests(unittest.TestCase):
    def test_relative_minutes_preserve_seconds(self):
        now = NOW.replace(second=31)
        self.assertEqual(parse_time_input("2分钟后交材料", now).due, now + timedelta(minutes=2))
        self.assertEqual(parse_time_input("两分钟后", now).due, now + timedelta(minutes=2))

    def test_tomorrow_and_chinese_weekday(self):
        self.assertEqual(parse_time_input("明天14:30", NOW).due, datetime(2026, 10, 14, 14, 30, tzinfo=SHANGHAI))
        self.assertEqual(parse_time_input("周五上午10点", NOW).due, datetime(2026, 10, 16, 10, 0, tzinfo=SHANGHAI))
        self.assertEqual(parse_time_input("下周五下午三点半", NOW).due, datetime(2026, 10, 23, 15, 30, tzinfo=SHANGHAI))

    def test_daily_rollover_and_timezone(self):
        parsed = parse_time_input("每天9:00", NOW.astimezone(timezone.utc))
        self.assertEqual(parsed.recurrence, "daily")
        self.assertEqual(parsed.due, datetime(2026, 10, 14, 9, 0, tzinfo=SHANGHAI))
        self.assertEqual(parse_time_input("每天18:00", NOW).due.date(), NOW.date())

    def test_same_weekday_past_moves_to_next_week(self):
        self.assertEqual(parse_time_input("周二上午10点", NOW).due.day, 20)

    def test_invalid_and_missing_times_are_not_guessed(self):
        for text in ("买牛奶", "明天", "明天25:30", "每天09:99", "0分钟后", "十十分钟后", "2026-02-30 10:00"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_time_input(text, NOW)

    def test_editable_confirmation(self):
        self.assertEqual(parse_editable_time("2026-10-14 14:30"), datetime(2026, 10, 14, 14, 30, tzinfo=SHANGHAI))
        self.assertEqual(parse_editable_time("2026-10-14 14:30:15").second, 15)


class DueSelectionTests(unittest.TestCase):
    def test_overdue_after_sleep_once_and_future_excluded(self):
        state = new_state()
        overdue = add_task(state, "已到期", NOW - timedelta(hours=2))
        add_task(state, "将来", NOW + timedelta(minutes=2))
        completed = add_task(state, "完成", NOW - timedelta(minutes=1))
        complete_task(state, completed["id"])
        self.assertEqual([t["id"] for t in collect_due(state, NOW)], [overdue["id"]])
        self.assertEqual(collect_due(state, NOW), [])
        self.assertEqual(len(state["deliveries"]), 1)
        self.assertIsNotNone(ready_delivery(state, NOW))

    def test_daily_missed_days_coalesce_and_keep_original_time_after_snooze(self):
        state = new_state()
        task = add_task(state, "每天", NOW.replace(day=9, hour=9), "daily")
        self.assertEqual(len(collect_due(state, NOW)), 1)
        self.assertEqual(datetime.fromisoformat(task["due"]), NOW.replace(day=14, hour=9, minute=40))
        snooze_task(state, task["id"], NOW)
        collect_due(state, NOW + timedelta(minutes=11))
        self.assertEqual(datetime.fromisoformat(task["due"]).hour, 9)
        self.assertEqual(datetime.fromisoformat(task["due"]).minute, 40)

    def test_failure_retries_are_bounded_and_persist(self):
        state = new_state()
        add_task(state, "失败保留", NOW)
        collect_due(state, NOW)
        delivery = ready_delivery(state, NOW)
        for attempt in (1, 2, 3):
            delivery.update(status="sending", attempts=attempt)
            finish_delivery(delivery, NOW, "离线")
        self.assertEqual(delivery["status"], "failed")
        self.assertIsNone(ready_delivery(state, NOW + timedelta(days=1)))
        with tempfile.TemporaryDirectory(dir=TEST_DIRECTORY) as directory:
            path = Path(directory) / "data.json"
            save_state(path, state)
            restored = load_state(path)
            self.assertEqual(restored["deliveries"][0]["status"], "failed")
            self.assertEqual(restored["tasks"][0]["text"], "失败保留")

    def test_no_early_retry_and_completed_cancel(self):
        state = new_state()
        task = add_task(state, "任务", NOW)
        collect_due(state, NOW)
        delivery = state["deliveries"][0]
        delivery.update(status="sending", attempts=1)
        finish_delivery(delivery, NOW, "超时")
        self.assertIsNone(ready_delivery(state, NOW + timedelta(seconds=29)))
        self.assertIsNotNone(ready_delivery(state, NOW + timedelta(seconds=30)))
        complete_task(state, task["id"])
        self.assertEqual(delivery["status"], "cancelled")
        self.assertIsNone(ready_delivery(state, NOW + timedelta(days=1)))

    def test_restart_does_not_reset_attempt_budget(self):
        for attempts, expected in ((1, "waiting"), (3, "failed")):
            state = new_state()
            add_task(state, "任务", NOW)
            collect_due(state, NOW)
            state["deliveries"][0].update(status="sending", attempts=attempts)
            with tempfile.TemporaryDirectory(dir=TEST_DIRECTORY) as directory:
                path = Path(directory) / "data.json"
                save_state(path, state)
                restored = load_state(path)
            self.assertEqual(restored["deliveries"][0]["status"], expected)
            self.assertEqual(restored["deliveries"][0]["attempts"], attempts)


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_denied_until_first_releases(self):
        with tempfile.TemporaryDirectory(dir=TEST_DIRECTORY) as directory:
            path = Path(directory) / "instance.lock"
            first = acquire_instance_lock(path)
            try:
                with self.assertRaises(AlreadyRunningError):
                    acquire_instance_lock(path)
            finally:
                release_instance_lock(first)
            second = acquire_instance_lock(path)
            release_instance_lock(second)


if __name__ == "__main__":
    unittest.main()
