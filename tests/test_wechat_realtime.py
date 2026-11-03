import isolation  # noqa: F401

from concurrent.futures import Future, wait
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import wechat_cli as cli
import wechat_helper as helper
from wechat_realtime import RealtimeContextSession


class RealtimeContextTests(unittest.TestCase):
    def test_background_child_does_not_wait_for_or_consume_session_input(self):
        code = (
            "import sys, threading; from wechat_realtime import RealtimeContextSession; "
            "runner=object.__new__(RealtimeContextSession); runner.lock=threading.Lock(); "
            "runner.closed=False; runner.processes=set(); "
            "print(runner._run([sys.executable,'-c',"
            "'import sys; assert not sys.stdin.read(); print(123)'],2).strip(),flush=True)"
        )
        process = subprocess.Popen([sys.executable, '-B', '-c', code], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            # Keep the parent's control input open while the non-interactive child exits.
            process.wait(timeout=6)
            self.assertEqual(process.returncode, 0, process.stderr.read().decode('utf-8'))
            self.assertEqual(process.stdout.read().strip(), b'123')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def fixture(self, *, cached=False, clock=time.monotonic):
        temporary = tempfile.TemporaryDirectory(dir=isolation.ROOT)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        session = SimpleNamespace(account="primary", event_targets={"peer"}, settings={"state_root": root},
            expected_wxid="wxid-self", expected_sha256="synthetic", _on_message=mock.Mock(),
            _verify_identity=mock.Mock(), start=mock.Mock(), execute=mock.Mock(),
            close=mock.Mock(return_value={"dllUnloaded": True, "status": "success"}))
        message = {"serverId": "123", "type": "voice", "content": "缓存原文" if cached else None,
                   "senderUsername": "peer", "createTime": 1,
                   "media_manifest": [{"kind": "voice", "locator": "voice"}]}
        if cached:
            message["contentSource"] = "wechat_native_cache"
        reader = mock.MagicMock()
        reader.__enter__.return_value = reader
        reader.fetch_message_by_server_id.return_value = message
        reader.open_locator.return_value = b"\x02#!SILK_V3\x01\x00x"
        local = mock.Mock()
        local.prepare.return_value = {"event": "ready", "state": "ready"}
        wrapper = RealtimeContextSession(session, asr_root=root, account={}, reader_factory=lambda: reader,
                                         local=local, clock=clock)
        self.addCleanup(wrapper.close)
        return wrapper, session, reader, local

    def observe(self, session):
        session._on_message(1, 1, {"chat_id": "peer", "native_message_id": "123", "voice_status": "pending"}, None)

    def test_first_observation_cache_fast_path_and_same_message_request_reuse(self):
        current = [10.0]
        wrapper, session, reader, local = self.fixture(cached=True, clock=lambda: current[0])
        self.observe(session)
        current[0] = 12.0
        self.observe(session)
        with mock.patch.object(wrapper, "_candidate", side_effect=AssertionError("cache must not transcribe")):
            first = wrapper.execute({"action": "read-context", "target": "peer", "native_message_id": "123"})
            second = wrapper.execute({"action": "read-context", "target": "peer", "native_message_id": "123"})
        self.assertTrue(first["complete"])
        self.assertEqual(first["elapsed"], 2)
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["messages"][0]["contentSource"], "wechat_native_cache")
        self.assertTrue(Path(first["messages"][0]["media_manifest"][0]["output"]).is_file())
        reader.fetch_message_by_server_id.assert_called_once_with("peer", "123", exact_media_lookup=True)
        local.transcribe.assert_not_called()

    def test_first_complete_candidate_returns_while_same_cloud_request_finishes_later(self):
        wrapper, session, reader, _local = self.fixture()
        self.observe(session)
        observed = wrapper.observed[("peer", "123")][0]
        missing = mock.MagicMock()
        missing.__enter__.return_value = missing
        missing.fetch_message_by_server_id.return_value = None
        wrapper.reader_factory = mock.Mock(side_effect=[missing, reader])
        release = threading.Event()
        def candidate(route, _target, _message, _wav, _deadline):
            if route == "cloud":
                release.wait(2)
            return {"route": route, "nativeMessageId": "123", "status": "success" if route != "native" else "candidate_failed",
                    "text": route + "正文", "binding": {}, "finished": time.monotonic()}
        with mock.patch.object(wrapper, "_media", return_value=([], Path("selected.wav"))), mock.patch.object(wrapper, "_decode", return_value=Path("selected.wav")), mock.patch.object(wrapper, "_candidate", side_effect=candidate) as calls:
            first = wrapper.read_context({"target": "peer", "native_message_id": "123"})
            self.assertEqual(first["messages"][0]["content"], "local正文")
            self.assertTrue(first["complete"])
            self.assertEqual(wrapper.requests[first["request_id"]]["observed"][0], observed)
            self.assertEqual(wrapper.reader_factory.call_count, 2)
            self.assertEqual(first["diagnostics"][0]["code"], "realtime_exact_message_pending")
            self.assertGreater(first["pending"], 0)
            repeated = wrapper.read_context({"target": "peer", "native_message_id": "123"})
            self.assertEqual(first["request_id"], repeated["request_id"])
            release.set()
            wait(wrapper.requests[first["request_id"]]["futures"], timeout=2)
            later = wrapper.execute({"action": "context-results", "request_id": first["request_id"]})
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(later["pending"], 0)
        self.assertIn("cloud正文", [item.get("text") for item in later["candidates"]])

    def test_foreign_audio_hash_is_never_usable_text(self):
        wrapper, _session, reader, local = self.fixture()
        wav = Path(isolation.ROOT) / (str(time.monotonic_ns()) + ".wav")
        wav.write_bytes(b"selected audio")
        self.addCleanup(wav.unlink, missing_ok=True)
        message = reader.fetch_message_by_server_id.return_value
        local.transcribe.return_value = {"state": "completed", "coverage": {"status": "complete"}, "text": "wrong", "source_sha256": "foreign"}
        self.assertEqual(wrapper._candidate("local", "peer", message, wav, time.monotonic() + 5)["status"], "candidate_failed")
        with mock.patch.object(wrapper, "_run", return_value=json.dumps({"status": "succeeded", "text": "wrong", "source_audio_sha256": "foreign"})):
            self.assertEqual(wrapper._candidate("cloud", "peer", message, wav, time.monotonic() + 5)["status"], "candidate_failed")

    def test_materialized_visuals_require_real_ai_view_and_do_not_claim_complete(self):
        from PIL import Image
        wrapper, session, reader, _local = self.fixture()
        message = reader.fetch_message_by_server_id.return_value
        message.update(type="image", media_manifest=[{"kind": "image", "locator": "visual"}])
        data = io.BytesIO()
        Image.new("RGB", (2, 2)).save(data, "PNG")
        reader.open_locator.return_value = data.getvalue()
        self.observe(session)
        result = wrapper.read_context({"target": "peer", "native_message_id": "123"})
        self.assertTrue(result["materialComplete"])
        self.assertTrue(result["ready_for_vision"])
        self.assertFalse(result["complete"])
        self.assertFalse(result["onTime"])
        self.assertTrue(Path(result["messages"][0]["media_manifest"][0]["frames"][0]["path"]).is_file())

    def test_slow_materials_return_at_original_deadline_and_never_start_late_cloud(self):
        wrapper, session, _reader, _local = self.fixture()
        self.observe(session)
        key = ("peer", "123")
        wrapper.observed[key] = (time.monotonic() - 14.96, wrapper.observed[key][1])
        release, finished = threading.Event(), threading.Event()
        def slow(*_args):
            release.wait(2)
            finished.set()
            return [], Path("late-selected.silk")
        with mock.patch.object(wrapper, "_media", side_effect=slow), mock.patch.object(wrapper, "_candidate") as candidate:
            result = wrapper.read_context({"target": "peer", "native_message_id": "123"})
            self.assertFalse(finished.is_set())
            self.assertFalse(result["complete"])
            self.assertEqual(result["gaps"][0]["kind"], "materials_pending")
            release.set()
            wrapper.requests[result["request_id"]]["preparation"].result(timeout=2)
            candidate.assert_not_called()

    def test_main_and_quoted_content_gaps_prevent_complete(self):
        for location in ("main", "quote"):
            with self.subTest(location=location):
                wrapper, session, reader, _local = self.fixture(cached=True)
                self.observe(session)
                quoted = {"serverId": "456", "type": "unknown", "content": None,
                          "contentGap": "message_type_unsupported", "media_manifest": []}
                quotes = [quoted] if location == "quote" else []
                if location == "main":
                    reader.fetch_message_by_server_id.return_value["contentGap"] = "message_content_unparsed"
                with mock.patch.object(cli, "_attach_quote_targets", return_value=(quotes, [])):
                    result = wrapper.read_context({"target": "peer", "native_message_id": "123"})
                self.assertFalse(result["complete"])
                self.assertFalse(result["materialComplete"])
                self.assertIn("message_content_gap", [gap["kind"] for gap in result["gaps"]])

    def test_late_missing_scope_and_explicit_close_or_eof_are_honest(self):
        current = [0.0]
        wrapper, session, _reader, _local = self.fixture(cached=True, clock=lambda: current[0])
        with self.assertRaisesRegex(ValueError, "not_observed"):
            wrapper.read_context({"target": "other", "native_message_id": "123"})
        self.observe(session)
        current[0] = 16.0
        result = wrapper.read_context({"target": "peer", "native_message_id": "123"})
        self.assertTrue(result["late"])
        self.assertFalse(result["onTime"])
        for request in ("", '{"action":"close"}\n'):
            child, _session, _reader, local = self.fixture(cached=True)
            output = io.StringIO()
            helper.run_session(child, io.StringIO(request), output)
            self.assertTrue(json.loads(output.getvalue().splitlines()[-1])["cleanup"]["dllUnloaded"])
            local.close.assert_called_once()
        args = cli.parser().parse_args(["helper", "session", "--account", "primary", "--realtime", "--asr-root", "selected-asr"])
        self.assertTrue(args.realtime)

    def test_delivery_timing_includes_final_identity_and_is_stable_on_later_queries(self):
        for ready_at, identity_delay, on_time in ((7.882, 0, True), (14.9, 0.2, False)):
            with self.subTest(ready_at=ready_at):
                current, checks = [0.0], [0]
                wrapper, session, _reader, _local = self.fixture(cached=True, clock=lambda: current[0])
                self.observe(session)
                current[0] = ready_at
                def verify():
                    checks[0] += 1
                    if checks[0] == 2:
                        current[0] += identity_delay
                session._verify_identity.side_effect = verify
                first = wrapper.execute({"action": "read-context", "target": "peer", "native_message_id": "123"})
                self.assertEqual(first["onTime"], on_time)
                self.assertAlmostEqual(first["elapsed"], ready_at + identity_delay)
                self.assertEqual(first["materialDeliveryElapsed"], first["completeDeliveryElapsed"])
                late_candidate = Future()
                late_candidate.set_result({"route": "cloud", "nativeMessageId": "123", "status": "success",
                    "text": "后到正文", "binding": {}, "finished": 23.0})
                wrapper.requests[first["request_id"]]["futures"].append(late_candidate)
                current[0] = 24.0
                later = wrapper.execute({"action": "context-results", "request_id": first["request_id"]})
                self.assertEqual((later["elapsed"], later["onTime"], later["late"]),
                                 (first["elapsed"], first["onTime"], first["late"]))
                self.assertEqual(later["queryElapsed"], 24.0)
                self.assertEqual(later["candidates"][0]["finished"], 23.0)

    def test_close_failures_still_attempt_all_cleanup_and_eof_is_nonzero(self):
        for request in ("", '{"action":"close"}\n'):
            with self.subTest(request=request):
                wrapper, session, _reader, local = self.fixture(cached=True)
                local.close.side_effect = RuntimeError("local cleanup failed")
                wrapper.processes.update({object(), object()})
                output = io.StringIO()
                with mock.patch("wechat_realtime._stop", side_effect=[OSError("process cleanup failed"), None]) as stop, \
                        mock.patch.object(wrapper.pool, "shutdown", side_effect=RuntimeError("pool cleanup failed")) as shutdown:
                    code = helper.run_session(wrapper, io.StringIO(request), output)
                cleanup_receipt = json.loads(output.getvalue().splitlines()[-1])
                self.assertEqual((code, cleanup_receipt["status"]), (2, "partial"))
                self.assertTrue(cleanup_receipt["cleanup"]["dllUnloaded"])
                self.assertEqual(cleanup_receipt["cleanup"]["status"], "incomplete")
                self.assertEqual({gap["stage"] for gap in cleanup_receipt["cleanup"]["cleanupGaps"]},
                                 {"local_asr", "task_process", "candidate_pool"})
                self.assertTrue(wrapper.closed)
                self.assertEqual(stop.call_count, 2)
                shutdown.assert_called_once()
                session.close.assert_called_once()
                wrapper.processes.clear()


if __name__ == "__main__":
    unittest.main()
