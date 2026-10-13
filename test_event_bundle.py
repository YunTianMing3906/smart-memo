"""Offline expansion tests for multi-event reminder bundles; no UI/API/network."""
import unittest
from datetime import datetime, timedelta

from event_bundle import EventExpansionError, expand_bundle, expand_event
from parser import SHANGHAI

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=SHANGHAI)


def event(series_id, title, event_at, recurrence, **extras):
    """A validated-style event: model fields plus the plan fields the expander reads."""
    result = {"id": series_id, "title": title, "event_at": event_at, "recurrence": recurrence,
              "remind_before_minutes": 30, "priority": "normal", "followup_offsets_minutes": [],
              "count": None, "until": None, "exceptions": [], "numbering_start": 1,
              "questions": [], "assumptions": [], "reason": "离线用例"}
    result.update(extras)
    return result


class EventBundleTests(unittest.TestCase):
    def test_party_meeting_bundle_expands_to_ten_meetings_plus_pickup(self):
        events = [
            event("s1", "例会", "2026-10-19T15:00:00+08:00", "weekly", count=10,
                  exceptions=[{"occurrence_at": "2026-12-14T15:00:00+08:00", "title": "例会（主持例会资料）"}]),
            event("s2", "跟负责人领资料", "2026-12-12T09:00:00+08:00", "none",
                  remind_before_minutes=0, priority="high"),
        ]
        plans = expand_bundle(events, NOW)
        self.assertEqual(len(plans), 11)
        meetings = [p for p in plans if p["source"]["series_id"] == "s1"]
        self.assertEqual(len(meetings), 10)
        self.assertEqual([p["event_at"][:10] for p in meetings],
                         ["2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09", "2026-11-16",
                          "2026-11-23", "2026-11-30", "2026-12-07", "2026-12-14", "2026-12-21"])
        self.assertTrue(all(p["recurrence"] == "none" for p in meetings))
        self.assertTrue(all(p["source"]["expanded"] for p in meetings))
        self.assertEqual([p["source"]["occurrence_index"] for p in meetings], list(range(1, 11)))
        titles = {p["event_at"][:10]: p["title"] for p in meetings}
        self.assertEqual(titles["2026-12-14"], "例会（主持例会资料）")
        self.assertEqual(titles["2026-10-19"], "例会")
        pickup = [p for p in plans if p["source"]["series_id"] == "s2"]
        self.assertEqual(len(pickup), 1)
        self.assertEqual(pickup[0]["title"], "跟负责人领资料")
        self.assertEqual(pickup[0]["event_at"], "2026-12-12T09:00:00+08:00")
        self.assertEqual(pickup[0]["priority"], "high")
        self.assertEqual(pickup[0]["remind_before_minutes"], 0)
        self.assertFalse(pickup[0]["source"]["expanded"])

    def test_numbering_start_shifts_display_only_not_dates(self):
        weekly = event("s1", "例会", "2026-10-19T15:00:00+08:00", "weekly",
                       count=10, numbering_start=2)
        plans = expand_event(weekly, NOW)
        self.assertEqual([p["event_at"][:10] for p in plans][:2], ["2026-10-19", "2026-10-26"])
        self.assertEqual([p["source"]["numbering_start"] for p in plans], [2] * 10)
        self.assertEqual([p["source"]["occurrence_index"] for p in plans], list(range(1, 11)))
        with self.assertRaises(EventExpansionError):
            expand_event(event("s1", "单次", "2026-10-19T15:00:00+08:00", "none",
                               numbering_start=2), NOW)
        with self.assertRaises(EventExpansionError):
            expand_event(event("s1", "例会", "2026-10-19T15:00:00+08:00", "weekly",
                               numbering_start=0), NOW)

    def test_count_and_until_whichever_comes_first(self):
        weekly = event("s1", "例会", "2026-10-19T15:00:00+08:00", "weekly",
                       count=2, until="2026-12-01T15:00:00+08:00")
        self.assertEqual(len(expand_event(weekly, NOW)), 2)
        weekly = event("s1", "例会", "2026-10-19T15:00:00+08:00", "weekly",
                       until="2026-11-02T15:00:00+08:00")
        self.assertEqual([p["event_at"][:10] for p in expand_event(weekly, NOW)],
                         ["2026-10-19", "2026-10-26", "2026-11-02"])

    def test_infinite_series_stays_one_recurring_plan(self):
        weekly = event("s1", "例会", "2026-10-19T15:00:00+08:00", "weekly")
        plans = expand_event(weekly, NOW)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["recurrence"], "weekly")
        self.assertFalse(plans[0]["source"]["expanded"])

    def test_infinite_series_with_exceptions_is_rejected(self):
        weekly = event("s1", "例会", "2026-10-19T15:00:00+08:00", "weekly",
                       exceptions=[{"occurrence_at": "2026-10-26T15:00:00+08:00", "title": "改期"}])
        with self.assertRaises(EventExpansionError):
            expand_event(weekly, NOW)

    def test_weekdays_and_monthly_finite_series(self):
        weekdays = event("s1", "晨会", "2026-10-19T09:00:00+08:00", "weekdays", count=5)
        self.assertEqual([p["event_at"][:10] for p in expand_event(weekdays, NOW)],
                         ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"])
        monthly = event("s1", "每月31日", "2026-01-31T13:00:00+08:00", "monthly", count=3)
        plans = expand_event(monthly, datetime(2026, 1, 1, 0, 0, tzinfo=SHANGHAI))
        self.assertEqual([p["event_at"][:10] for p in plans],
                         ["2026-01-31", "2026-03-31", "2026-05-31"])

    def test_past_start_weekend_weekdays_and_out_of_range_until_are_rejected(self):
        for changes in ({"event_at": "2026-10-12T15:00:00+08:00"},
                        {"recurrence": "weekdays", "event_at": "2026-10-17T15:00:00+08:00"},
                        {"until": "2026-10-18T15:00:00+08:00"},
                        {"until": "2027-10-20T15:00:00+08:00"}):
            candidate = dict(event("s1", "例会", "2026-10-19T15:00:00+08:00", "weekly"), **changes)
            with self.subTest(changes=changes), self.assertRaises(EventExpansionError):
                expand_event(candidate, NOW)

    def test_exception_outside_series_is_rejected(self):
        weekly = event("s1", "例会", "2026-10-19T15:00:00+08:00", "weekly", count=2,
                       exceptions=[{"occurrence_at": "2026-12-01T15:00:00+08:00", "title": "改期"}])
        with self.assertRaises(EventExpansionError):
            expand_event(weekly, NOW)

    def test_bundle_rejects_missing_or_duplicate_ids(self):
        first = event("s1", "甲", "2026-10-19T15:00:00+08:00", "none")
        second = event("s2", "乙", "2026-10-20T15:00:00+08:00", "none")
        missing = {key: value for key, value in first.items() if key != "id"}
        with self.assertRaises(EventExpansionError):
            expand_bundle([missing], NOW)
        with self.assertRaises(EventExpansionError):
            expand_bundle([first, second, dict(second, id="s1")], NOW)


if __name__ == "__main__":
    unittest.main()
