import isolation  # noqa: F401
import hashlib
import io
import json
from pathlib import Path
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

    def run_open(self, media, *, local_only=False):
        output = SimpleNamespace(buffer=io.BytesIO())
        argv = ["media-open", "--account", "secondary", "--locator", self.locator, "--output", str(self.case.root / "output")]
        with patch("wechat_cli._read_config", return_value=self.config), patch("wechat_cli._reader", return_value=self.reader), \
                patch("wechat_cli._helper_config_path", return_value=Path("unused")), patch("wechat_helper.load_settings", return_value={}), \
                patch("wechat_helper.download_bound_image", return_value=(media, {"attempted": True, "reason": None if media else "native_image_download_failed", "downloadElapsedSeconds": 1.25})) as download, \
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
        for local in (False, True):
            receipt, download = self.run_open(None, local_only=local)
            self.assertEqual((receipt["status"], receipt["quality"]), ("partial", "thumbnail"))
            self.assertTrue(receipt["reason"] and receipt["warning"] and receipt["nextAction"])
            self.assertEqual(download.call_count, 0 if local else 1)
            Path(receipt["output"]).unlink()

    def test_context_and_reader_remain_local_and_report_download_option(self):
        with patch("wechat_cli._download_image_for_open") as download:
            metadata = self.reader.resolve_locator(self.locator)
            self.assertEqual(metadata["resolution_gap"], "only_thumbnail_local")
            self.assertTrue(metadata["standardDownload"]["requiresLogin"])
            self.assertEqual(self.reader.open_locator(self.locator), self.case.png)
            download.assert_not_called()
