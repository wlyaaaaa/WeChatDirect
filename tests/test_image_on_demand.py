import isolation  # noqa: F401
import hashlib
import io
import json
from pathlib import Path
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_native_media_source as fixtures
import test_helper_image_cache as cache
import wechat_cli as cli
from wechat_media import LocalMedia


class ImageOnDemandTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.NativeMediaSourceTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.folder, self.locator = self.case.packed_image()
        (self.folder / (self.case.md5 + "_t.dat")).write_bytes(self.case.png)
        self.reader = self.case.reader
        self.reader.close = lambda: None
        self.reader._identity = "wxid_synthetic"
        self.reader.list_sessions = lambda: [{"id": self.case.session}]
        self.config = {"secondary": {"expected_moments_author_sha256": "sha256:" + hashlib.sha256(b"wxid_synthetic").hexdigest()}}

    def run_open(self, media, *, local_only=False, background=False):
        output = SimpleNamespace(buffer=io.BytesIO())
        argv = ["media-open", "--account", "secondary", "--locator", self.locator, "--output", str(self.case.root / "output")]
        with patch("wechat_cli._read_config", return_value=self.config), patch("wechat_cli._reader", return_value=self.reader), \
                patch("wechat_cli._helper_config_path", return_value=Path("unused")), patch("wechat_helper.load_settings", return_value={}), \
                patch("wechat_helper.download_image_for_open", return_value=(media, {"attempted": True, "reason": None if media else "native_image_download_timeout" if background else "native_image_download_failed", "backgroundPending": background, "downloadElapsedSeconds": 1.25})) as download, \
                patch.object(cli.sys, "stdout", output):
            self.assertEqual(cli.command_media_open(cli.parser().parse_args(argv + (["--local-only"] if local_only else []))), 0)
        return json.loads(output.buffer.getvalue()), download

    def test_explicit_open_downloads_standard_without_exposing_metadata(self):
        self.case.messages.execute(f"UPDATE {self.case.table} SET message_content=?", (f'<img md5="{self.case.md5}" cdnmidimgurl="https://synthetic.invalid/private" aeskey="synthetic-private-key"/>',))
        media = LocalMedia(self.case.png, "normal.png", "image/png", "standard", "native_helper_on_demand")
        receipt, download = self.run_open(media)
        self.assertEqual((receipt["status"], receipt["quality"], receipt["materializationSource"]), ("success", "standard", "native_helper_on_demand"))
        self.assertEqual(receipt["imageDownload"]["downloadElapsedSeconds"], 1.25)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(download.call_args.kwargs["chat_id"], self.case.session)
        self.assertNotIn("synthetic-private", json.dumps(receipt))
        self.assertNotIn("synthetic.invalid", json.dumps(receipt))
        cached = cache.CachedImageTests()
        cached.setUp()
        self.addCleanup(cached.doCleanups)
        cache.helper._cache_ready_image(cached.session, cached.event | {"chat_id": self.case.session, "native_message_id": 101})
        self.reader._helper_image_provider = lambda payload: cache.helper.read_cached_image(cached.settings, account="secondary", account_sha256=cached.sha, chat_hash=self.case.table[4:], server_id=payload["server_id"])
        Path(receipt["output"]).unlink()
        second, download = self.run_open(None)
        self.assertEqual((second["quality"], second["materializationSource"], download.call_count), ("standard", "native_helper_download", 0))

    def test_failed_download_or_local_only_delivers_prominent_thumbnail(self):
        for local, background in ((False, False), (True, False), (False, True)):
            receipt, download = self.run_open(None, local_only=local, background=background)
            self.assertEqual((receipt["status"], receipt["quality"]), ("partial", "thumbnail"))
            self.assertTrue(receipt["reason"] and receipt["warning"] and receipt["nextAction"])
            self.assertEqual(receipt["nextAction"], "use_thumbnail_for_gist_and_tell_owner")
            self.assertIn("AI 回复时要告诉本人这次看的是缩略图", receipt["warning"])
            if background:
                self.assertIn("正常图还在后台下载", receipt["warning"])
                self.assertEqual(receipt["reason"], "native_image_download_timeout")
            self.assertEqual(download.call_count, 0 if local else 1)
            Path(receipt["output"]).unlink()

    def test_context_and_reader_remain_local_and_report_download_option(self):
        with patch("wechat_cli._download_image_for_open") as download:
            metadata = self.reader.resolve_locator(self.locator)
            self.assertEqual(metadata["resolution_gap"], "only_thumbnail_local")
            self.assertTrue(metadata["standardDownload"]["requiresLogin"])
            self.assertEqual(self.reader.open_locator(self.locator), self.case.png)
            download.assert_not_called()


