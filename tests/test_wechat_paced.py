"""Synthetic pacing contracts; never connect an actual helper runtime or read real chats."""

import isolation  # noqa: F401

import io
import json
import unittest
from unittest import mock

import wechat_cli as cli
import wechat_helper as helper
import wechat_paced as paced


class Clock:
    def __init__(self, start=1000.0):
        self.value = start
        self.naps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.naps.append(seconds)
        self.value += seconds


def steps(*waits):
    return [{"text": f"bubble{index}", "waitSeconds": wait} for index, wait in enumerate(waits)]


class PacingTests(unittest.TestCase):
    def test_typing_time_follows_characters_and_emoji(self):
        self.assertAlmostEqual(paced.typing_seconds("一二三四五六七八九十"), 6.0)
        self.assertAlmostEqual(paced.typing_seconds("一二三四五[旺柴]"), 3.0 + paced.EMOJI_SECONDS)
        self.assertAlmostEqual(paced.typing_seconds("一二三四五六七八九十", 200), 3.0)

    def test_first_bubble_has_no_wait_and_followup_keeps_typing_speed(self):
        rng = mock.Mock()
        rng.uniform.side_effect = lambda low, high: high
        plan = paced.make_plan(["一二", "一二三四五六七八九[旺柴]"], typing_spread=(1, 1), rng=rng)
        self.assertEqual(tuple(plan[0][key] for key in ("thinkSeconds", "typingSeconds", "waitSeconds")), (0, 0, 0))
        self.assertEqual((plan[1]["thinkSeconds"], plan[1]["waitSeconds"]), (paced.NEXT_THINK_SECONDS[1], 8.9))

    def test_equal_length_bubbles_get_their_own_typing_speed(self):
        rng = mock.Mock()
        rng.uniform.side_effect = lambda low, high: low if rng.uniform.call_count % 4 == 0 else high
        plan = paced.make_plan(["一", "一二三四五六七八九十", "一二三四五六七八九十"], next_think=(1, 1), rng=rng)
        self.assertEqual(plan[0]["typingSeconds"], 0)
        self.assertEqual([plan[1]["typingSeconds"], plan[2]["typingSeconds"]], [7.8, 4.2])

    def test_first_bubble_sends_at_start_then_followup_waits(self):
        clock = Clock()
        sent_at = []

        def send(_text):
            sent_at.append(clock.now())
            return {"status": "success", "wechatAccepted": True}

        result = paced.run_plan(paced.make_plan(["一二", "三"], next_think=(0.5, 0.5), typing_spread=(1, 1)), started_at=clock.now(), send=send,
                                newer_messages=lambda _index: [], now=clock.now, sleep=clock.sleep)
        self.assertEqual((result["status"], sent_at), ("sent", [1000.0, 1001.1]))

    def test_new_message_stops_and_returns_unsent(self):
        clock = Clock()
        sends = []

        def send(text):
            sends.append(text)
            return {"status": "success", "wechatAccepted": True}

        result = paced.run_plan(steps(1.0, 1.0, 1.0), started_at=clock.now(), send=send,
                                newer_messages=lambda index: ["new"] if index == 1 else [],
                                now=clock.now, sleep=clock.sleep)
        self.assertEqual(result["status"], "chat_moved")
        self.assertEqual(sends, ["bubble0"])
        self.assertEqual(result["unsent"], ["bubble1", "bubble2"])

    def test_uncertain_send_stops_without_resend(self):
        clock = Clock()
        sends = []

        def send(text):
            sends.append(text)
            return {"status": "unknown", "wechatAccepted": None}

        result = paced.run_plan(steps(1.0, 1.0), started_at=clock.now(), send=send,
                                newer_messages=lambda _index: [], now=clock.now, sleep=clock.sleep)
        self.assertEqual(result["status"], "send_uncertain")
        self.assertEqual(sends, ["bubble0"])
        self.assertEqual(result["uncertain"], "bubble0")
        self.assertEqual(result["unsent"], ["bubble1"])

    def test_logout_before_the_native_call_keeps_the_bubble_unsent(self):
        clock = Clock()
        sends = []

        def send(text):
            sends.append(text)
            if len(sends) == 2:
                raise helper.HelperError("helper_account_logged_out", requiredAction="owner_relogin")
            return {"status": "success", "wechatAccepted": True}

        result = paced.run_plan(steps(0.0, 0.0, 0.0), started_at=clock.now(), send=send,
                                newer_messages=lambda _index: [], now=clock.now, sleep=clock.sleep)
        self.assertEqual((result["status"], result["requiredAction"]), ("login_required", "owner_relogin"))
        self.assertEqual([item["text"] for item in result["sent"]], ["bubble0"])
        self.assertEqual(result["unsent"], ["bubble1", "bubble2"])

    def test_failed_check_never_sends_blindly(self):
        clock = Clock()
        send = mock.Mock()

        def broken(_index):
            raise cli.ProductError("synthetic_read_failed")

        result = paced.run_plan(steps(0.0), started_at=clock.now(), send=send, newer_messages=broken,
                                now=clock.now, sleep=clock.sleep)
        self.assertEqual(result["status"], "check_failed")
        send.assert_not_called()


