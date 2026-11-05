"""Synthetic waits and fail-closed access checks; no native connection or chats."""
import isolation
import io
import json
import unittest
from unittest import mock
import wechat_cli as cli
import wechat_helper as helper
from wechat_wait import wait_login, wait_new
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
        self.assertEqual(read.call_count, 2)  # connect, then the record check just before waking

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
                                   ([OSError("x"), OSError("x"), [], []], "timed_out")):
            reader = mock.Mock(side_effect=sequence)
            result = self.run_wait(read=reader, max_minutes=0.25)
            self.assertEqual(result["status"], expected)
            self.assertEqual(reader.call_count, min(len(sequence), 4))

    def test_message_reaching_the_database_after_the_first_read_still_wakes(self):
        late = {"createTime": 1001, "senderRole": "other"}
        reader = mock.Mock(side_effect=[[], [late], [late]])
        result = self.run_wait(read=reader, max_minutes=1)
        reader.side_effect = [[], [late], [late]]
        reader.reset_mock()
        result = self.run_wait(read=reader, max_minutes=1)
        self.assertEqual((result["status"], result["new"], reader.call_count), ("new_messages", [late], 3))

    def test_owner_reply_only_in_the_record_cancels_the_wake(self):
        # Private chats deliver no self events, so only the record shows the owner answered.
        other = {"createTime": 1001, "senderRole": "other"}
        mine = {"createTime": 1002, "senderRole": "self"}
        reader = mock.Mock(side_effect=[[], [other, mine], [other, mine], [other, mine]])
        result = self.run_wait(lambda t: [{"is_self": False}] if t == 1001 else [], read=reader, max_minutes=1)
        self.assertEqual(result["status"], "timed_out")

    def test_unmatched_self_echo_cannot_swallow_the_other_sides_message(self):
        other = {"createTime": 1001, "senderRole": "other"}
        result = self.run_wait(lambda t: [{"is_self": False}, {"is_self": True}] if t == 1001 else [],
                               read=mock.Mock(side_effect=[[]] + [[other]] * 9), max_minutes=1)
        self.assertEqual((result["status"], result["new"]), ("new_messages", [other]))

    def test_failed_recheck_keeps_waiting_and_is_reported(self):
        result = self.run_wait(read=mock.Mock(side_effect=[[], OSError("x"), OSError("x"), OSError("x")]), max_minutes=0.25)
        self.assertEqual((result["status"], result["recheckError"]), ("timed_out", "x"))

    def test_logout_during_wait_asks_for_owner_login_and_keeps_messages(self):
        message = {"createTime": 1001, "senderRole": "other"}
        def events(_t):
            raise helper.HelperError("helper_account_logged_out", diagnosticCode="account_logged_out", requiredAction="owner_relogin")
        result = self.run_wait(events, read=lambda: [message])
        self.assertEqual((result["status"], result["requiredAction"], result["new"]), ("login_required", "owner_relogin", [message]))

    def test_login_hold_reports_each_ending_without_connecting(self):
        for ready, stopped, expected in (([True], False, "logged_in"), ([False, False, True], False, "login_restored"),
                                         ([False] * 99, False, "timed_out"), ([False] * 99, True, "stopped")):
            with self.subTest(expected=expected):
                clock = Clock()
                answer = iter(ready)
                self.assertEqual(wait_login(ready=lambda: next(answer), max_minutes=1, stopped=lambda: stopped,
                                            now=clock.now, sleep=clock.sleep), expected)

    def test_connection_failure_is_reported(self):
        events = mock.Mock(side_effect=OSError("synthetic-disconnect"))
        self.assertEqual(self.run_wait(events)["status"], "connection_lost")


class WaitCommandTests(unittest.TestCase):
    def run_command(self, session, *extra, wait=None, login=None, read=lambda *_args: []):
        args = cli.parser().parse_args(["wait-new", "--account", "secondary", "--target", "filehelper",
                                        "--since", "1970-01-01T08:00:00", "--config", "synthetic.json", *extra])
        output = io.BytesIO()
        patches = [mock.patch.object(cli, "_check_personal_access"), mock.patch.object(cli, "_read_config", return_value={}),
                   mock.patch.object(cli, "_resolve_config_path", return_value="synthetic.json"),
                   mock.patch.object(cli, "_paced_session", return_value=session),
                   mock.patch.object(cli, "_paced_read", side_effect=read),
                   mock.patch.object(cli.sys, "stdout", mock.Mock(buffer=output))]
        if wait is not None:
            patches.append(mock.patch("wechat_wait.wait_new", return_value=wait))
        if login is not None:
            patches.append(mock.patch("wechat_wait.wait_login", return_value=login))
        for patch in patches:
            patch.start()
        try:
            code = args.handler(args)
        finally:
            for patch in patches:
                patch.stop()
        return code, json.loads(output.getvalue())

    def test_normal_endings_exit_zero_and_faults_exit_two(self):
        for status, expected in (("new_messages", 0), ("stopped", 0), ("timed_out", 0), ("read_failed", 2),
                                 ("connection_lost", 2), ("login_required", 2), ("locked", 2)):
            with self.subTest(status=status):
                code, report = self.run_command(mock.Mock(), wait={"status": status, "since": 0, "new": []})
                self.assertEqual((code, report["status"]), (expected, status))

    def test_logged_out_at_start_says_owner_must_log_in(self):
        session = mock.Mock()
        session.start.side_effect = helper.HelperError(
            "helper_start_failed", diagnosticCode="bound_account_not_logged_in", requiredAction="owner_relogin")
        code, report = self.run_command(session)
        self.assertEqual((code, report["status"], report["requiredAction"]), (2, "login_required", "owner_relogin"))

    def test_logout_still_reads_what_arrived_from_the_local_record(self):
        message = {"createTime": 5, "senderRole": "other", "content": "synthetic"}
        code, report = self.run_command(mock.Mock(), wait={"status": "login_required", "since": 0, "new": []},
                                        read=lambda *_args: [message])
        self.assertEqual((code, report["status"], len(report["new"])), (2, "login_required", 1))

    def test_wait_login_returns_when_login_is_back_without_connecting(self):
        session = mock.Mock()
        message = {"createTime": 5, "senderRole": "other", "content": "synthetic"}
        code, report = self.run_command(session, "--wait-login", login="login_restored", read=lambda *_args: [message])
        self.assertEqual((code, report["status"], len(report["new"])), (0, "login_restored", 1))
        session.start.assert_not_called()
        code, report = self.run_command(session, "--wait-login", login="timed_out")
        self.assertEqual((code, report["status"], report["requiredAction"]), (2, "login_required", "owner_relogin"))
        session.start.assert_not_called()

    def test_wait_login_while_logged_in_waits_normally(self):
        session = mock.Mock()
        code, report = self.run_command(session, "--wait-login", login="logged_in", wait={"status": "timed_out", "since": 0, "new": []})
        self.assertEqual((code, report["status"]), (0, "timed_out"))
        session.start.assert_called_once()

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
