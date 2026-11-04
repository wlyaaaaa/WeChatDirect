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
