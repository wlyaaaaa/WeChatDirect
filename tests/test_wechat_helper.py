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
from types import SimpleNamespace
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

    def test_cache_key_reader_checks_identity_and_fingerprint_without_service(self):
        runtime = self.settings["state_root"] / "runtime"
        (runtime / "native").mkdir(parents=True)
        (runtime / "native" / "compatibility.json").write_text("{}", encoding="utf-8")
        settings = {**self.settings, "runtime_path": runtime}
        core = (4096, "synthetic-weixin.dll")
        native = mock.Mock()
        native.find_account_process.return_value = 123
        native.modules.return_value = {"weixin.dll": core}
        native.inspect_account.return_value = WXID
        native._inspect_identity.side_effect = lambda pid, read, **kw: read(None, core[0])
        probe = SimpleNamespace(__file__=runtime / "native" / "run_selftest.py",
                                compatible_core=mock.Mock(return_value={"matched": True}))
        cache = SimpleNamespace(__file__=runtime / "wxbot_framework" / "emoticon_account.py",
                                read_emoticon_cache_key=mock.Mock(return_value=b"synthetic-key-16"))
        modules = {"run_selftest": probe, "wxbot_framework.emoticon_account": cache}
        with mock.patch.object(helper, "native_module", return_value=native), \
                mock.patch.object(helper.importlib, "import_module", side_effect=modules.__getitem__):
            self.assertEqual(helper.read_emoticon_cache_key(
                settings, expected_wxid=WXID, expected_sha256=COMMITMENT), b"synthetic-key-16")
            native.NativeTextService.assert_not_called()
            for fault in ("commitment", "fingerprint", "identity_before", "identity_after", "core_after"):
                with self.subTest(fault=fault):
                    native.inspect_account.side_effect = None
                    native.modules.side_effect = None
                    probe.compatible_core.return_value = None if fault == "fingerprint" else {}
                    if fault.startswith("identity"):
                        native.inspect_account.side_effect = (["wxid_other"] if fault.endswith("before")
                                                             else [WXID, "wxid_other"])
                    if fault == "core_after":
                        native.modules.side_effect = [{"weixin.dll": core}, {"weixin.dll": (8192, core[1])}]
                    with self.assertRaises(helper.HelperError):
                        helper.read_emoticon_cache_key(settings, expected_wxid=WXID,
                            expected_sha256="sha256:" + "0" * 64 if fault == "commitment" else COMMITMENT)
            native.NativeTextService.assert_not_called()

    def test_reader_cache_key_callback_is_lazy_and_retains_fixed_errors(self):
        account = {"config_path": "unused", "local_state_path": "unused",
                   "expected_moments_author_sha256": COMMITMENT,
                   "expected_source_identity_sha256": "sha256:" + "a" * 64}
        reader = mock.Mock(account_identity_commitment="a" * 64)
        with mock.patch.object(cli, "DirectWeChatReader", return_value=reader) as build, \
                mock.patch.object(cli, "_plaintext_cache_setting", return_value=None), \
                mock.patch.object(cli, "_helper_emoji_cache_key", return_value=b"synthetic-key-16") as key:
            self.assertIs(cli._reader(account, 123), reader)
            key.assert_not_called()
            self.assertEqual(build.call_args.kwargs["emoticon_cache_key_provider"](), b"synthetic-key-16")
        with mock.patch.object(cli, "_helper_config_path", return_value=Path("synthetic")), \
                mock.patch.object(helper, "load_settings", return_value=self.settings), \
                mock.patch.object(cli, "_helper_expected_identity", return_value=WXID), \
                mock.patch.object(helper, "read_emoticon_cache_key", side_effect=helper.HelperError(
                    "helper_emoticon_cache_key_unavailable", diagnosticCode="native_core_fingerprint_mismatch")):
            with self.assertRaises(cli.ProductError) as caught:
                cli._helper_emoji_cache_key(account)
            self.assertEqual(caught.exception.details["diagnosticCode"], "native_core_fingerprint_mismatch")

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
            parent, parent_alias = base / "parent", base / "parent-alias"
            parent.mkdir()
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(parent_alias), str(parent)],
                                    capture_output=True, timeout=20,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            physical, logical = parent_alias / "physical", parent_alias / "logical"
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
                    self.assertTrue(path.is_relative_to(physical.resolve()))
                    self.assertEqual(path.resolve(), path)
                self.assertEqual(self.settings["state_root"], logical / "helper-state")
                self.session.close()
            finally:
                # Remove only this synthetic junction, never its target tree.
                logical.rmdir()
                parent_alias.rmdir()

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

    def test_stale_alias_name_fallback_cannot_invoke_native_send(self):
        args = cli.parser().parse_args([
            "helper", "send-text", "--helper-config", "synthetic-helper.json",
            "--config", "synthetic-config.json", "--account", "primary",
            "--contact", "synthetic_alias", "--text", "synthetic"])
        reader = mock.Mock()
        reader.list_contacts.return_value = [
            {"nativeId": "wxid_other_synthetic", "remark": "synthetic_alias"}]
        stale = {"name": "synthetic_alias", "account": "primary", "nativeId": "wxid_absent",
                 "chatType": "private", "label": "old_label", "addedAt": "2026-01-01"}
        with (
            mock.patch.object(helper, "load_settings", return_value=self.settings),
            mock.patch.object(cli, "_read_config", return_value={
                "primary": {"expected_moments_author_sha256": COMMITMENT}}),
            mock.patch.object(cli, "_helper_expected_identity", return_value=WXID),
            mock.patch.object(cli, "_read_aliases", return_value=[stale]),
            mock.patch.object(cli, "_reader", return_value=reader),
            mock.patch.object(helper, "run_once") as invoke,
            mock.patch.object(helper, "native_module") as load_native,
        ):
            with self.assertRaisesRegex(cli.ProductError, "helper_stale_alias_requires_resolution") as caught:
                cli.command_helper(args)
        self.assertEqual(caught.exception.details["staleAliases"][0]["label"], "old_label")
        self.assertEqual(caught.exception.details["nextAction"],
                         "verify_explicit_target_or_repair_alias_before_sending")
        reader.close.assert_called_once_with()
        invoke.assert_not_called()
        load_native.assert_not_called()

    def test_exact_and_live_alias_targets_remain_available_and_close_reader(self):
        stale = {"name": "synthetic_alias", "account": "primary", "nativeId": "wxid_absent",
                 "chatType": "private", "label": "old_label", "addedAt": "2026-01-01"}
        for query, name, aliases in (
            ("synthetic_name", "synthetic_name", []),
            ("synthetic_alias", "different_name", [stale, {**stale, "nativeId": "wxid_chosen"}]),
        ):
            with self.subTest(query=query):
                args = cli.parser().parse_args([
                    "helper", "send-text", "--account", "primary", "--contact", query, "--text", "synthetic"])
                reader = mock.Mock()
                reader.list_contacts.return_value = [{"nativeId": "wxid_chosen", "remark": name}]
                with (
                    mock.patch.object(cli, "_read_aliases", return_value=aliases),
                    mock.patch.object(cli, "_reader", return_value=reader),
                ):
                    self.assertEqual(cli._helper_target(args, {"primary": {}}, Path("synthetic.json")),
                                     "wxid_chosen")
                reader.close.assert_called_once_with()

    def test_explicit_target_does_not_resolve_names_or_aliases(self):
        args = cli.parser().parse_args([
            "helper", "send-text", "--account", "primary", "--target", "wxid_explicit", "--text", "synthetic"])
        with mock.patch.object(cli, "_resolve_contact", side_effect=AssertionError("must not resolve names")):
            self.assertEqual(cli._helper_target(args, {}, Path("synthetic.json")), "wxid_explicit")

    def _voice_recovery_fixture(self):
        self.module.account = WXID
        self.session = helper.HelperSession(self.settings, account="primary", expected_wxid=WXID,
                                            expected_sha256=COMMITMENT, module=self.module)
        self.session.start()
        body = b"\x02#!SILK_V3\x01\x00x"
        path = self.settings["state_root"] / "synthetic-received.silk"
        def save(data):
            path.write_bytes(data)
            return path
        receiver = SimpleNamespace(_active=mock.Mock(return_value=True), _save=mock.Mock(side_effect=save),
                                   _remove_owned=mock.Mock(side_effect=lambda value: value.unlink(missing_ok=True)))
        self.module.service._voice_receiver = receiver
        self.session.voice_reader = mock.Mock(return_value=body)
        inspector = SimpleNamespace(_inspect_silk=mock.Mock(return_value=SimpleNamespace(silk=body, duration_ms=20)))
        event = dict(chat_id="filehelper", sender_id=WXID, native_sender=WXID, native_receiver="filehelper",
                     is_self=True, sequence=123, native_message_id="456", wx_type=34, voice_status="failed")
        return event, receiver, path, inspector

    def test_failed_voice_recovers_exact_bytes_metadata_and_concurrent_buffer_count(self):
        event, receiver, path, inspector = self._voice_recovery_fixture()
        body = self.session.voice_reader.return_value
        def read(data):
            self.module.service.handler(123, 11046, dict(chat_id="filehelper", wx_type=1, msg="next"), None)
            return body
        self.session.voice_reader.side_effect = read
        self.module.service.handler(123, 11046, event, None)
        self.session.voice_reader.assert_not_called()
        with mock.patch.object(helper.importlib, "import_module", return_value=inspector):
            receipt = self.session.execute(dict(action="receive", seconds=0, limit=1))
        recovered = receipt["events"][0]
        self.assertEqual({key: recovered[key] for key in event if key != "voice_status"},
                         {key: value for key, value in event.items() if key != "voice_status"})
        self.assertEqual((recovered["voice_status"], recovered["voice_source"], recovered["voice_duration_ms"]),
                         ("ready", "local_database", 20))
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(recovered["voice_size"], len(body))
        self.assertEqual(receipt["bufferedRemaining"], 1)
        receiver._remove_owned.assert_not_called()
        self.session.close()

    def test_native_ready_and_nonvoice_events_never_request_database_voice(self):
        event, _receiver, _path, inspector = self._voice_recovery_fixture()
        for change in ({"voice_status": "ready"}, {"wx_type": 1}):
            self.module.service.handler(123, 11046, {**event, **change}, None)
        with mock.patch.object(helper.importlib, "import_module", return_value=inspector):
            receipt = self.session.execute(dict(action="receive", seconds=0, limit=2))
        self.assertEqual(len(receipt["events"]), 2)
        self.session.voice_reader.assert_not_called()
        inspector._inspect_silk.assert_not_called()
        self.session.close()

    def test_received_voice_database_lookup_rejects_conflicts_and_bounds_fresh_attempts(self):
        event = dict(chat_id="synthetic-chat", sender_id=WXID, native_message_id="456", is_self=True)
        message = dict(type="voice", serverId="456", senderUsername=WXID, isSend=True,
                       media_manifest=[dict(kind="voice", openable=True, locator="synthetic-locator")])
        for change in (dict(type="text"), dict(serverId="other"), dict(senderUsername="other"), dict(isSend=False)):
            reader = mock.MagicMock()
            reader.__enter__.return_value = reader
            reader.fetch_message_by_server_id.return_value = {**message, **change}
            with self.subTest(change=change), mock.patch.object(cli, "_reader", return_value=reader) as factory:
                self.assertIsNone(cli._helper_received_voice({}, event))
            factory.assert_called_once()
            reader.open_locator.assert_not_called()
            reader.__exit__.assert_called_once()
        readers = [mock.MagicMock() for _ in range(3)]
        for index, reader in enumerate(readers):
            reader.__enter__.return_value = reader
            reader.fetch_message_by_server_id.return_value = message if index == 2 else None
        readers[-1].open_locator.return_value = b"synthetic-silk"
        with mock.patch.object(cli, "_reader", side_effect=readers) as factory, mock.patch.object(cli.time, "sleep") as sleep:
            self.assertEqual(cli._helper_received_voice({}, event), b"synthetic-silk")
        self.assertEqual(factory.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(0.15), mock.call(0.15)])
        for reader in readers:
            reader.fetch_message_by_server_id.assert_called_once_with("synthetic-chat", "456", exact_media_lookup=True)
            reader.__exit__.assert_called_once()
        with mock.patch.object(cli, "_reader", side_effect=cli.ProductError("wechat_account_identity_binding_mismatch")) as factory:
            with self.assertRaisesRegex(cli.ProductError, "identity_binding_mismatch"):
                cli._helper_received_voice({}, event)
        factory.assert_called_once()

    def test_voice_recovery_account_change_cache_failure_and_missing_payload_never_ready(self):
        for mode in ("identity", "inactive", "save", "unavailable", "invalid"):
            with self.subTest(mode=mode):
                event, receiver, path, inspector = self._voice_recovery_fixture()
                if mode == "identity":
                    save = receiver._save.side_effect
                    def switch(data):
                        result = save(data)
                        self.module.account = "wxid_other"
                        return result
                    receiver._save.side_effect = switch
                elif mode == "inactive":
                    receiver._active.side_effect = [True, True, False]
                elif mode == "save":
                    receiver._save.side_effect = OSError("synthetic failure")
                else:
                    self.session.voice_reader.return_value = None if mode == "unavailable" else b"invalid"
                self.module.service.handler(123, 11046, event, None)
                with mock.patch.object(helper.importlib, "import_module", return_value=inspector):
                    if mode == "identity":
                        with self.assertRaisesRegex(helper.HelperError, "identity_mismatch"):
                            self.session.execute(dict(action="receive", seconds=0, limit=1))
                        self.assertFalse(self.session.started)
                    else:
                        receipt = self.session.execute(dict(action="receive", seconds=0, limit=1))
                        self.assertEqual(receipt["events"][0]["voice_status"], "failed")
                self.assertFalse(path.exists())
                if mode in {"identity", "inactive"}:
                    receiver._remove_owned.assert_called_once_with(path)
                if self.session.started:
                    self.session.close()


if __name__ == "__main__":
    unittest.main()
