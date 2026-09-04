from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import wechat_source
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
        self.hardlink.executemany("INSERT INTO dir2id VALUES (?)", [("synthetic-directory",), ("2026-09",)])
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
        path = self.root / "msg" / "attach" / "synthetic-directory" / "2026-09" / "Img" / (self.md5 + ".dat")
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
                path = self.root / "msg" / kind / "synthetic-directory" / "2026-09" / "synthetic.bin"
                path.parent.mkdir(parents=True)
                path.write_bytes(b"different bytes at the correct native path")
                locator = self.locator(kind, local_id, local_id + 100)
                with self.assertRaises(MediaNotOpenableError):
                    self.reader.open_locator(locator)
                path.write_bytes(self.png)
                self.assertEqual(self.png, self.reader.open_locator(locator))

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


if __name__ == "__main__":
    unittest.main()
