from __future__ import annotations

import isolation  # noqa: F401

import hashlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import wechat_source
from wechat_cli import ProductError
from wechat_media import LocalMedia
from wechat_source import DirectCredentialError, DirectSchemaError, DirectWeChatReader, MediaNotOpenableError


class NativeMediaSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "account"
        self.storage = self.root / "db_storage"
        self.session = "synthetic-session"
        self.table = "Msg_" + hashlib.md5(self.session.encode(), usedforsecurity=False).hexdigest()
        self.paths = {}
        self.connections = {}
        for name in ("message_0.db", "hardlink.db", "emoticon.db"):
            path = self.storage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            self.addCleanup(connection.close)
            self.paths[name] = path
            self.connections[path] = connection
        self.messages = self.connections[self.paths["message_0.db"]]
        self.messages.execute(f"CREATE TABLE {self.table}(local_id INTEGER, server_id INTEGER, local_type INTEGER, message_content TEXT)")
        self.hardlink = self.connections[self.paths["hardlink.db"]]
        self.hardlink.execute("CREATE TABLE dir2id(username TEXT)")
        self.hardlink.executemany("INSERT INTO dir2id VALUES (?)", [(self.table[4:],), ("2026-09",)])
        self.hardlink.execute("CREATE TABLE image_hardlink_info_v4(md5 TEXT,file_name TEXT,dir1 INTEGER,dir2 INTEGER,modify_time INTEGER)")
        self.emoticon = self.connections[self.paths["emoticon.db"]]
        self.emoticon.execute("CREATE TABLE kNonStoreEmoticonTable(md5 TEXT,aes_key TEXT,cdn_url TEXT)")
        buffer = io.BytesIO()
        Image.new("RGB", (3, 2), (12, 34, 56)).save(buffer, format="PNG")
        self.png = buffer.getvalue()
        self.md5 = hashlib.md5(self.png, usedforsecurity=False).hexdigest()
        self.reader = object.__new__(DirectWeChatReader)
        self.reader._storage = self.storage
        self.reader.account_identity_commitment = "a" * 64
        self.reader._named_databases = lambda name: [self.paths[name]] if name in self.paths else []
        self.reader._open = lambda path: self.connections[path]

    def locator(self, kind, local_id=1, server_id=101):
        return self.reader._make_locator({
            "record": "native_media", "kind": kind,
            "message_database": "message_0.db", "message_table": self.table,
            "local_id": local_id, "server_id": server_id,
        })

    def add_image(self):
        self.messages.execute(f"INSERT INTO {self.table} VALUES (1,101,3,?)", (f'<msg><img md5="{self.md5}"/></msg>',))
        self.hardlink.execute("INSERT INTO image_hardlink_info_v4 VALUES (?,?,1,2,200)", (self.md5, self.md5 + ".dat"))
        path = self.root / "msg" / "attach" / self.table[4:] / "2026-09" / "Img" / (self.md5 + ".dat")
        path.parent.mkdir(parents=True)
        path.write_bytes(self.png)

    def test_exact_image_message_hardlink_and_real_bytes(self):
        self.add_image()
        locator = self.locator("image")
        pending = self.reader.resolve_locator(locator)
        self.assertIsNone(pending["openable"])
        self.assertTrue(pending["materializable"])
        self.assertFalse(pending["requiresNetwork"])
        self.assertEqual(self.png, self.reader.open_locator(locator))
        metadata = self.reader.resolve_locator(locator)
        self.assertEqual("image/png", metadata["mimeType"])
        self.assertEqual("local", metadata["materializationSource"])
        with self.assertRaises(MediaNotOpenableError):
            self.reader.open_locator(self.locator("image", server_id=999))
        with self.assertRaises(DirectSchemaError):
            self.reader.open_locator(locator.replace("a" * 64, "b" * 64))

    def test_file_and_video_bytes_must_match_the_bound_native_md5(self):
        for local_id, kind, local_type in ((4, "file", 49), (5, "video", 43)):
            with self.subTest(kind=kind):
                self.hardlink.execute(f"CREATE TABLE {kind}_hardlink_info_v4(md5 TEXT,file_name TEXT,dir1 INTEGER,dir2 INTEGER,modify_time INTEGER)")
                self.hardlink.execute(f"INSERT INTO {kind}_hardlink_info_v4 VALUES (?,?,1,2,200)", (self.md5, "synthetic.bin"))
                xml = (f'<appmsg><type>6</type><title>synthetic.bin</title><appattach><md5>{self.md5}</md5></appattach></appmsg>'
                       if kind == "file" else f'<videomsg md5="{self.md5}"/>')
                self.messages.execute(f"INSERT INTO {self.table} VALUES (?,?,?,?)", (local_id, local_id + 100, local_type, f"<msg>{xml}</msg>"))
                path = self.root / "msg" / kind / self.table[4:] / "2026-09" / "synthetic.bin"
                path.parent.mkdir(parents=True)
                path.write_bytes(b"different bytes at the correct native path")
                locator = self.locator(kind, local_id, local_id + 100)
                with self.assertRaises(MediaNotOpenableError):
                    self.reader.open_locator(locator)
                path.write_bytes(self.png)
                self.assertEqual(self.png, self.reader.open_locator(locator))

    def test_app_emoji_uses_bound_md5_and_keeps_missing_media_gap(self):
        xml = f'<msg><appmsg><type>8</type><appattach><emoticonmd5>{self.md5}</emoticonmd5><totallen>{len(self.png)}</totallen></appattach></appmsg></msg>'
        self.messages.execute(f"INSERT INTO {self.table} VALUES (2,102,?,?)", ((8 << 32) | 49, xml))
        locator = self.locator("emoji", 2, 102)
        attributes = self.reader._native_media_attributes(self.reader._decode_locator(locator))
        self.assertEqual(attributes, {"md5": self.md5, "len": str(len(self.png))})
        with patch("wechat_media.fetch_emoji_media") as fetch:
            metadata = self.reader.resolve_locator(locator)
            self.assertEqual(metadata["resolution_gap"], "native_media_missing_or_unreadable")
            self.assertFalse(metadata["openable"])
            fetch.assert_not_called()

    def test_emoji_network_is_only_used_by_explicit_materialization(self):
        self.messages.execute(f"INSERT INTO {self.table} VALUES (2,102,47,?)", (f'<msg><emoji md5="{self.md5}" len="{len(self.png)}"/></msg>',))
        url = "https://emoji.qpic.cn/synthetic-native-item"
        self.emoticon.execute("INSERT INTO kNonStoreEmoticonTable VALUES (?,NULL,?)", (self.md5, url))
        locator = self.locator("emoji", 2, 102)
        with patch("wechat_media.fetch_emoji_media", return_value=LocalMedia(self.png, "emoji.png", "image/png", "original", source="remote")) as fetch:
            metadata = self.reader.resolve_locator(locator)
            self.assertIsNone(metadata["openable"])
            self.assertTrue(metadata["materializable"])
            fetch.assert_not_called()
            with self.assertRaises(MediaNotOpenableError):
                self.reader.open_locator(locator)
            fetch.assert_not_called()
            self.assertEqual(self.png, self.reader.open_locator(locator, allow_remote=True))
            fetch.assert_called_once_with(emoji_md5=self.md5, native_url=url, declared_size=len(self.png))
            self.assertEqual("remote", self.reader.resolve_locator(locator)["materializationSource"])

    def test_emoji_uses_its_message_url_when_native_catalog_has_no_row(self):
        url = "https://emoji.qpic.cn/synthetic-message-item"
        self.messages.execute(
            f"INSERT INTO {self.table} VALUES (3,103,47,?)",
            (f'<msg><emoji md5="{self.md5}" len="{len(self.png)}" cdnurl="{url}"/></msg>',),
        )
        with patch("wechat_media.fetch_emoji_media", return_value=LocalMedia(
            self.png, "emoji.png", "image/png", "original", source="remote"
        )) as fetch:
            self.assertEqual(self.png, self.reader.open_locator(self.locator("emoji", 3, 103), allow_remote=True))
            fetch.assert_called_once_with(emoji_md5=self.md5, native_url=url, declared_size=len(self.png))

    def test_monthly_emoji_cache_only_materializes_on_explicit_open_with_message_binding(self):
        self.messages.execute(f"INSERT INTO {self.table} VALUES (2,102,47,?)", (
            f'<msg><emoji md5="{self.md5}" len="{len(self.png)}"/></msg>',))
        locator = self.locator("emoji", 2, 102)
        with (
            patch.object(self.reader, "_emoticon_cache_key_provider", create=True, return_value=b"synthetic-key") as provider,
            patch("wechat_media.emoji_cache_paths", create=True, return_value=[self.root / "monthly-cache"]) as paths,
            patch("wechat_media.open_cached_emoji_media", create=True,
                  return_value=LocalMedia(self.png, "emoji.png", "image/png", "original")) as cached,
            patch("wechat_media.fetch_emoji_media") as fetch,
        ):
            pending = self.reader.resolve_locator(locator)
            self.assertTrue(pending["materializable"])
            self.assertTrue(pending["requiresLiveAccount"])
            self.assertFalse(pending["requiresNetwork"])
            self.assertEqual(pending["materializationSource"], "local")
            provider.assert_not_called()
            cached.assert_not_called()
            self.assertEqual(self.png, self.reader.open_locator(locator))
            provider.assert_called_once_with()
            paths.assert_called_with(self.root, self.md5)
            cached.assert_called_once_with(account_root=self.root, emoji_md5=self.md5,
                                           cache_key=b"synthetic-key", declared_size=len(self.png))
            fetch.assert_not_called()

    def test_monthly_emoji_key_failure_keeps_other_media_and_permitted_cdn_available(self):
        self.add_image()
        url = "https://emoji.qpic.cn/synthetic-cache-fallback"
        self.messages.execute(f"INSERT INTO {self.table} VALUES (2,102,47,?)", (
            f'<msg><emoji md5="{self.md5}" len="{len(self.png)}" cdnurl="{url}"/></msg>',))
        failure = ProductError("helper_emoticon_cache_key_unavailable", diagnosticCode="bound_account_not_logged_in")
        with (
            patch.object(self.reader, "_emoticon_cache_key_provider", create=True,
                         side_effect=failure) as provider,
            patch("wechat_media.emoji_cache_paths", create=True, return_value=[self.root / "monthly-cache"]),
            patch("wechat_media.open_cached_emoji_media", create=True),
            patch("wechat_media.fetch_emoji_media", return_value=LocalMedia(
                self.png, "emoji.png", "image/png", "original", source="remote")) as fetch,
        ):
            self.assertEqual(self.png, self.reader.open_locator(self.locator("image")))
            provider.assert_not_called()
            with self.assertRaises(ProductError) as caught:
                self.reader.open_locator(self.locator("emoji", 2, 102))
            self.assertIs(caught.exception, failure)
            fetch.assert_not_called()
            self.assertEqual(self.png, self.reader.open_locator(self.locator("emoji", 2, 102), allow_remote=True))
            fetch.assert_called_once_with(emoji_md5=self.md5, native_url=url, declared_size=len(self.png))

    def test_media_keys_use_only_the_current_identity_scoped_fields(self):
        path = self.root / "synthetic-config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "myWxid": "synthetic-primary", "imageAesKey": "fedcba9876543210", "imageXorKey": "0x7f",
            "wxidConfigs": {
                "synthetic-primary": {"imageAesKey": "0123456789abcdef", "imageXorKey": 0},
                "synthetic-other": {"imageAesKey": "not-for-this-id!", "imageXorKey": 20},
            },
        }), encoding="utf-8")
        with patch.object(wechat_source, "_safe_storage_key", return_value=b"synthetic-safe-key"):
            aes, xor = wechat_source.load_direct_media_keys(path, "unused-state", "synthetic-primary")
            self.assertEqual(b"0123456789abcdef", aes)
            self.assertEqual(0, xor)
            with self.assertRaises(DirectCredentialError):
                wechat_source.load_direct_media_keys(path, "unused-state", "synthetic-other")

    def packed_image(self):
        self.messages.execute(f"ALTER TABLE {self.table} ADD COLUMN packed_info_data BLOB")
        self.messages.execute(f"ALTER TABLE {self.table} ADD COLUMN create_time INTEGER")
        stamp = int(datetime(2026, 10, 31, 17, 12, tzinfo=timezone.utc).timestamp())
        packed = b"\x08\x02\x10\x0c\x1a\x22\x22\x20" + self.md5.encode()
        self.messages.execute(f"INSERT INTO {self.table} VALUES (1,101,3,?,?,?)",
            (f'<msg><img md5="{self.md5}" length="100" hdlength="200"/></msg>', packed, stamp))
        folder = self.root / "msg/attach" / self.table[4:] / "2026-10/Img"
        folder.mkdir(parents=True)
        return folder, self.locator("image")

    def test_image_protobuf_shapes_and_malformed_data(self):
        stem = self.md5.encode()
        for value in (b"\x08\x02\x10\x0c\x1a\x28\x08\xb4\x01\x10\xc0\x02\x22\x20" + stem,
                      b"\x1a\x22\x22\x20" + stem + b"\x58\x00",
                      b"\x08\x02\x10\x37\x1a\x22\x22\x20" + stem + b"\x58\x00"):
            self.assertEqual(wechat_source._native_image_stem(value), self.md5)
        for value in (None, b"\x08\x05\x10\x3e", b"\x1a\x22\x22\x20short", b"\x80" * 12,
                      b"\x1a\x22\x22\x20" + b"../" + b"a" * 29,
                      b"\x1a\x22\x22\x20" + stem + b"\x1a\x00"):
            self.assertIsNone(wechat_source._native_image_stem(value))

    def test_packed_image_quality_preference_and_context(self):
        folder, locator = self.packed_image()
        payload = self.reader._decode_locator(locator)
        for suffix, expected in (("_t.dat", "thumbnail"), (".dat", "standard"), ("_h.dat", "original")):
            (folder / (self.md5 + suffix)).write_bytes(self.png)
            self.assertEqual(self.reader._describe_native_media(payload)["quality"], expected)
            self.assertEqual(self.reader.open_locator(locator), self.png)
            self.assertEqual(self.reader.resolve_locator(locator)["quality"], expected)
        (folder / (self.md5 + "_h.dat")).unlink()
        for hd in (' hdlength="100"', ""):
            self.messages.execute(f"UPDATE {self.table} SET message_content=?",
                (f'<img md5="{self.md5}" length="100"{hd}/>',))
            self.reader.open_locator(locator)
            self.assertEqual(self.reader.resolve_locator(locator)["quality"], "original")

    def test_month_boundary_and_missing_image_recovery_hint(self):
        from wechat_cli import _failure_details
        folder, locator = self.packed_image()
        path = folder / (self.md5 + "_t.dat")
        november = folder.parent.parent / "2026-11/Img"
        november.mkdir(parents=True)
        (november / path.name).write_bytes(self.png)
        self.assertEqual(self.reader.open_locator(locator), self.png)
        (november / path.name).unlink()
        payload = self.reader._decode_locator(locator)
        self.assertEqual(self.reader._describe_native_media(payload)["resolution_gap"], "image_not_stored_locally")
        with self.assertRaisesRegex(MediaNotOpenableError, "image_not_stored_locally") as caught:
            self.reader.open_locator(locator)
        self.assertTrue(_failure_details(caught.exception)["retryable"])
        self.assertIn("open_this_chat", _failure_details(caught.exception)["nextAction"])

    def test_image_paths_cannot_cross_chat_or_account(self):
        folder, locator = self.packed_image()
        outside = self.root.parent / "escape.dat"
        outside.write_bytes(self.png)
        with patch.object(self.reader, "_hardlink_media_paths", return_value=[(outside, "original")]):
            with self.assertRaises(MediaNotOpenableError):
                self.reader.open_locator(locator)
        sibling = self.root / "msg/attach" / ("a" * 32) / "2026-10/Img/image.dat"
        sibling.parent.mkdir(parents=True)
        sibling.write_bytes(self.png)
        with patch.object(self.reader, "_hardlink_media_paths", return_value=[(sibling, "original")]):
            with self.assertRaises(MediaNotOpenableError):
                self.reader.open_locator(locator)

    def test_helper_standard_upgrades_thumbnail_but_keeps_original(self):
        folder, locator = self.packed_image()
        (folder / (self.md5 + "_t.dat")).write_bytes(self.png)
        helper = LocalMedia(self.png, "helper.png", "image/png", "standard", "native_helper_download")
        with patch.object(self.reader, "_helper_image_provider", create=True, return_value=helper):
            metadata = self.reader._describe_native_media(self.reader._decode_locator(locator))
            self.assertEqual(metadata["materializationSource"], "native_helper_download")
            self.reader.open_locator(locator)
            self.assertEqual(self.reader.resolve_locator(locator)["quality"], "standard")
            (folder / (self.md5 + "_h.dat")).write_bytes(self.png)
            self.reader.open_locator(locator)
            self.assertEqual(self.reader.resolve_locator(locator)["quality"], "original")


if __name__ == "__main__":
    unittest.main()
