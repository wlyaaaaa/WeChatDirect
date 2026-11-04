"""Synthetic waits and fail-closed access checks; no native connection or chats."""
import isolation
import unittest
from unittest import mock
import wechat_cli as cli
from wechat_wait import wait_new
from test_wechat_paced import Clock


class WaitTests(unittest.TestCase):
    def run_wait(self, events=lambda _t: [], read=lambda: [], **options):
        clock = Clock()
        def event(seconds):
            clock.sleep(seconds)
            return events(clock.now())
        return wait_new(since=999, read=read, event=event, now=clock.now, sleep=clock.sleep, **options)

    def test_push_burst_waits_until_quiet_without_polling_or_exposing_events(self):
        read = mock.Mock(return_value=[])
        result = self.run_wait(lambda t: [{"is_self": False, "content": "event-only"}]
                              if t in (1001, 1011) else [], read=read)
        self.assertEqual((result["status"], result["waitedSeconds"]), ("new_messages", 26))
        self.assertEqual(result["new"], [])
        read.assert_called_once()

    def test_continuous_burst_has_sixty_second_cap(self):
        result = self.run_wait(lambda _t: [{"is_self": False}])
        self.assertEqual((result["status"], result["waitedSeconds"]), ("new_messages", 61))

    def test_owner_reply_cancels_until_other_speaks_again(self):
        for events, expected in (
                ({1001: [False], 1002: [True]}, ("timed_out", 60)),
                ({1001: [False], 1002: [True], 1003: [False]}, ("new_messages", 18)),
                ({1001: [True, False]}, ("new_messages", 16)),
                ({1001: [False, True]}, ("timed_out", 60)),
                ({1001: [None]}, ("new_messages", 16))):
            with self.subTest(events=events):
                result = self.run_wait(lambda t: [{"is_self": role} if role is not None else {}
                                                 for role in events.get(t, [])], max_minutes=1)
                self.assertEqual((result["status"], result["waitedSeconds"]), expected)

    def test_initial_read_uses_last_timestamp_and_preserves_both_roles(self):
        for role, status in (("self", "timed_out"), ("other", "new_messages")):
            messages = [{"createTime": 1001, "senderRole": role},
                        {"createTime": 1000, "senderRole": "other" if role == "self" else "self"}]
            result = self.run_wait(read=lambda: messages, max_minutes=1)
            self.assertEqual(result["status"], status)
            self.assertEqual(result["new"], sorted(messages, key=lambda m: m["createTime"]))

    def test_long_settle_waits_for_full_interval(self):
        for continuous in (False, True):
            result = self.run_wait(lambda t: [{"is_self": False}] if continuous or t == 1001 else [],
                                   settle_seconds=120)
            self.assertEqual((result["status"], result["waitedSeconds"]), ("new_messages", 121))

    def test_include_self_still_triggers_and_extends_quiet_period(self):
        result = self.run_wait(lambda t: [{"is_self": False}, {"is_self": True}] if t == 1002 else [],
                               read=lambda: [{"createTime": 1001, "senderRole": "self"}],
                               include_self=True, max_minutes=1)
        self.assertEqual((result["status"], result["waitedSeconds"]), ("new_messages", 17))

    def test_timeout_and_self_trigger_option(self):
        for is_self in (None, True):
            result = self.run_wait(lambda t: [{"is_self": is_self}]
                                   if is_self is not None and t == 1001 else [], max_minutes=0.1)
            self.assertEqual(result["status"], "timed_out")
        result = self.run_wait(lambda t: [{"is_self": True}] if t == 1001 else [], include_self=True)
        self.assertEqual((result["status"], result["waitedSeconds"]), ("new_messages", 16))

    def test_initial_read_retries_only_until_first_success(self):
        for sequence, expected in (([OSError("x")] * 3, "read_failed"),
                                   ([OSError("x"), OSError("x"), []], "timed_out")):
            reader = mock.Mock(side_effect=sequence)
            result = self.run_wait(read=reader, max_minutes=0.25)
            self.assertEqual(result["status"], expected)
            self.assertEqual(reader.call_count, 3)

    def test_broken_or_blocked_access_never_reads(self):
        bodies = ("return {'status': 'blocked', 'required_action': 'verify'}", "return []",
                  "raise RuntimeError('synthetic')", "syntax !", None)
        for index, body in enumerate(bodies):
            path = isolation.ROOT / f"synthetic_access_{index}.py"
            if body is not None:
                path.write_text(f"def check_access(level):\n    assert level == 'factor'\n    {body}\n", encoding="utf-8")
            with mock.patch.object(cli, "_local_settings", return_value={"personal_data_access": str(path)}):
                with self.assertRaises(cli.ProductError) as caught:
                    cli._check_personal_access()
                self.assertEqual(caught.exception.details["status"], "locked")

    def test_connection_failure_is_reported(self):
        events = mock.Mock(side_effect=OSError("synthetic-disconnect"))
        self.assertEqual(self.run_wait(events)["status"], "connection_lost")
