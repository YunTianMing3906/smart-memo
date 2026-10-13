"""Pure/mock verification examples; intentionally NOT executed during authoring.

No real API/account/credential is read or contacted. Explicitly run this module
only if the user later elects to verify it.
"""

import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

from deepseek_client import (
    API_ENDPOINT, DeepSeekClient, DeepSeekError, PlanValidationError,
    normalize_preferences, validate_bundle,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def model_event(**changes):
    result = {"id": "s1", "title": "明天打电话", "event_at": "2026-10-14T14:00:00+08:00",
              "recurrence": "none", "remind_before_minutes": 15, "priority": "normal",
              "followup_offsets_minutes": [], "count": None, "until": None, "exceptions": [],
              "numbering_start": 1}
    result.update(changes)
    return result


def model_bundle(**changes):
    result = {"events": [model_event()], "questions": [], "assumptions": [], "reason": "提前15分钟准备。"}
    result.update(changes)
    return result


class MockResponse(io.BytesIO):
    def getcode(self):
        return 200


class MockOpener:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return MockResponse(json.dumps(self.result, ensure_ascii=False).encode("utf-8"))


class DeepSeekClientContractTests(unittest.TestCase):
    def test_bundle_validation_normalizes_events(self):
        bundle = validate_bundle(model_bundle(), now=NOW)
        event = bundle["events"][0]
        self.assertEqual(event["title"], "明天打电话")
        self.assertEqual(event["event_at"], "2026-10-14T14:00:00+08:00")
        self.assertEqual(event["questions"], [])
        self.assertEqual(event["reason"], "提前15分钟准备。")

    def test_bundle_validation_rejects_bad_shapes(self):
        for changes in (
            {"events": []},
            {"events": [model_event(), model_event()]},
            {"events": [model_event(id="")]},
            {"events": [model_event(event_at="2026-10-14T06:00:00Z")]},
            {"events": [model_event(event_at="2026-10-13T11:00:00+08:00")]},
            {"events": [model_event(event_at=None)]},
            {"events": [model_event(count=3)]},
            {"events": [model_event(count=0)]},
            {"events": [model_event(count=101)]},
            {"events": [model_event(until="2026-12-01T00:00:00+08:00")]},
            {"events": [model_event(until="2026-12-01T00:00:00Z")]},
            {"events": [model_event(exceptions=[{"occurrence_at": "2026-12-14T15:00:00+08:00"}])]},
            {"events": [model_event(recurrence="weekly", followup_offsets_minutes=[30])]},
            {"events": [model_event(remind_before_minutes=True)]},
            {"events": [model_event(numbering_start=0)]},
            {"events": [model_event(numbering_start="2")]},
            {"events": [model_event(numbering_start=101)]},
            {"events": [model_event(numbering_start=2)]},
        ):
            with self.subTest(changes=changes), self.assertRaises(PlanValidationError):
                validate_bundle(model_bundle(**changes), now=NOW)

    def test_bundle_questions_require_all_events_missing(self):
        bundle = validate_bundle({"events": [model_event(event_at=None)], "questions": ["哪天？"],
                                  "assumptions": [], "reason": "缺时间。"}, now=NOW)
        self.assertEqual(bundle["questions"], ["哪天？"])
        with self.assertRaises(PlanValidationError):
            validate_bundle(model_bundle(questions=["哪天？"]), now=NOW)
        with self.assertRaises(PlanValidationError):
            validate_bundle({"events": [model_event(), model_event(event_at=None)],
                             "questions": ["哪天？"], "assumptions": [], "reason": "混合。"}, now=NOW)

    def test_finite_series_count_and_until_are_accepted(self):
        bundle = validate_bundle(model_bundle(events=[model_event(
            id="s1", event_at="2026-10-19T15:00:00+08:00", recurrence="weekly", count=10)]), now=NOW)
        self.assertEqual(bundle["events"][0]["count"], 10)
        bundle = validate_bundle(model_bundle(events=[model_event(
            id="s1", event_at="2026-10-19T15:00:00+08:00", recurrence="weekly",
            until="2026-11-30T15:00:00+08:00")]), now=NOW)
        self.assertEqual(bundle["events"][0]["until"], "2026-11-30T15:00:00+08:00")

    def test_series_numbering_start_is_accepted_and_normalized(self):
        bundle = validate_bundle(model_bundle(events=[model_event(
            id="s1", event_at="2026-10-19T15:00:00+08:00", recurrence="weekly",
            count=10, numbering_start=2)]), now=NOW)
        self.assertEqual(bundle["events"][0]["numbering_start"], 2)
        bundle = validate_bundle(model_bundle(), now=NOW)
        self.assertEqual(bundle["events"][0]["numbering_start"], 1)

    def test_request_has_one_current_note_and_no_preferences_secrets(self):
        client = DeepSeekClient("fake-test-key")
        mock = MockOpener({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(model_bundle(), ensure_ascii=False)}}]})
        client._opener = mock
        result = client.interpret("明天下午2点打电话", now=NOW, preferences={"guidance": "不打扰午休", "api_key": "must-never-be-sent", "history": ["must-never-be-sent"]})
        self.assertEqual(result["events"][0]["event_at"], "2026-10-14T14:00:00+08:00")
        request, timeout = mock.requests[0]
        self.assertEqual(request.full_url, API_ENDPOINT)
        self.assertEqual(timeout, 45)
        payload = json.loads(request.data)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("must-never-be-sent", request.data.decode())
        self.assertEqual(len(mock.requests), 1)

    def test_finish_reason_and_empty_content_are_rejected(self):
        for finish, content in (("length", json.dumps(model_bundle())), ("aborted", json.dumps(model_bundle())), ("stop", "  "), ("stop", "```json\n{}\n```")):
            client = DeepSeekClient("fake-test-key")
            client._opener = MockOpener({"choices": [{"finish_reason": finish, "message": {"content": content}}]})
            with self.subTest(finish=finish), self.assertRaises(DeepSeekError):
                client.interpret("明天14点提醒", now=NOW)

    def test_http_errors_are_sanitized_and_never_retried(self):
        for status in (401, 402, 429, 302):
            client = DeepSeekClient("fake-secret-key")
            mock = MockOpener(error=urllib.error.HTTPError(API_ENDPOINT, status, "fake-secret-key and user text", {}, io.BytesIO(b"private body")))
            client._opener = mock
            with self.subTest(status=status), self.assertRaises(DeepSeekError) as context:
                client.interpret("private user text", now=NOW)
            self.assertEqual(len(mock.requests), 1)
            self.assertNotIn("fake-secret-key", str(context.exception))
            self.assertNotIn("private", str(context.exception))

    def test_conditional_events_do_not_get_fabricated_times(self):
        client = DeepSeekClient("fake-test-key")
        client._opener = MockOpener({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(model_bundle(), ensure_ascii=False)}}]})
        result = client.interpret("收到客户邮件后提醒我回复", now=NOW)
        self.assertIsNone(result["events"][0]["event_at"])
        self.assertTrue(result["questions"])

    def test_preferences_are_explicitly_allowlisted(self):
        self.assertEqual(normalize_preferences({"default_event_time": "09:00", "username": "private", "password": "private"}), {"default_event_time": "09:00"})
        self.assertEqual(normalize_preferences({"quiet_hours": {"start": 22, "end": 8}}), {"quiet_hours": {"start": 22, "end": 8}})
        self.assertEqual(normalize_preferences({"quiet_hours": None}), {"quiet_hours": None})


if __name__ == "__main__":
    unittest.main()
