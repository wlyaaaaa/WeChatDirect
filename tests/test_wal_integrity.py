"""Synthetic SQLCipher pages and real SQLite WALs; no account data required."""

from __future__ import annotations
import hashlib
import hmac
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from wechat_source import (
    EncryptedPageCodec,
    SnapshotCopyError,
    DirectWeChatReader,
    _committed_wal_page_offsets,
)

SIZE = 4096
KEY = b"K" * 32
SALT = b"S" * 16
GEN = b"G" * 8


def checksum(data, state=(0, 0), endian="<"):
    a, b = state
    words = struct.unpack(endian + str(len(data) // 4) + "I", data)
    for i in range(0, len(words), 2):
        a = (a + words[i] + b) & 0xFFFFFFFF
        b = (b + words[i + 1] + a) & 0xFFFFFFFF
    return a, b


def page(marker=0):
    p = bytearray(SIZE)
    p[:16] = b"SQLite format 3\0"
    p[16:18] = struct.pack(">H", SIZE)
    p[18:24] = bytes((1, 1, 80, 64, 32, 32))
    p[28:32] = struct.pack(">I", 1)
    p[100] = 13
    p[105:107] = struct.pack(">H", SIZE - 80)
    p[200] = marker
    return bytes(p)


def encrypted(p):
    iv = b"I" * 16
    enc = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
    body = SALT + enc.update(p[16 : SIZE - 80]) + enc.finalize() + iv
    mac_key = hashlib.pbkdf2_hmac("sha512", KEY, bytes(x ^ 0x3A for x in SALT), 2, 32)
    return (
        body
        + hmac.new(mac_key, body[16:] + struct.pack("<I", 1), hashlib.sha512).digest()
    )


def wal(frames, *, magic=0x377F0682, version=3007000, declared=SIZE):
    end = ">" if magic & 1 else "<"
    header = struct.pack(">IIII8s", magic, version, declared, 0, GEN)
    sums = checksum(header, endian=end)
    out = header + struct.pack(">II", *sums)
    for payload, commit, generation in frames:
        first = struct.pack(">II", 1, commit)
        sums = checksum(first + payload, sums, end)
        out += first + generation + struct.pack(">II", *sums) + payload
    return out


class WalIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "copy.db"
        self.wal = self.root / "source.db-wal"
        self.db.write_bytes(page())
        self.codec = EncryptedPageCodec(
            KEY, SALT, key_derivation="raw", hmac_mode="sqlcipher"
        )

    def test_roundtrip_sqlcipher_page_and_committed_frame(self):
        payload = encrypted(page(7))
        self.assertTrue(self.codec.verify_page_hmac(1, payload))
        self.assertEqual(self.codec.decrypt_page(1, payload), page(7))
        self.wal.write_bytes(wal([(payload, 1, GEN)]))
        before = self.wal.read_bytes()
        self.assertEqual(self.codec.merge_wal(self.db, self.wal), 1)
        self.assertEqual(self.db.read_bytes(), page(7))
        self.assertEqual(self.wal.read_bytes(), before)

    def test_big_endian_checksum_is_supported(self):
        self.wal.write_bytes(wal([(encrypted(page(4)), 1, GEN)], magic=0x377F0683))
        self.assertEqual(self.codec.merge_wal(self.db, self.wal), 1)
        self.assertEqual(self.db.read_bytes(), page(4))

    def test_invalid_header_is_rejected_by_both_readers(self):
        for kwargs in ({"magic": 123}, {"version": 999}, {"declared": 1024}):
            with self.subTest(kwargs=kwargs):
                self.wal.write_bytes(wal([(encrypted(page(9)), 1, GEN)], **kwargs))
                with self.assertRaises(SnapshotCopyError):
                    self.codec.merge_wal(self.db, self.wal)
                with self.assertRaises(SnapshotCopyError):
                    _committed_wal_page_offsets(
                        self.wal, base_page_count=1, page_size=SIZE
                    )

    def test_header_checksum_is_required(self):
        data = bytearray(wal([(encrypted(page(9)), 1, GEN)]))
        data[24] ^= 1
        self.wal.write_bytes(data)
        with self.assertRaises(SnapshotCopyError):
            self.codec.merge_wal(self.db, self.wal)
        with self.assertRaises(SnapshotCopyError):
            _committed_wal_page_offsets(self.wal, base_page_count=1, page_size=SIZE)

    def test_generation_gap_never_resumes(self):
        self.wal.write_bytes(
            wal([(encrypted(page(3)), 0, b"X" * 8), (encrypted(page(9)), 1, GEN)])
        )
        self.assertEqual(self.codec.merge_wal(self.db, self.wal), 0)
        self.assertEqual(self.db.read_bytes(), page())
        self.assertEqual(
            _committed_wal_page_offsets(self.wal, base_page_count=1, page_size=SIZE),
            ({}, None),
        )

    def test_checksum_failure_stops_at_last_commit(self):
        data = bytearray(
            wal(
                [
                    (encrypted(page(3)), 1, GEN),
                    (encrypted(page(9)), 1, GEN),
                    (encrypted(page(10)), 1, GEN),
                ]
            )
        )
        data[32 + (SIZE + 24) + 16] ^= 1
        self.wal.write_bytes(data)
        self.assertEqual(self.codec.merge_wal(self.db, self.wal), 1)
        offsets, pages = _committed_wal_page_offsets(
            self.wal, base_page_count=1, page_size=SIZE
        )
        self.assertEqual((offsets, pages), ({1: 56}, 1))
        self.assertEqual(self.db.read_bytes(), page(3))

    def test_uncommitted_and_torn_tail_is_not_applied(self):
        data = (
            wal([(encrypted(page(3)), 1, GEN), (encrypted(page(9)), 0, GEN)])
            + b"partial"
        )
        self.wal.write_bytes(data)
        self.assertEqual(self.codec.merge_wal(self.db, self.wal), 1)
        self.assertEqual(self.db.read_bytes(), page(3))

    def test_from_frame_cannot_bypass_invalid_prefix(self):
        data = bytearray(
            wal([(encrypted(page(3)), 1, GEN), (encrypted(page(9)), 1, GEN)])
        )
        data[48] ^= 1
        self.wal.write_bytes(data)
        with self.assertRaises(SnapshotCopyError):
            self.codec.merge_wal(self.db, self.wal, from_frame=1)

    def test_decrypt_cannot_replace_source(self):
        self.db.write_bytes(encrypted(page(3)))
        before = self.db.read_bytes()
        with self.assertRaises(SnapshotCopyError):
            self.codec.decrypt_database(self.db, self.db)
        self.assertEqual(self.db.read_bytes(), before)


class PlainSnapshotTests(unittest.TestCase):
    def test_plain_sqlite_snapshot_includes_committed_wal_without_source_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "db_storage"
            storage.mkdir()
            source = storage / "message_0.db"
            conn = sqlite3.connect(source)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA wal_autocheckpoint=0")
                conn.execute("CREATE TABLE items(value TEXT)")
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("INSERT INTO items VALUES('only in WAL')")
                conn.commit()
                before = {p.name: p.read_bytes() for p in storage.iterdir()}
                reader = object.__new__(DirectWeChatReader)
                reader._storage = storage
                reader._prepared = {}
                reader._connections = {}
                snapshots = root / "snapshots"
                snapshots.mkdir()
                reader._temporary = SimpleNamespace(name=str(snapshots))
                copied = reader._prepare(source)
                check = sqlite3.connect(copied)
                try:
                    self.assertEqual(
                        check.execute("SELECT value FROM items").fetchall(),
                        [("only in WAL",)],
                    )
                finally:
                    check.close()
                self.assertEqual(
                    {p.name: p.read_bytes() for p in storage.iterdir()}, before
                )
            finally:
                conn.close()
