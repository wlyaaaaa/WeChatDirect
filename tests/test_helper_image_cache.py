import isolation  # noqa: F401
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
from PIL import Image
import wechat_cli as cli
import wechat_helper as helper
from test_wechat_helper import Module, Service


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

    def test_bound_download_succeeds_or_reports_failure_and_always_cleans_up(self):
        self.settings["runtime_path"] = self.settings["state_root"] / "runtime"
        package = self.settings["runtime_path"] / "wxbot_framework"
        body = self.path.read_bytes()
        parser = Mock(side_effect=lambda xml: SimpleNamespace(**(dict(aeskey="", cdnmidimgurl="", filekey="", cdnbigimgurl="") | ET.fromstring(xml)[0].attrib)))
        store = Mock(return_value=SimpleNamespace(save=lambda data: (self.path.write_bytes(data), self.path)[1]))
        modules = {"wxbot_framework.image_metadata": SimpleNamespace(__file__=package / "image_metadata.py", parse_image_metadata=parser, ImageStore=store),
                   "wxbot_framework.image_receiver": SimpleNamespace(__file__=package / "image_receiver.py", ImageReceiver=SimpleNamespace(_read_download=lambda path: path.read_bytes()))}
        for outcome in ("success", "failed", "raise", "timeout", "startup", "identity_before", "identity_after", "invalid", "cleanup", "thumbnail"):
            with self.subTest(outcome=outcome):
                module, turn = Module(), Mock()
                if outcome == "startup":
                    module.find_account_process = Mock(side_effect=RuntimeError("等待绑定的机器人账号登录"))
                if outcome == "identity_before":
                    module.inspect_account = Mock(side_effect=["wxid_synthetic", "wxid_synthetic", "wxid_other"])
                def download(metadata, destination):
                    if outcome in ("raise", "timeout"):
                        raise (TimeoutError if outcome == "timeout" else RuntimeError)("never-return-raw-url-or-key")
                    if outcome == "identity_after":
                        module.account = "wxid_other"
                    if outcome == "cleanup":
                        module.service.cleanup = {"code": 1, "dll_unloaded": False}
                    destination.write_bytes(b"not an image" if outcome == "invalid" else body)
                    return outcome != "failed"
                attributes = {"aeskey": "synthetic-key", "cdnmidimgurl": "synthetic<&"}
                if outcome == "thumbnail":
                    attributes = {"cdnthumburl": "synthetic-thumb", "cdnthumbaeskey": "synthetic-key"}
                with patch.object(helper, "native_module", return_value=module), patch.object(helper, "take_send_turn", return_value=turn), \
                        patch.object(helper.importlib, "import_module", side_effect=modules.__getitem__), \
                        patch.object(helper.HelperSession, "close", autospec=True, side_effect=helper.HelperSession.close) as close, \
                        patch.object(Service, "_download_image", side_effect=download, create=True) as call:
                    media, receipt = helper.download_bound_image(self.settings, account="secondary", expected_wxid="wxid_synthetic",
                        expected_sha256=self.sha, chat_id="filehelper", server_id=123, attributes=attributes)
                if module.service is not None:
                    self.assertEqual(module.service.options["image_allowlist"], [""])
                    self.assertEqual(module.service.stop_calls, 1)
                close.assert_called_once()
                self.assertEqual(turn.close.call_count, 0 if outcome == "startup" else 1)
                self.assertTrue(receipt["attempted"])
                self.assertGreaterEqual(receipt["downloadElapsedSeconds"], 0)
                self.assertNotIn("synthetic", json.dumps(receipt))
                self.assertNotIn("never-return", json.dumps(receipt))
                self.assertEqual(receipt["cleanup"]["status"], "incomplete" if outcome == "cleanup" else "success" if module.service else "not_needed")
                if outcome in ("timeout", "startup"):
                    self.assertEqual(receipt["diagnosticCode"], {"timeout": "native_timeout", "startup": "bound_account_not_logged_in"}[outcome])
                if outcome in ("success", "cleanup"):
                    self.assertEqual((media.quality, media.source, media.payload), ("standard", "native_helper_on_demand", body))
                    self.assertIsNone(receipt["reason"])
                    self.assertEqual(self.read().source, "native_helper_download")
                else:
                    self.assertIsNone(media)
                    self.assertIsNotNone(receipt["reason"])
                if outcome in ("thumbnail", "startup", "identity_before"):
                    call.assert_not_called()
                self.assertFalse(list(self.settings["state_root"].rglob(".bound-image-*")))
