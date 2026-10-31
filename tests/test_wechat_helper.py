"""Synthetic native adapter contracts; never import or connect an actual helper runtime."""

import isolation  # noqa: F401

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import wechat_cli as cli
import wechat_helper as helper


WXID = "wxid_synthetic"
COMMITMENT = "sha256:" + hashlib.sha256(WXID.encode()).hexdigest()


class Service:
    def __init__(self, **options):
        self.options = options
        self.pid = options["pid"]
        self.last_error = self.last_result = None
        self.calls = []
        self.stop_calls = 0
        self.start_calls = 0
        self.handler = None
        self.cleanup = {"code": 0, "dll_unloaded": True}
        self.raises = None
        self.native = {"code": 0, "success": True, "cleanup_code": 0,
                       "report": {"completion_success": True, "text": "not-for-stdout",
                                  "ticket": "secret-ticket", "operation": "text_send"}}

    def register_msg_handler(self, handler):
        self.handler = handler

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1
        if isinstance(self.cleanup, Exception):
            raise self.cleanup
        return self.cleanup

    def send_text(self, target, text):
        self.calls.append(("text", target, text))
        if self.raises:
            raise self.raises
        self.last_result = self.native
        return self.native["success"]

    def send_file(self, target, path):
        return self.send_text(target, path)

    send_image = send_emoticon = send_voice = send_file

    def send_at_text(self, target, text, members):
        return self.send_text(target, text)

    def friend_requests(self):
        return [{"request_id": "fr_" + "a" * 32, "sender_id": "wxid_requester",
                 "nickname": "合成", "status": "pending", "v3": "never-output-v3", "v4": "never-output-v4"}]

    def accept_friend(self, request_id):
        return self.send_text("request", request_id)

    def group_members(self, target):
        return None


