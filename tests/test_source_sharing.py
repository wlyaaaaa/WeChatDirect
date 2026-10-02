"""Windows file-sharing regression checks using synthetic databases only."""

from __future__ import annotations

import isolation  # noqa: F401

from contextlib import closing
import io
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import wechat_storage
import wechat_source
from wechat_source import DirectWeChatReader, EncryptedPageCodec, PAGE_SIZE


@unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
class SourceSharingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_read_only_handle_allows_writer_and_rotation(self):
        source = self.root / "source.db"
        replacement = self.root / "next.db"
        source.write_bytes(b"old")
        replacement.write_bytes(b"new")
        with wechat_storage.open_source_read(source) as stream:
            self.assertFalse(stream.writable())
            with self.assertRaises(io.UnsupportedOperation):
                stream.write(b"x")
            with source.open("r+b") as writer:
                writer.write(b"OLD")
            # Windows can reject replacing an open destination even with
            # delete sharing. Moving the old generation aside is supported.
            source.rename(self.root / "previous.db")
            replacement.rename(source)
            self.assertEqual(stream.read(), b"OLD")
        self.assertEqual(source.read_bytes(), b"new")

    def test_missing_source_is_not_created(self):
        source = self.root / "missing.db"
        with self.assertRaises(FileNotFoundError):
            wechat_storage.open_source_read(source)
        self.assertFalse(source.exists())

    def test_decryption_does_not_hold_source_against_rename(self):
        source = self.root / "encrypted.db"
        moved = self.root / "rotated.db"
        destination = self.root / "copy.db"
        source.write_bytes(b"A" * PAGE_SIZE)
        codec = EncryptedPageCodec(b"K" * 32, b"S" * 16)

        def decode(_number, _page):
            source.rename(moved)
            return b"B" * PAGE_SIZE

        with patch.object(EncryptedPageCodec, "decrypt_page", side_effect=decode):
            codec.decrypt_database(source, destination)
        self.assertEqual(destination.read_bytes(), b"B" * PAGE_SIZE)
        self.assertEqual(moved.read_bytes(), b"A" * PAGE_SIZE)

    def test_snapshot_retries_replaced_file_with_same_size_and_mtime(self):
        source = self.root / "source.db"
        replacement = self.root / "next.db"
        for path, value in ((source, 1), (replacement, 2)):
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE sample(value INTEGER)")
                connection.execute("INSERT INTO sample VALUES (?)", (value,))
                connection.commit()
        original = source.stat()
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(source.stat().st_size, replacement.stat().st_size)
        reader = DirectWeChatReader.__new__(DirectWeChatReader)
        reader._storage = self.root
        reader._temporary = SimpleNamespace(name=str(self.root / "snapshot"))
        Path(reader._temporary.name).mkdir()
        reader._prepared = {}
        copy = wechat_source.copy_plain_snapshot
        replaced = False

        def rotate_after_copying(*args, **kwargs):
            nonlocal replaced
            result = copy(*args, **kwargs)
            if not replaced:
                replacement.replace(source)
                replaced = True
            return result

        with patch.object(wechat_source, "copy_plain_snapshot", rotate_after_copying):
            snapshot = reader._prepare(source)
        self.assertTrue(replaced)
        with closing(sqlite3.connect(snapshot)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM sample").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
