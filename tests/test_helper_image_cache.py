import isolation  # noqa: F401
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
import wechat_cli as cli
import wechat_helper as helper


class CachedImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=isolation.ROOT)
        self.addCleanup(temporary.cleanup)
        self.settings = {"state_root": Path(temporary.name)}
        self.sha = helper._identity_hash("wxid_synthetic")
        self.chat = hashlib.md5(b"filehelper", usedforsecurity=False).hexdigest()
        self.session = helper.HelperSession(self.settings, account="secondary", expected_wxid="wxid_synthetic", expected_sha256=self.sha)
        _, images, self.index = helper._image_cache_paths(self.settings, "secondary", self.sha, self.chat, 123)
        images.mkdir(parents=True)
        self.path = images / "image.png"
        Image.new("RGB", (2, 2), "green").save(self.path)
        self.event = dict(chat_id="filehelper", native_message_id=123, wx_type=3, image_status="ready", image_quality="standard", image_path=str(self.path))

    def read(self, **overrides):
        return helper.read_cached_image(self.settings, **(dict(account="secondary", account_sha256=self.sha, chat_hash=self.chat, server_id=123) | overrides))

    def test_exact_cache_and_validation(self):
        self.session._on_message(None, None, self.event, None)
        result = self.read()
        self.assertEqual((result.quality, result.source, result.payload), ("standard", "native_helper_download", self.path.read_bytes()))
        self.assertFalse(list(self.settings["state_root"].rglob("*.tmp")))
        for overrides in ({"server_id": 124}, {"chat_hash": "a" * 32}, {"account": "primary"}, {"account_sha256": "sha256:" + "a" * 64}):
            self.assertIsNone(self.read(**overrides))
        record = json.loads(self.index.read_text())
        for key, value in (("sha256", "0" * 64), ("size", -1), ("path", "../outside.png"), ("server_id", "124"), ("chat_hash", "a" * 32)):
            self.index.write_text(json.dumps(record | {key: value}))
            self.assertIsNone(self.read())
        self.session._on_message(None, None, self.event, None)
        self.path.write_bytes(b"not an image")
        self.session._on_message(None, None, self.event, None)
        self.assertIsNone(self.read())

    def test_rejects_outside_paths_and_unselected_or_thumbnail_events(self):
        for changes in ({"chat_id": "elsewhere"}, {"image_quality": "thumbnail"}, {"image_status": "pending"}, {"image_path": str(self.settings["state_root"] / "outside.png")}):
            self.session._on_message(None, None, self.event | changes, None)
            self.assertFalse(self.index.exists())
        self.assertFalse(list(self.settings["state_root"].rglob("*.tmp")))

    def test_cli_selected_slot_reads_only_its_exact_cached_image(self):
        self.session._on_message(None, None, self.event, None)
        account = cli._account_config({"secondary": {"expected_moments_author_sha256": self.sha}}, "secondary")
        payload = {"message_table": "Msg_" + self.chat, "server_id": 123}
        with patch("wechat_cli._helper_config_path", return_value=Path("unused")), patch("wechat_helper.load_settings", return_value=self.settings):
            self.assertEqual(cli._helper_cached_image(account, payload).quality, "standard")
            self.assertIsNone(cli._helper_cached_image(account, payload | {"server_id": 124}))
        with patch("wechat_cli._helper_config_path", side_effect=cli.ProductError("helper_config_not_configured")):
            self.assertIsNone(cli._helper_cached_image(account, payload))
