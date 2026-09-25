"""Synthetic SQLCipher pages and real SQLite WALs; no account data required."""

from __future__ import annotations

import isolation  # noqa: F401

import hashlib
import hmac
import sqlite3
import os
import struct
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from wechat_source import (
    EncryptedPageCodec,
    SnapshotCopyError,
    DirectWeChatReader,
    _committed_wal_page_offsets,
    derive_page_key,
)
from wechat_wal import checksum as wal_checksum

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


class EncryptedSnapshotTests(unittest.TestCase):
    def test_complete_encrypted_database_and_wal_reach_message_fetch(self):
        """Exercise the real decrypt, WAL merge, quick_check and SQL selector."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "db_storage" / "message"
            storage.mkdir(parents=True)
            source = storage / "message_0.db"
            session = "synthetic-contact"
            table = "Msg_" + hashlib.md5(
                session.encode(), usedforsecurity=False
            ).hexdigest()
            connection = sqlite3.connect(source)
            try:
                connection.execute("PRAGMA page_size=4096")
                connection.execute(
                    f"CREATE TABLE {table}(local_id INTEGER, local_type INTEGER, "
                    "server_id INTEGER, real_sender_id INTEGER, create_time INTEGER, "
                    "message_content TEXT, source TEXT, packed_info_data TEXT, "
                    "compress_content TEXT, sort_seq INTEGER, status INTEGER, "
                    "origin_source TEXT)"
                )
                connection.execute(f"CREATE INDEX msg_time ON {table}(create_time)")
                connection.execute(
                    f"INSERT INTO {table} VALUES(1, 1, 1, 1, 100, "
                    "'base', '', '', '', 1, 4, '')"
                )
                connection.commit()
            finally:
                connection.close()

            # SQLite's bundled API cannot set reserve_size directly. Setting
            # the header then VACUUM rebuilds cells with SQLCipher's 80-byte
            # reserve, so encryption does not discard live page data.
            base = bytearray(source.read_bytes())
            base[20] = 80
            source.write_bytes(base)
            connection = sqlite3.connect(source)
            try:
                connection.execute("VACUUM")
                self.assertEqual(source.read_bytes()[20], 80)
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute(
                    f"INSERT INTO {table} VALUES(2, 1, 2, 1, 200, "
                    "'wal-only', '', '', '', 2, 4, '')"
                )
                connection.commit()
                plain_base = source.read_bytes()
                plain_wal = source.with_name(source.name + "-wal").read_bytes()
            finally:
                connection.close()

            salt = b"S" * 16
            master = "4b" * 32
            key = derive_page_key(master, salt)
            mac_key = hashlib.pbkdf2_hmac(
                "sha512", key, bytes(value ^ 0x3A for value in salt), 2, 32
            )

            def seal(number, plain):
                iv = number.to_bytes(16, "big")
                start = 16 if number == 1 else 0
                enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
                body = enc.update(plain[start : SIZE - 80]) + enc.finalize()
                prefix = salt if number == 1 else b""
                data = prefix + body + iv
                mac_input = data[16:] if number == 1 else data
                return data + hmac.new(
                    mac_key,
                    mac_input + struct.pack("<I", number),
                    hashlib.sha512,
                ).digest()

            source.write_bytes(
                b"".join(
                    seal(number + 1, plain_base[number * SIZE : (number + 1) * SIZE])
                    for number in range(len(plain_base) // SIZE)
                )
            )
            magic = struct.unpack(">I", plain_wal[:4])[0]
            big = bool(magic & 1)
            rolling = wal_checksum(plain_wal[:24], big_endian=big)
            encrypted_wal = bytearray(plain_wal[:32])
            for offset in range(32, len(plain_wal), 24 + SIZE):
                header = plain_wal[offset : offset + 24]
                plain_page = plain_wal[offset + 24 : offset + 24 + SIZE]
                if len(header) != 24 or len(plain_page) != SIZE:
                    break
                number = struct.unpack(">I", header[:4])[0]
                payload = seal(number, plain_page)
                rolling = wal_checksum(
                    payload,
                    wal_checksum(header[:8], rolling, big_endian=big),
                    big_endian=big,
                )
                encrypted_wal.extend(header[:16] + struct.pack(">II", *rolling))
                encrypted_wal.extend(payload)
            wal_path = source.with_name(source.name + "-wal")
            wal_path.write_bytes(encrypted_wal)

            # Construct through the real initializer; only the protected
            # credential carrier is replaced by the synthetic master key.
            with (
                mock.patch(
                    "wechat_source.load_direct_source_identity",
                    return_value=(root, master, "synthetic-self"),
                ),
                mock.patch.dict(os.environ, {"WECHAT_DIRECT_TEMP_ROOT": str(root)}),
            ):
                reader = DirectWeChatReader(
                    config_path=root / "unused-config",
                    local_state_path=root / "unused-state",
                    snapshot_cutoff_s=1000,
                )
            try:
                copied = reader._prepare(source)
                self.assertTrue(copied.is_relative_to(root / "wechat-direct-scratch"))
                check = sqlite3.connect(copied)
                try:
                    self.assertEqual(
                        check.execute("PRAGMA quick_check").fetchone()[0], "ok"
                    )
                finally:
                    check.close()
                reader._message_connections = lambda _table=None: [
                    (source, reader._open(source))
                ]
                reader._session_is_registered = lambda _session: True
                reader._message_from_row = lambda **kwargs: {
                    "content": kwargs["row"]["message_content"]
                }
                result = reader.fetch_messages(
                    session, since_s=None, end_s=300, limit=None
                )
                self.assertEqual(
                    [item["content"] for item in result["messages"]],
                    ["base", "wal-only"],
                )
                self.assertNotEqual(source.read_bytes()[:16], b"SQLite format 3\0")
            finally:
                reader.close()