class Module:
    def __init__(self):
        self.account = WXID
        self.service = None
        self.inspections = 0

    def find_account_process(self, expected):
        return 123

    def inspect_account(self, pid):
        self.inspections += 1
        return self.account

    def NativeTextService(self, **options):
        self.service = Service(**options)
        return self.service


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.module = Module()
        temporary = tempfile.TemporaryDirectory(dir=isolation.ROOT)
        self.addCleanup(temporary.cleanup)
        test_root = Path(temporary.name)
        self.settings = {"runtime_path": Path(isolation.ROOT) / "absent-runtime",
                         "state_root": test_root / "helper-state"}
        self.settings["state_root"].mkdir()
        self.session = helper.HelperSession(self.settings, account="primary", expected_wxid=WXID,
                                            expected_sha256=COMMITMENT, module=self.module)

    def test_wrong_native_identity_blocks_before_start(self):
        self.module.account = "wxid_other"
        receipt = helper.run_once(self.session, {"action": "send-text", "target": "filehelper", "text": "合成"})
        self.assertEqual(receipt["error"], "helper_account_identity_mismatch")
        self.assertEqual(self.module.service.start_calls, 0)
        self.assertEqual(self.module.service.calls, [])

    def test_start_failure_retains_fixed_diagnostics_not_exception_text(self):
        with mock.patch.object(Service, "start", side_effect=RuntimeError("接收 Hook 启动失败：17")):
            receipt = helper.run_once(self.session, {"action": "send-text", "target": "filehelper", "text": "合成"})
        self.assertEqual(receipt["stage"], "start_native_service")
        self.assertEqual(receipt["exceptionType"], "RuntimeError")
        self.assertEqual(receipt["diagnosticCode"], "native_listen_rejected")
        self.assertEqual(receipt["nativeCode"], 17)
        with mock.patch.object(Service, "start", side_effect=RuntimeError("private-ticket-body")):
            receipt = helper.run_once(self.session, {"action": "send-text", "target": "filehelper", "text": "合成"})
        self.assertNotIn("private-ticket-body", json.dumps(receipt))

    def test_account_switch_before_send_blocks_and_closes(self):
        self.session.start()
        self.module.account = "wxid_other"
        with self.assertRaisesRegex(helper.HelperError, "identity_mismatch"):
            self.session.execute({"action": "send-text", "target": "filehelper", "text": "合成"})
        self.assertEqual(self.module.service.calls, [])
        self.assertTrue(self.session.close()["dllUnloaded"])

    def test_completed_send_is_not_readback_and_report_is_allowlisted(self):
        receipt = helper.run_once(self.session, {"action": "send-text", "target": "filehelper", "text": "合成"})
        self.assertEqual(receipt["status"], "success")
        self.assertTrue(receipt["wechatAccepted"])
        self.assertEqual(receipt["actualReadback"], "not_checked")
        self.assertNotIn("secret-ticket", json.dumps(receipt))
        self.assertNotIn("not-for-stdout", json.dumps(receipt))
        self.assertEqual(self.module.service.stop_calls, 1)
        self.assertIsNone(self.module.service.options["wechat_data_directory"])
        self.assertFalse(self.module.service.options["preserve_reports"])
        self.assertEqual(self.module.service.options["image_allowlist"], ["filehelper"])

    def test_timeout_unknown_not_retried_and_session_poisoned(self):
        self.session.start()
        self.module.service.raises = TimeoutError("private native detail")
        receipt = self.session.execute({"action": "send-text", "target": "filehelper", "text": "合成"})
        self.assertEqual(receipt["status"], "unknown")
        self.assertFalse(receipt["retryable"])
        with self.assertRaisesRegex(helper.HelperError, "session_unavailable"):
            self.session.execute({"action": "send-text", "target": "filehelper", "text": "合成"})
        self.assertEqual(len(self.module.service.calls), 1)
        self.assertNotIn("private native detail", json.dumps(receipt))
        self.session.close()

    def test_lost_native_receipt_is_unknown(self):
        self.session.start()
        self.module.service.native = {"success": False, "code": 0, "cleanup_code": 0}
        receipt = self.session.execute({"action": "send-text", "target": "filehelper", "text": "合成"})
        self.assertEqual(receipt["status"], "unknown")
        self.assertIsNone(receipt["wechatAccepted"])
        self.session.close()

    def test_cleanup_failure_preserves_accepted_but_is_partial(self):
        original = self.module.NativeTextService

        def service(**options):
            value = original(**options)
            value.cleanup = RuntimeError("sensitive detail")
            return value

        self.module.NativeTextService = service
        receipt = helper.run_once(self.session, {"action": "send-text", "target": "filehelper", "text": "合成"})
        self.assertEqual(receipt["status"], "partial")
        self.assertTrue(receipt["wechatAccepted"])
        self.assertEqual(receipt["cleanup"]["status"], "incomplete")
        self.assertNotIn("sensitive detail", json.dumps(receipt))

    def test_utf8_bytes_and_nul_are_validated_without_truncating(self):
        self.session.start()
        for text in ("中" * 342, "x\0y", ""):
            with self.subTest(text=text[:8]), self.assertRaises(helper.HelperError):
                self.session.execute({"action": "send-text", "target": "filehelper", "text": text})
        good = "中" * 341
        receipt = self.session.execute({"action": "send-text", "target": "filehelper", "text": good})
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(self.module.service.calls, [("text", "filehelper", good)])
        self.session.close()

    def test_media_delegates_to_service_owned_prepare_lifecycle(self):
        with tempfile.TemporaryDirectory(dir=isolation.ROOT) as directory:
            path = Path(directory) / "合成.bin"
            path.write_bytes(b"synthetic")
            self.session.start()
            for action in ("send-image", "send-file", "send-emoticon", "send-voice"):
                receipt = self.session.execute({"action": action, "target": "filehelper", "path": str(path)})
                self.assertTrue(receipt["wechatAccepted"])
            self.assertEqual(path.read_bytes(), b"synthetic")
            self.assertEqual(len(self.module.service.calls), 4)
            self.session.close()

    def test_wav_missing_encoder_is_explicit_before_invocation(self):
        with tempfile.TemporaryDirectory(dir=isolation.ROOT) as directory:
            path = Path(directory) / "voice.wav"
            path.write_bytes(b"RIFF0000WAVEdata")
            self.session.start()
            with mock.patch.object(helper.importlib.util, "find_spec", return_value=None):
                with self.assertRaisesRegex(helper.HelperError, "wav_encoder_unavailable"):
                    self.session.execute({"action": "send-voice", "target": "filehelper", "path": str(path)})
            self.assertEqual(self.module.service.calls, [])
            self.session.close()

    def test_receive_filters_scope_and_omits_xml_and_credentials(self):
        self.session.start()

        def emit(_delay):
            self.module.service.handler(123, 11046, {"chat_id": "other", "wx_type": 1, "msg": "outside"}, None)
            self.module.service.handler(123, 11046, {"chat_id": "filehelper", "wx_type": 1,
                                                    "msg": "合成\n两行", "raw_content": "v3 ticket"}, None)

        with mock.patch.object(helper.time, "sleep", side_effect=emit):
            receipt = self.session.execute({"action": "receive", "seconds": 1, "limit": 1})
        self.assertEqual(len(receipt["events"]), 1)
        self.assertEqual(receipt["events"][0]["text"], "合成\n两行")
        self.assertNotIn("v3 ticket", json.dumps(receipt))
        self.assertNotIn("outside", json.dumps(receipt))
        self.session.close()

    def test_friend_public_ids_in_same_session_no_tickets(self):
        self.session.start()
        receipt = self.session.execute({"action": "friend-requests"})
        self.assertNotIn("never-output", json.dumps(receipt))
        request_id = receipt["requests"][0]["request_id"]
        self.assertTrue(self.session.execute({"action": "accept-friend", "request_id": request_id})["wechatAccepted"])
        self.session.close()

    def test_previously_accepted_friend_is_noop_without_poisoning_session(self):
        self.session.start()
        request_id = "fr_" + "b" * 32
        accepted = [{"request_id": request_id, "status": "accepted"}]
        with mock.patch.object(self.module.service, "friend_requests", return_value=accepted):
            with mock.patch.object(self.module.service, "accept_friend") as native_accept:
                receipt = self.session.execute({"action": "accept-friend", "request_id": request_id})
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["outcome"], "previously_accepted")
        self.assertFalse(receipt["nativeInvoked"])
        self.assertIsNone(receipt["wechatAccepted"])
        self.assertNotIn("native", receipt)
        native_accept.assert_not_called()
        self.assertFalse(self.session.poisoned)
        self.assertEqual(self.session.execute({"action": "status"})["status"], "success")
        self.session.close()

    def test_live_friend_actions_reject_one_shot_before_native_or_config(self):
        self.session.module = None
        with mock.patch.object(helper, "native_module") as load_native:
            for action in ("friend-requests", "accept-friend"):
                request = {"action": action, "request_id": "fr_" + "c" * 32}
                receipt = helper.run_once(self.session, request)
                self.assertEqual(receipt["error"], "helper_live_session_required")
                self.assertIsNone(self.session.service)
                arguments = ["helper", action, "--account", "primary"]
                if action == "accept-friend":
                    arguments.extend(["--request-id", request["request_id"]])
                args = cli.parser().parse_args(arguments)
                with mock.patch.object(cli, "_helper_config_path", side_effect=AssertionError("must not read config")):
                    with self.assertRaisesRegex(cli.ProductError, "helper_live_session_required"):
                        cli.command_helper(args)
            load_native.assert_not_called()

    def test_callback_before_receive_is_retained_and_consumed_once(self):
        self.session.start()
        self.module.service.handler(123, 11046, {"chat_id": "filehelper", "wx_type": 1, "msg": "先到的合成消息"}, None)
        first = self.session.execute({"action": "receive", "seconds": 0, "limit": 10})
        second = self.session.execute({"action": "receive", "seconds": 0, "limit": 10})
        self.assertEqual(first["events"][0]["text"], "先到的合成消息")
        self.assertEqual(second["events"], [])
        self.session.close()

    def test_receive_limit_leaves_remaining_events_for_next_call(self):
        self.session.start()
        for sequence in range(3):
            self.module.service.handler(123, 11046, {"chat_id": "filehelper", "wx_type": 1,
                                                    "msg": str(sequence), "sequence": sequence}, None)
        first = self.session.execute({"action": "receive", "seconds": 0, "limit": 2})
        second = self.session.execute({"action": "receive", "seconds": 0, "limit": 2})
        self.assertEqual([event["sequence"] for event in first["events"]], [0, 1])
        self.assertEqual(first["bufferedRemaining"], 1)
        self.assertEqual([event["sequence"] for event in second["events"]], [2])
        self.session.close()

    def test_live_buffer_overflow_is_bounded_and_reported_partial(self):
        self.session.start()
        for sequence in range(201):
            self.module.service.handler(123, 11046, {"chat_id": "filehelper", "wx_type": 1,
                                                    "msg": "合成", "sequence": sequence}, None)
        first = self.session.execute({"action": "receive", "seconds": 0, "limit": 100})
        second = self.session.execute({"action": "receive", "seconds": 0, "limit": 100})
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["dropped"], 1)
        self.assertEqual(first["events"][0]["sequence"], 1)
        self.assertEqual(first["bufferedRemaining"], 100)
        self.assertEqual(second["status"], "success")
        self.assertEqual(second["dropped"], 0)
        self.assertEqual(second["events"][-1]["sequence"], 200)
        self.session.close()

    def test_group_cache_missing_is_not_empty_group(self):
        self.session.start()
        receipt = self.session.execute({"action": "group-members", "target": "123@chatroom"})
        self.assertEqual(receipt["status"], "partial")
        self.assertIsNone(receipt["members"])
        self.session.close()

    def test_session_invalid_json_does_not_escape_and_eof_cleans(self):
        output = io.StringIO()
        code = helper.run_session(self.session, io.StringIO('bad-json\n{"action":"status"}\n'), output)
        receipts = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(len(receipts), 3)
        self.assertEqual(receipts[0]["status"], "failed")
        self.assertEqual(receipts[1]["action"], "status")
        self.assertEqual(receipts[2]["action"], "close")
        self.assertEqual(self.module.service.stop_calls, 1)

    def test_session_start_failure_clean_stop_still_returns_nonzero(self):
        output = io.StringIO()
        with mock.patch.object(Service, "start", side_effect=RuntimeError("synthetic-start-failure")):
            code = helper.run_session(self.session, io.StringIO('{"action":"status"}\n'), output)
        receipts = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(code, 2)
        self.assertEqual(receipts[0]["error"], "helper_start_failed")
        self.assertEqual(receipts[-1]["action"], "close")
        self.assertEqual(receipts[-1]["cleanup"]["status"], "success")
        self.assertEqual(self.module.service.stop_calls, 1)

    def test_explicit_close_is_one_receipt_and_no_second_stop(self):
        output = io.StringIO()
        helper.run_session(self.session, io.StringIO('{"action":"close"}\n'), output)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(self.module.service.stop_calls, 1)

    def test_arbitrary_methods_and_unsupported_functions_are_blocked(self):
        self.session.start()
        for action in ("__getattribute__", "publish-moments", "contacts", "add-friend"):
            with self.subTest(action=action), self.assertRaises(helper.HelperError):
                self.session.execute({"action": action})
        self.assertEqual(self.module.service.calls, [])
        self.session.close()

    def test_static_status_never_imports_native_or_creates_state(self):
        self.settings["state_root"] = self.settings["state_root"] / "not-created"
        with mock.patch.object(helper.importlib, "import_module", side_effect=AssertionError("must not import")):
            receipt = helper.environment_status(self.settings)
        self.assertFalse(receipt["nativeCodeLoaded"])
        self.assertFalse(receipt["accountRead"])
        self.assertFalse(self.settings["state_root"].exists())

    def test_missing_managed_state_root_fails_before_native_and_is_not_created(self):
        self.settings["state_root"].rmdir()
        receipt = helper.run_once(self.session, {"action": "send-text", "target": "filehelper", "text": "合成"})
        self.assertEqual(receipt["error"], "helper_state_root_unavailable")
        self.assertFalse(self.settings["state_root"].exists())
        self.assertIsNone(self.module.service)

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_state_root_junction_resolves_at_use_time_without_rewriting_config(self):
        with tempfile.TemporaryDirectory(dir=isolation.ROOT) as directory:
            base = Path(directory)
            physical, logical = base / "physical", base / "logical"
            physical.mkdir()
            (physical / "helper-state").mkdir()
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(logical), str(physical)],
                                    capture_output=True, timeout=20,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            try:
                self.settings["state_root"] = logical / "helper-state"
                self.session.start()
                for name in ("media_directory", "file_directory", "voice_directory", "emoticon_directory"):
                    path = self.module.service.options[name]
                    self.assertTrue(path.is_relative_to(physical))
                    self.assertEqual(path.resolve(), path)
                self.assertEqual(self.settings["state_root"], logical / "helper-state")
                self.session.close()
            finally:
                # Remove only this synthetic junction, never its target tree.
                logical.rmdir()

    def test_old_local_settings_and_helper_precedence(self):
        with mock.patch.object(cli, "_local_settings", return_value={"config": "synthetic"}):
            with self.assertRaisesRegex(cli.ProductError, "helper_config_not_configured"):
                cli._helper_config_path()
        with mock.patch.object(cli, "_local_settings", return_value={"helper_config": "local"}):
            self.assertEqual(cli._helper_config_path(), Path("local"))
            with mock.patch.dict(cli.os.environ, {"WECHAT_DIRECT_HELPER_CONFIG": "environment"}):
                self.assertEqual(cli._helper_config_path(), Path("environment"))
                self.assertEqual(cli._helper_config_path("explicit"), Path("explicit"))

    def test_source_and_native_identity_commitments_are_checked_separately(self):
        account = {"config_path": "unused", "local_state_path": "unused",
                   "expected_source_identity_sha256": cli._sha256((WXID + "_abcd").encode()),
                   "expected_moments_author_sha256": COMMITMENT}
        with mock.patch.object(cli, "load_direct_source_identity", return_value=(None, None, WXID + "_abcd")):
            self.assertEqual(cli._helper_expected_identity(account), WXID)
            account["expected_moments_author_sha256"] = account["expected_source_identity_sha256"]
            with self.assertRaisesRegex(cli.ProductError, "moments_identity_binding_mismatch"):
                cli._helper_expected_identity(account)

    def test_reading_cli_import_never_imports_optional_helper(self):
        result = subprocess.run([sys.executable, "-B", "-c",
                                 "import sys,wechat_cli; assert 'wechat_helper' not in sys.modules; assert 'text_service' not in sys.modules"],
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_requires_explicit_account_for_send(self):
        with self.assertRaises(cli.ProductError):
            cli.parser().parse_args(["helper", "send-text", "--target", "filehelper", "--text", "x"])
        args = cli.parser().parse_args(["helper", "send-text", "--account", "primary", "--target", "filehelper", "--text", "x"])
        self.assertEqual(args.helper_action, "send-text")


if __name__ == "__main__":
    unittest.main()
