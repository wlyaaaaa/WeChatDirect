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
        rng.uniform.side_effect = lambda low, high: low
        plan = paced.make_plan(["一二", "三"], rng=rng)
        self.assertEqual(plan[0]["thinkSeconds"], 0)
        self.assertEqual(plan[0]["typingSeconds"], 0)
        self.assertEqual(plan[1]["thinkSeconds"], paced.NEXT_THINK_SECONDS[0])
        self.assertEqual(plan[0]["waitSeconds"], 0)
        self.assertEqual(plan[1]["waitSeconds"], 1.1)

    def test_followup_nine_characters_and_emoji_waits_at_most_ten_seconds(self):
        rng = mock.Mock()
        rng.uniform.side_effect = lambda low, high: high
        plan = paced.make_plan(["先发", "一二三四五六七八九[旺柴]"], rng=rng)
        self.assertEqual(plan[1]["waitSeconds"], 8.9)
        self.assertLessEqual(plan[1]["waitSeconds"], 10)

    def test_first_bubble_sends_at_start_then_followup_waits(self):
        clock = Clock()
        sent_at = []

        def send(_text):
            sent_at.append(clock.now())
            return {"status": "success", "wechatAccepted": True}

        result = paced.run_plan(paced.make_plan(["一二", "三"], next_think=(0.5, 0.5)), started_at=clock.now(), send=send,
                                newer_messages=lambda _index: [], now=clock.now, sleep=clock.sleep)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(sent_at, [1000.0, 1001.1])

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
    def run_command(self, messages, *extra):
        args = cli.parser().parse_args([
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
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "plan_only")
        self.assertIn("now", report)
        self.assertEqual(report["lastFromOther"]["content"], "早")
        self.assertIsNone(report["lastFromSelf"])
        self.assertIn("firstBubbleAt", report)
        settings.assert_not_called()

    def test_new_message_is_reported_before_any_send(self):
        code, report, settings = self.run_command([
            {"createTime": cli._parse_time("2026-01-01T10:00:01", default=0), "senderRole": "other", "content": "新"}],
            "--send")
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "chat_moved")
        self.assertEqual(report["unsent"], ["一二三"])
        settings.assert_not_called()

    def test_hand_typed_message_also_stops_the_first_bubble(self):
        code, report, _settings = self.run_command([
            {"createTime": cli._parse_time("2026-01-01T10:00:01", default=0), "senderRole": "self", "content": "手打"}])
        self.assertEqual((code, report["status"]), (2, "chat_moved"))

    def test_oversized_bubble_is_rejected_before_anything_runs(self):
        args = cli.parser().parse_args([
            "helper", "send-paced", "--account", "secondary", "--target", "wxid_synthetic",
            "--since", "2026-01-01T10:00:00", "--bubble", "字" * 400])
        with self.assertRaisesRegex(cli.ProductError, "helper_text_utf8_limit"):
            args.handler(args)

    def test_initial_lock_never_connects_or_sends(self):
        with mock.patch.object(cli, "_check_personal_access", side_effect=cli.ProductError(
                "locked", status="locked", required_action="verify")):
            code, report, settings = self.run_command([], "--send")
        self.assertEqual((code, report["status"]), (2, "locked"))
        settings.assert_not_called()

    def test_lock_while_waiting_never_sends_or_reads_back(self):
        locked = cli.ProductError("locked", status="locked", required_action="verify")
        session = mock.Mock()
        session.execute.return_value = {"status": "success", "wechatAccepted": True}
        with (mock.patch.object(cli, "_paced_read", side_effect=[[], [], [], locked]) as read,
              mock.patch.object(cli, "_paced_session", return_value=session),
              mock.patch.object(cli.time, "sleep"),
              mock.patch.object(paced, "make_plan", return_value=steps(0, 0))):
            code, report, _settings = self.run_command([], "--send", "--bubble", "四五六")
        self.assertEqual((code, report["status"]), (2, "locked"))
        self.assertEqual((read.call_count, len(report["sent"]), report["unsent"]), (4, 1, ["bubble1"]))
        session.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