SYNTHETIC_WORKER = '''
import sys
sys.path.insert(0, "tests")
import isolation, io, time
from pathlib import Path
from types import SimpleNamespace
from PIL import Image
from test_wechat_helper import Module, Service, COMMITMENT
import wechat_helper as h
def native(settings):
    root = settings["state_root"]
    mode = (root / "mode").read_text()
    def log(value):
        with (root / "trace").open("a") as stream: stream.write(value + "\\n")
    class FakeService(Service):
        def _download_image(self, metadata, destination):
            log("download")
            if mode == "failed": return False
            if mode == "retry" and (root / "trace").read_text().count("download") == 1: return False
            if mode == "hang": time.sleep(0.7)
            body = io.BytesIO()
            Image.new("RGB", (2, 2), "green").save(body, format="PNG")
            destination.write_bytes(body.getvalue())
            return True
        def stop(self):
            log("stop")
            return super().stop()
    module = Module()
    module.NativeTextService = lambda **options: setattr(module, "service", FakeService(**options)) or module.service
    if mode == "login": module.find_account_process = lambda expected: (_ for _ in ()).throw(RuntimeError("等待绑定的机器人账号登录"))
    h.take_send_turn = lambda *args, **kwargs: SimpleNamespace(close=lambda: log("turn"))
    images = h._image_cache_paths(settings, "secondary", COMMITMENT, h.hashlib.md5(b"filehelper").hexdigest(), 123)[1]
    path = images / "normal.png"
    package = settings["runtime_path"] / "wxbot_framework"
    modules = {"wxbot_framework.image_metadata": SimpleNamespace(__file__=package / "image_metadata.py",
        parse_image_metadata=lambda xml: SimpleNamespace(aeskey="fake", cdnmidimgurl="fake", filekey="", cdnbigimgurl=""),
        ImageStore=lambda *args: SimpleNamespace(save=lambda body: (path.write_bytes(body), path)[1])),
        "wxbot_framework.image_receiver": SimpleNamespace(__file__=package / "image_receiver.py",
            ImageReceiver=SimpleNamespace(_read_download=lambda path: path.read_bytes()))}
    h.importlib.import_module = modules.__getitem__
    return module
h.native_module = native
h._image_download_worker()
'''


class ImageWorkerTests(unittest.TestCase):
    def setUp(self):
        self.case = cache.CachedImageTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.settings["runtime_path"] = self.case.settings["state_root"] / "runtime"
        self.root = self.case.settings["state_root"]
        self.request = dict(account="secondary", expected_wxid="wxid_synthetic", expected_sha256=self.case.sha,
            chat_id="filehelper", server_id=123, attributes={"aeskey": "private-key", "cdnmidimgurl": "https://private.invalid"})
        self.children, self.spawn = [], subprocess.Popen
        self.addCleanup(self.finish)

    def launch(self, arguments, **kwargs):
        self.assertNotIn("private", repr(arguments))
        self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)
        time.sleep(0.03)  # Expose callers racing a previous receipt while the next child starts.
        child = self.spawn(arguments[:-1] + [SYNTHETIC_WORKER], **kwargs)
        self.children.append(child)
        return child

    def run_download(self, seconds=5):
        return cache.helper.download_image_for_open(self.case.settings, deadline=time.monotonic() + seconds, **self.request)

    def finish(self):
        for child in self.children:
            self.assertEqual(child.wait(timeout=5), 0)

    def test_success_retry_and_login_use_real_worker_and_cleanup(self):
        for mode, attempts, trace in (("success", 1, ["download", "stop", "turn"]),
                ("retry", 2, ["download", "stop", "turn"] * 2), ("failed", 2, ["download", "stop", "turn"] * 2), ("login", 1, [])):
            with self.subTest(mode=mode), patch.object(cache.helper.subprocess, "Popen", side_effect=self.launch):
                (self.root / "mode").write_text(mode)
                (self.root / "trace").unlink(missing_ok=True)
                self.case.index.unlink(missing_ok=True)
                started = time.monotonic()
                media, receipt = self.run_download()
                self.finish()
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(receipt["attempts"], attempts)
                self.assertEqual((self.root / "trace").read_text().splitlines() if (self.root / "trace").exists() else [], trace)
                self.assertNotIn("private", json.dumps(receipt))
                self.assertEqual(media.quality if media else None, None if mode in ("login", "failed") else "standard")
                if mode == "login":
                    self.assertEqual(receipt["reason"], "bound_account_not_logged_in")

    def test_timeout_continues_safely_and_concurrent_callers_share_download(self):
        (self.root / "mode").write_text("hang")
        result = self.case.index.with_suffix(".download.json")
        result.parent.mkdir(parents=True)
        result.write_text(json.dumps({"pending": False, "receipt": {"reason": "previous_failure"}}))
        with patch.object(cache.helper.subprocess, "Popen", side_effect=self.launch), ThreadPoolExecutor(2) as pool:
            started = time.monotonic()
            results = list(pool.map(lambda _: self.run_download(0.35), range(2)))
            self.assertLess(time.monotonic() - started, 0.65)
            self.assertEqual(len(self.children), 1)
            for media, receipt in results:
                self.assertIsNone(media)
                self.assertEqual(receipt["reason"], "native_image_download_timeout")
                self.assertTrue(receipt["backgroundPending"])
                self.assertNotIn("private", json.dumps(receipt))
            self.finish()
        self.assertEqual((self.root / "trace").read_text().splitlines(), ["download", "stop", "turn"])
        with patch.object(cache.helper.subprocess, "Popen", side_effect=AssertionError("must read index")):
            self.assertEqual(self.case.read().quality, "standard")