class CommandTests(unittest.TestCase):
    def run_command(self, messages, *extra, command=None):
        args = cli.parser().parse_args(command or [
            "helper", "send-paced", "--account", "secondary", "--target", "wxid_synthetic",
            "--since", "2026-01-01T10:00:00", "--bubble", "一二三", "--config", "synthetic.json", *extra])
        output = io.BytesIO()
        fake_stdout = mock.Mock(buffer=output)
        with (
            mock.patch.object(cli, "_read_config", return_value={}),
            mock.patch.object(cli, "_resolve_config_path", return_value="synthetic.json"),
            mock.patch.object(cli, "_context_result", return_value={"messages": messages}),
            mock.patch.object(helper, "load_settings") as settings,
            mock.patch.object(cli.sys, "stdout", fake_stdout),
        ):
            code = args.handler(args)
        return code, json.loads(output.getvalue()), settings

    def test_default_only_prints_the_plan_with_the_current_time(self):
        code, report, settings = self.run_command([
            {"createTime": cli._parse_time("2026-01-01T09:00:00", default=0), "senderRole": "other", "content": "早"}])
        self.assertEqual((code, report["status"]), (0, "plan_only"))
        self.assertIn("now", report)
        self.assertEqual(report["lastFromOther"]["content"], "早")
        self.assertIsNone(report["lastFromSelf"])
        self.assertIn("firstBubbleAt", report)
        settings.assert_not_called()

    def test_new_message_is_reported_before_any_send(self):
        for role in ("other", "self"):
            code, report, settings = self.run_command([{"createTime": cli._parse_time("2026-01-01T10:00:01", default=0), "senderRole": role, "content": "新"}], "--send")
            self.assertEqual((code, report["status"]), (2, "chat_moved"))
            self.assertEqual(report["unsent"], ["一二三"])
            settings.assert_not_called()

    def test_oversized_bubble_is_rejected_before_anything_runs(self):
        args = cli.parser().parse_args([
            "helper", "send-paced", "--account", "secondary", "--target", "wxid_synthetic",
            "--since", "2026-01-01T10:00:00", "--bubble", "字" * 400])
        with self.assertRaisesRegex(cli.ProductError, "helper_text_utf8_limit"):
            args.handler(args)

    def test_self_messages_cancel_only_one_matching_sent_bubble_each(self):
        for inserted, bubbles in ((None, ["一二三", "四五六"]),
                                  ("synthetic manual message", ["一二三", "四五六"]),
                                  ("四五六", ["一二三", "四五六"]),
                                  ("一二三", ["一二三", "四五六"]),
                                  (None, ["一二三", "一二三", "四五六"])):
            with self.subTest(inserted=inserted, bubbles=bubbles):
                session = mock.Mock()
                session.execute.return_value = {"status": "success", "wechatAccepted": True}
                session.close.return_value = {}
                stamp = cli._parse_time("2026-01-01T10:00:01", default=0)
                def read(*_args):
                    texts = [call.args[0]["text"] for call in session.execute.call_args_list]
                    if texts and inserted:
                        texts.append(inserted)
                    return [{"createTime": stamp, "senderRole": "self", "content": text} for text in texts]
                with (mock.patch.object(cli, "_check_personal_access"),
                      mock.patch.object(cli, "_paced_session", return_value=session),
                      mock.patch.object(cli, "_paced_read", side_effect=read),
                      mock.patch.object(paced, "make_plan", return_value=[
                          {"text": text, "waitSeconds": 0} for text in bubbles]),
                      mock.patch.object(cli.time, "sleep")):
                    code, report, _settings = self.run_command(
                        [], "--send", *[arg for text in bubbles[1:] for arg in ("--bubble", text)])
                self.assertEqual((code, report["status"]), (2, "chat_moved") if inserted else (0, "sent"))
                self.assertEqual([call.args[0]["text"] for call in session.execute.call_args_list],
                                 bubbles[:1] if inserted else bubbles)
                self.assertEqual(report.get("unsent", []), bubbles[1:] if inserted else [])

    def test_initial_lock_never_connects_or_sends(self):
        for command in (None, ["wait-new", "--account", "secondary", "--target", "filehelper", "--since", "1970-01-01T08:00:00"]):
            with (mock.patch.object(cli, "_check_personal_access", side_effect=cli.ProductError(
                    "locked", status="locked", required_action="verify")), mock.patch.object(cli, "_paced_read") as read):
                code, report, settings = self.run_command([], "--send", command=command)
            self.assertEqual((code, report["status"], settings.call_count, read.call_count), (2, "locked", 0, 0))

    def test_send_checks_access_only_once(self):
        session = mock.Mock()
        session.execute.return_value = {"status": "success", "wechatAccepted": True}
        with (mock.patch.object(cli, "_check_personal_access") as access,
              mock.patch.object(cli, "_paced_session", return_value=session),
              mock.patch.object(cli.time, "sleep")):
            code, report, _settings = self.run_command([], "--send")
        self.assertEqual((code, report["status"], access.call_count), (0, "sent", 1))
        session.execute.assert_called_once()

    def test_wait_integer_event_id_and_access_boundaries(self):
        message = {"serverId": "987", "createTime": 1000, "content": "stale-body"}
        for status in ("new_messages", "stopped", "connection_lost"):
            session = mock.Mock()
            session.execute.return_value = {"events": [{"native_message_id": 987, "received_at": 1002}]}
            def wait(**kwargs):
                kwargs["read"]()
                kwargs["event"](0)
                return {"status": status, "new": [message]}
            ending = None if status == "new_messages" else cli.ProductError("locked", status="locked")
            with (mock.patch.object(cli, "_check_personal_access", side_effect=[None, ending]) as access,
                  mock.patch.object(cli, "_paced_session", return_value=session),
                  mock.patch.object(cli, "_paced_read", side_effect=lambda *args: [message] if session.close.called else []) as read,
                  mock.patch("wechat_wait.wait_new", side_effect=wait)):
                code, report, _settings = self.run_command([], command=["wait-new", "--account", "secondary", "--target", "filehelper", "--since", "1970-01-01T08:00:00"])
            self.assertEqual(access.call_count, 2)
            if status == "new_messages":
                self.assertEqual((code, report["firstEventDelaySeconds"], read.call_count), (0, 2, 2))
            else:
                self.assertEqual((code, report["status"], report["new"], report["nextSince"], read.call_count), (2, "locked", [], report["since"], 1))
            session.close.assert_called_once()

    def test_logged_out_at_send_start_returns_every_bubble_unsent(self):
        session = mock.Mock()
        session.start.side_effect = helper.HelperError(
            "helper_start_failed", diagnosticCode="bound_account_not_logged_in", requiredAction="owner_relogin")
        session.close.return_value = {"status": "not_needed"}
        with (mock.patch.object(cli, "_check_personal_access"), mock.patch.object(cli, "_paced_session", return_value=session),
              mock.patch.object(cli.time, "sleep")):
            code, report, _settings = self.run_command([], "--send")
        self.assertEqual((code, report["status"], report["unsent"], report["requiredAction"]),
                         (2, "login_required", ["一二三"], "owner_relogin"))
        session.execute.assert_not_called()

    def wait_after(self, session, read, *extra):
        with (mock.patch.object(cli, "_check_personal_access"),
              mock.patch.object(cli, "_paced_session", return_value=session) as make,
              mock.patch.object(cli, "_paced_read", side_effect=read),
              mock.patch.object(cli.time, "sleep")):
            code, report, _settings = self.run_command([], "--send", "--wait-after", "--settle-seconds", "0.01", *extra)
        return code, report, make

    def test_wait_after_catches_instant_reply_on_the_same_connection(self):
        stamp = cli._parse_time("2026-01-01T10:00:02", default=0)
        session = mock.Mock()
        pending = [[{"is_self": True, "text": "一二三", "received_at": 1}, {"is_self": False, "text": "synthetic reply", "received_at": 1}]]
        def execute(request):
            if request["action"] == "send-text":
                return {"status": "success", "wechatAccepted": True}
            return {"events": pending.pop() if pending else [], "dropped": 0}
        session.execute.side_effect = execute
        def read(*_args):
            if not any(call.args[0]["action"] == "send-text" for call in session.execute.call_args_list):
                return []
            return [{"createTime": stamp, "senderRole": "self", "content": "一二三"},
                    {"createTime": stamp + 1, "senderRole": "other", "content": "synthetic reply"}]
        code, report, make = self.wait_after(session, read)
        self.assertEqual((code, report["status"], report["wait"]["status"]), (0, "sent", "new_messages"))
        self.assertEqual([item["content"] for item in report["wait"]["new"]], ["synthetic reply"])
        self.assertEqual(report["wait"]["nextSince"], cli._paced_moment(stamp + 1))
        session.start.assert_called_once()
        session.close.assert_called_once()
        self.assertEqual(make.call_args.kwargs["event_targets"], ("wxid_synthetic",))

    def test_wait_after_does_not_wake_on_own_bubble_alone(self):
        stamp = cli._parse_time("2026-01-01T10:00:02", default=0)
        session = mock.Mock()
        session.execute.side_effect = lambda request: (
            {"status": "success", "wechatAccepted": True} if request["action"] == "send-text"
            else {"events": [{"is_self": True, "text": "一二三", "received_at": 1}], "dropped": 0})
        def read(*_args):
            return ([{"createTime": stamp, "senderRole": "self", "content": "一二三"}]
                    if session.execute.call_count else [])
        code, report, _make = self.wait_after(session, read, "--max-minutes", "0.0005")
        self.assertEqual((code, report["wait"]["status"], report["wait"]["new"]), (0, "timed_out", []))
        self.assertEqual(report["wait"]["nextSince"], cli._paced_moment(stamp))

    def test_wait_after_refuses_a_leftover_stop_file_before_sending(self):
        session = mock.Mock()
        path = isolation.ROOT / "synthetic-leftover.stop"
        path.write_text("", encoding="utf-8")
        self.addCleanup(path.unlink)
        code, report, make = self.wait_after(session, lambda *_args: [], "--stop-file", str(path))
        self.assertEqual((code, report["status"], report["unsent"]), (2, "stop_file_exists", ["一二三"]))
        make.assert_not_called()

    def test_failed_send_does_not_start_waiting(self):
        session = mock.Mock()
        session.execute.return_value = {"status": "unknown", "wechatAccepted": None}
        code, report, _make = self.wait_after(session, lambda *_args: [])
        self.assertEqual((code, report["status"]), (2, "send_uncertain"))
        self.assertNotIn("wait", report)
        session.execute.assert_called_once()

    def test_read_failure_after_waiting_still_reports_the_sent_bubble(self):
        session = mock.Mock()
        session.execute.side_effect = lambda request: (
            {"status": "success", "wechatAccepted": True} if request["action"] == "send-text" else {"events": []})
        with mock.patch("wechat_cli._finish_wait", side_effect=cli.ProductError("synthetic_read_failed")):
            code, report, _make = self.wait_after(session, lambda *_args: [], "--max-minutes", "0.0005")
        self.assertEqual((code, report["status"], report["wait"]["status"]), (2, "sent", "read_failed"))
        self.assertEqual([item["text"] for item in report["sent"]], ["一二三"])

    def test_bad_wait_settings_are_refused_before_any_send(self):
        session = mock.Mock()
        for extra in (("--max-minutes", "0"), ("--max-minutes", "nan"), ("--settle-seconds", "-1")):
            with self.subTest(extra=extra), self.assertRaisesRegex(cli.ProductError, "wait_intervals"):
                self.wait_after(session, lambda *_args: [], *extra)
        session.execute.assert_not_called()

    def test_readback_failure_of_any_kind_keeps_the_send_result(self):
        session = mock.Mock()
        session.execute.return_value = {"status": "success", "wechatAccepted": True}
        session.close.return_value = {}
        calls = []
        def read(*_args):
            calls.append(1)
            if session.execute.called:
                raise RuntimeError("synthetic")
            return []
        with (mock.patch.object(cli, "_check_personal_access"), mock.patch.object(cli, "_paced_session", return_value=session),
              mock.patch.object(cli, "_paced_read", side_effect=read), mock.patch.object(cli.time, "sleep")):
            code, report, _settings = self.run_command([], "--send")
        self.assertEqual((code, report["status"], report["readbackComplete"]), (0, "sent", False))
        self.assertEqual([item["text"] for item in report["sent"]], ["一二三"])

    def test_next_since_does_not_skip_a_reply_missing_from_the_report(self):
        stamp = cli._parse_time("2026-01-01T10:00:02", default=0)
        session = mock.Mock()
        session.execute.side_effect = lambda request: (
            {"status": "success", "wechatAccepted": True} if request["action"] == "send-text" else {"events": []})
        mine = {"createTime": stamp, "senderRole": "self", "content": "一二三"}
        reply_a = {"createTime": stamp + 1, "senderRole": "other", "content": "synthetic A"}
        reply_b = {"createTime": stamp + 5, "senderRole": "other", "content": "synthetic B"}
        def read(*_args):
            if not session.close.called:
                return [mine, reply_a] if session.execute.called else []
            read.after_close += 1
            return [mine, reply_a] if read.after_close == 1 else [mine, reply_a, reply_b]
        read.after_close = 0
        code, report, _make = self.wait_after(session, read)
        self.assertEqual([item["content"] for item in report["wait"]["new"]], ["synthetic A"])
        self.assertEqual(report["wait"]["nextSince"], cli._paced_moment(stamp + 1))

    def test_stop_file_cancels_a_scheduled_send(self):
        session = mock.Mock()
        path = isolation.ROOT / "synthetic-later.stop"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        with (mock.patch.object(cli, "_check_personal_access"), mock.patch.object(cli, "_paced_session", return_value=session),
              mock.patch.object(cli.time, "sleep", side_effect=lambda _s: path.write_text("", encoding="utf-8"))):
            code, report, _settings = self.run_command(
                [], "--send", "--stop-file", str(path), "--not-before", "2099-01-01T00:00:00")
        self.assertEqual((code, report["status"], report["unsent"]), (0, "stopped", ["一二三"]))
        session.start.assert_not_called()
        session.execute.assert_not_called()

    def test_stop_file_between_bubbles_keeps_the_rest_unsent(self):
        clock = Clock()
        send = mock.Mock(return_value={"status": "success", "wechatAccepted": True})
        result = paced.run_plan(steps(0.0, 0.0), started_at=clock.now(), send=send, newer_messages=lambda _index: [],
                                now=clock.now, sleep=clock.sleep, stopped=lambda: send.called)
        self.assertEqual((result["status"], result["unsent"], send.call_count), ("stopped", ["bubble1"], 1))

    def test_bubble_ends_stripped_by_the_reader_still_count_as_ours(self):
        stamp = int(cli.time.time())
        session = mock.Mock()
        session.execute.side_effect = lambda request: (
            {"status": "success", "wechatAccepted": True} if request["action"] == "send-text"
            else {"events": [{"is_self": True, "text": "一二三 ", "received_at": 1}]})
        def read(*_args):
            return [{"createTime": stamp, "senderRole": "self", "content": "一二三"}] if session.execute.called else []
        with mock.patch.object(paced, "make_plan", return_value=[{"text": "一二三 ", "waitSeconds": 0}]):
            code, report, _make = self.wait_after(session, read, "--max-minutes", "0.0005")
        self.assertEqual((code, report["readbackComplete"], report["wait"]["new"]), (0, True, []))

    def test_lock_after_waiting_hides_the_messages(self):
        session = mock.Mock()
        session.execute.side_effect = lambda request: (
            {"status": "success", "wechatAccepted": True} if request["action"] == "send-text" else {"events": []})
        reply = [{"createTime": cli._parse_time("2026-01-01T10:00:02", default=0), "senderRole": "other", "content": "synthetic reply"},
                 {"createTime": 5, "senderRole": "other", "content": "old body"}]
        with (mock.patch.object(cli, "_check_personal_access", side_effect=[None, cli.ProductError("locked", status="locked")]),
              mock.patch.object(cli, "_paced_session", return_value=session),
              mock.patch.object(cli, "_paced_read", side_effect=lambda *_args: reply if session.execute.call_count else []),
              mock.patch.object(cli.time, "sleep")):
            code, report, _settings = self.run_command([], "--send", "--wait-after", "--settle-seconds", "0.01")
        self.assertEqual((code, report["wait"]["status"], report["wait"]["new"], report["newAfterSend"]), (2, "locked", [], []))
        self.assertNotIn("synthetic reply", json.dumps(report))
        self.assertNotIn("old body", json.dumps(report))

    def test_timing_first_page_and_since_full_pagination(self):
        args = mock.Mock(account="secondary", config=None, chat_type=None)
        for window, count in ((("--lookback-days", "30"), 1), (("--since", "1970-01-01T08:00:00"), 2)):
            with mock.patch.object(cli, "_context_result", side_effect=[
                    {"messages": [2], "continuation": {"cursor": "synthetic"}}, {"messages": [1]}]) as read:
                messages = cli._paced_read(args, "synthetic", *window)
            self.assertEqual((read.call_count, messages), (count, [2] if count == 1 else [1, 2]))


if __name__ == "__main__":
    unittest.main()
