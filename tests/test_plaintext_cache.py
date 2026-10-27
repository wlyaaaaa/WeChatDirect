"""Persistent plaintext snapshot cache, with synthetic encrypted sources only."""

from __future__ import annotations

import isolation  # noqa: F401

from contextlib import closing
import gc
import hashlib
import hmac
import json
import os
from pathlib import Path
import random
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import wechat_cli
import wechat_plaincache
from wechat_plaincache import PlaintextCacheEntry, resolve_cache_root
from wechat_source import (
    DirectWeChatReader,
    EncryptedPageCodec,
    EncryptedPageError,
    SnapshotCopyError,
    _file_signature,
    derive_page_key,
)
from wechat_wal import checksum as wal_checksum

SIZE = 4096
MASTER = "4b" * 32
SALT = b"S" * 16
GENERATION = b"G" * 8


class Sealer:
    """SQLCipher-style page sealing with a fresh IV for every page write."""

    def __init__(self, master: str = MASTER, salt: bytes = SALT, seed: int = 1):
        self.salt = salt
        self.key = derive_page_key(master, salt)
        self.mac_key = hashlib.pbkdf2_hmac(
            "sha512", self.key, bytes(value ^ 0x3A for value in salt), 2, 32
        )
        self.rng = random.Random(seed)

    def seal(self, number: int, plain: bytes) -> bytes:
        iv = self.rng.randbytes(16)
        start = 16 if number == 1 else 0
        encryptor = Cipher(algorithms.AES(self.key), modes.CBC(iv)).encryptor()
        body = encryptor.update(plain[start : SIZE - 80]) + encryptor.finalize()
        data = (self.salt if number == 1 else b"") + body + iv
        mac_input = data[16:] if number == 1 else data
        return data + hmac.new(
            self.mac_key, mac_input + struct.pack("<I", number), hashlib.sha512
        ).digest()


class EncryptedSource:
    """A synthetic encrypted main database and WAL, rewritten like a writer.

    Unchanged plaintext pages keep their exact ciphertext; changed pages get a
    new IV. Every write moves the file signature deterministically.
    """

    clock = 1_700_000_000 * 1_000_000_000

    def __init__(self, path: Path, sealer: Sealer):
        self.path = path
        self.wal = path.with_name(path.name + "-wal")
        self.sealer = sealer
        self.plain: list[bytes] = []
        self.sealed: list[bytes] = []

    def _touch(self, path: Path) -> None:
        EncryptedSource.clock += 1_000_000_000
        os.utime(path, ns=(EncryptedSource.clock, EncryptedSource.clock))

    def write_main(self, pages: list[bytes]) -> None:
        sealed = []
        for index, page in enumerate(pages):
            if index < len(self.plain) and self.plain[index] == page:
                sealed.append(self.sealed[index])
            else:
                sealed.append(self.sealer.seal(index + 1, page))
        self.plain, self.sealed = list(pages), sealed
        self.path.write_bytes(b"".join(sealed))
        self._touch(self.path)

    def reseal(self, number: int) -> None:
        """Rewrite one page with identical plaintext and a new IV."""
        self.sealed[number - 1] = self.sealer.seal(number, self.plain[number - 1])
        self.path.write_bytes(b"".join(self.sealed))
        self._touch(self.path)

    def write_wal(
        self, frames: list[tuple[int, bytes, int]] | None, generation=GENERATION
    ) -> None:
        """Frames are (page number, plaintext, commit size or 0)."""
        if frames is None:
            self.wal.unlink(missing_ok=True)
            return
        header = struct.pack(">IIII8s", 0x377F0682, 3007000, SIZE, 0, generation)
        sums = wal_checksum(header)
        out = bytearray(header + struct.pack(">II", *sums))
        for number, plain, commit in frames:
            payload = self.sealer.seal(number, plain)
            first = struct.pack(">II", number, commit)
            sums = wal_checksum(payload, wal_checksum(first, sums))
            out += first + generation + struct.pack(">II", *sums) + payload
        self.wal.write_bytes(bytes(out))
        self._touch(self.wal)

    def write_plain_wal(self, plain_wal: bytes | None) -> None:
        """Encrypt a real SQLite WAL frame by frame, re-chaining checksums."""
        if not plain_wal:
            if plain_wal is None:
                self.wal.unlink(missing_ok=True)
            else:
                self.wal.write_bytes(b"")
                self._touch(self.wal)
            return
        big = bool(struct.unpack(">I", plain_wal[:4])[0] & 1)
        rolling = wal_checksum(plain_wal[:24], big_endian=big)
        out = bytearray(plain_wal[:32])
        for offset in range(32, len(plain_wal), 24 + SIZE):
            header = plain_wal[offset : offset + 24]
            page = plain_wal[offset + 24 : offset + 24 + SIZE]
            if len(header) != 24 or len(page) != SIZE:
                break
            payload = self.sealer.seal(struct.unpack(">I", header[:4])[0], page)
            rolling = wal_checksum(
                payload, wal_checksum(header[:8], rolling, big_endian=big), big_endian=big
            )
            out += header[:16] + struct.pack(">II", *rolling) + payload
        self.wal.write_bytes(bytes(out))
        self._touch(self.wal)


def make_codec(master: str = MASTER, salt: bytes = SALT) -> EncryptedPageCodec:
    return EncryptedPageCodec(
        master_hex=master, salt=salt, hmac_mode="sqlcipher", plaintext_header=False
    )


def fresh_snapshot(codec: EncryptedPageCodec, source: Path, work: Path) -> bytes:
    """The scratch path's result: full decrypt, then merge committed WAL."""
    target = work / f"fresh-{time.perf_counter_ns()}.sqlite3"
    codec.decrypt_database(source, target)
    wal = source.with_name(source.name + "-wal")
    if wal.is_file():
        codec.merge_wal(target, wal)
    data = target.read_bytes()
    target.unlink()
    return data


def random_pages(rng: random.Random, count: int) -> list[bytes]:
    return [rng.randbytes(SIZE) for _ in range(count)]


class CacheEntryRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "work").mkdir()
        self.rng = random.Random(42)
        self.sealer = Sealer()
        self.source = EncryptedSource(self.root / "message_0.db", self.sealer)
        self.codec = make_codec()
        self.entry = PlaintextCacheEntry(self.root / "cache", "a" * 64, "message/message_0.db")
        self.assertTrue(self.entry.acquire())
        self.addCleanup(self.entry.release)

    def refresh(self, codec=None):
        codec = codec or self.codec
        outcome = self.entry.refresh(
            codec, self.source.path, self.source.wal, _file_signature(self.source.path)
        )
        self.assertEqual(self.entry.read_metadata()["state"], "dirty")
        self.entry.commit(outcome, check_pending=False)
        return outcome

    def assertMatchesFresh(self, codec=None):
        expected = fresh_snapshot(codec or self.codec, self.source.path, self.root / "work")
        self.assertEqual(self.entry.plain.read_bytes(), expected)
        self.assertEqual(
            self.entry.reserves.read_bytes(),
            b"".join(page[SIZE - 80 :] for page in self.source.sealed),
        )
        metadata = self.entry.read_metadata()
        self.assertEqual(metadata["state"], "clean")
        self.assertEqual(metadata["mainPages"], len(self.source.sealed))
        self.assertEqual(metadata["walFilePages"] * SIZE, len(expected))
        self.assertNotIn(MASTER, json.dumps(metadata))

    def test_first_build_matches_scratch_decrypt_and_wal(self):
        pages = random_pages(self.rng, 40)
        self.source.write_main(pages)
        self.source.write_wal(
            [(2, self.rng.randbytes(SIZE), 0), (7, self.rng.randbytes(SIZE), 40)]
        )
        outcome = self.refresh()
        self.assertTrue(outcome.rebuilt)
        self.assertTrue(outcome.needs_check)
        self.assertMatchesFresh()
        self.assertEqual(self.entry.read_metadata()["walPages"], [[1, 2], [7, 7]])

    def test_reencrypted_pages_are_the_only_ones_decrypted(self):
        pages = random_pages(self.rng, 50)
        self.source.write_main(pages)
        self.refresh()
        for number in (3, 17, 50):
            pages[number - 1] = self.rng.randbytes(SIZE)
        self.source.write_main(pages)
        self.source.reseal(30)  # same plaintext, new IV
        with mock.patch.object(
            EncryptedPageCodec, "decrypt_database", side_effect=AssertionError()
        ):
            outcome = self.refresh()
        self.assertFalse(outcome.rebuilt)
        self.assertTrue(outcome.needs_check)
        self.assertEqual(outcome.decrypted_pages, 4)
        self.assertMatchesFresh()
        # Only the signature moved: the page compare finds nothing to do.
        os.utime(self.source.path, ns=(1, 1))
        outcome = self.refresh()
        self.assertEqual(outcome.decrypted_pages, 0)
        self.assertFalse(outcome.needs_check)
        self.assertMatchesFresh()

    def test_main_database_grows_and_shrinks(self):
        pages = random_pages(self.rng, 30)
        self.source.write_main(pages)
        self.refresh()
        pages += random_pages(self.rng, 4100)  # crosses compare chunks
        pages[5] = self.rng.randbytes(SIZE)
        self.source.write_main(pages)
        outcome = self.refresh()
        self.assertFalse(outcome.rebuilt)
        self.assertEqual(outcome.decrypted_pages, 4101)
        self.assertMatchesFresh()
        pages = pages[:12]
        pages[0] = self.rng.randbytes(SIZE)
        self.source.write_main(pages)
        outcome = self.refresh()
        self.assertFalse(outcome.rebuilt)
        self.assertEqual(outcome.decrypted_pages, 1)
        self.assertMatchesFresh()

    def test_replaced_or_cleared_wal_restores_overlaid_pages(self):
        pages = random_pages(self.rng, 20)
        self.source.write_main(pages)
        self.source.write_wal(
            [
                (2, self.rng.randbytes(SIZE), 0),
                (5, self.rng.randbytes(SIZE), 0),
                (1, self.rng.randbytes(SIZE), 20),
                (9, self.rng.randbytes(SIZE), 0),  # uncommitted tail
            ]
        )
        self.refresh()
        self.assertMatchesFresh()
        self.source.write_wal(
            [(7, self.rng.randbytes(SIZE), 0), (11, self.rng.randbytes(SIZE), 20)],
            generation=b"H" * 8,
        )
        outcome = self.refresh()
        self.assertFalse(outcome.rebuilt)
        self.assertFalse(outcome.needs_check)
        self.assertMatchesFresh()
        self.source.wal.write_bytes(b"")
        self.refresh()
        self.assertMatchesFresh()
        self.assertEqual(self.entry.read_metadata()["walPages"], [])
        self.source.write_wal([(4, self.rng.randbytes(SIZE), 20)])
        self.refresh()
        self.assertMatchesFresh()
        self.source.write_wal(None)
        self.refresh()
        self.assertMatchesFresh()

    def test_wal_extends_then_truncates_the_database(self):
        pages = random_pages(self.rng, 30)
        self.source.write_main(pages)
        self.refresh()
        self.source.write_wal(
            [(number, self.rng.randbytes(SIZE), 0) for number in range(31, 35)]
            + [(35, self.rng.randbytes(SIZE), 35)]
        )
        self.refresh()
        self.assertMatchesFresh()
        self.assertEqual(self.entry.read_metadata()["walFilePages"], 35)
        self.source.write_wal(
            [(3, self.rng.randbytes(SIZE), 12)], generation=b"H" * 8
        )
        self.refresh()
        self.assertMatchesFresh()
        self.assertEqual(self.entry.read_metadata()["walFilePages"], 12)
        self.source.write_wal(None)  # main unchanged: the cut tail is restored
        outcome = self.refresh()
        self.assertEqual(outcome.decrypted_pages, 20)  # pages 1, 3 and 13-30
        self.assertMatchesFresh()
        self.source.write_wal(
            [(3, self.rng.randbytes(SIZE), 12)], generation=b"J" * 8
        )
        self.refresh()
        self.assertMatchesFresh()
        # Main changes while the restored tail is also needed.
        pages[20] = self.rng.randbytes(SIZE)
        pages.append(self.rng.randbytes(SIZE))
        self.source.write_main(pages)
        self.source.write_wal(None)
        outcome = self.refresh()
        self.assertFalse(outcome.rebuilt)
        self.assertMatchesFresh()

    def test_checkpointed_overlay_page_escalates_to_full_compare(self):
        pages = random_pages(self.rng, 25)
        self.source.write_main(pages)
        page_two = self.rng.randbytes(SIZE)
        self.source.write_wal([(2, page_two, 25)])
        self.refresh()
        signature = os.stat(self.source.path)
        pages[1] = page_two
        pages[18] = self.rng.randbytes(SIZE)
        self.source.write_main(pages)
        self.source.write_wal(None)
        # Same size, identity and write time: only reserves reveal the change.
        os.utime(self.source.path, ns=(signature.st_atime_ns, signature.st_mtime_ns))
        outcome = self.refresh()
        self.assertTrue(outcome.needs_check)
        self.assertMatchesFresh()

    def test_new_wal_header_forces_compare_despite_same_main_signature(self):
        pages = random_pages(self.rng, 25)
        self.source.write_main(pages)
        self.refresh()  # no WAL, nothing overlaid
        signature = os.stat(self.source.path)
        # Between refreshes: a page went through the WAL, was checkpointed
        # into main, and the WAL restarted with other frames.
        pages[9] = self.rng.randbytes(SIZE)
        self.source.write_main(pages)
        os.utime(self.source.path, ns=(signature.st_atime_ns, signature.st_mtime_ns))
        self.source.write_wal([(5, self.rng.randbytes(SIZE), 25)], generation=b"H" * 8)
        outcome = self.refresh()
        self.assertTrue(outcome.needs_check)
        self.assertMatchesFresh()
        # Same WAL generation and main: only restores, no full compare.
        with mock.patch.object(
            PlaintextCacheEntry, "_compare_pass", side_effect=AssertionError()
        ):
            outcome = self.refresh()
        self.assertFalse(outcome.needs_check)
        self.assertMatchesFresh()

    def test_dirty_mark_is_written_in_place(self):
        self.source.write_main(random_pages(self.rng, 3))
        self.refresh()
        identity = os.stat(self.entry.metadata_path).st_ino
        self.entry.mark_dirty()
        self.assertEqual(os.stat(self.entry.metadata_path).st_ino, identity)
        self.assertEqual(self.entry.read_metadata()["state"], "dirty")

    def test_dropped_entry_frees_lock_and_registration(self):
        other = PlaintextCacheEntry(self.root / "cache", "b" * 64, "x.db")
        self.assertTrue(other.acquire(timeout=0))
        lock = other.lock_path
        del other
        gc.collect()
        again = PlaintextCacheEntry(self.root / "cache", "b" * 64, "x.db")
        self.assertEqual(again.lock_path, lock)
        self.assertTrue(again.acquire(timeout=0))
        again.release()
        again.release()

    def test_dirty_salt_key_and_missing_files_force_full_rebuild(self):
        pages = random_pages(self.rng, 10)
        self.source.write_main(pages)
        self.refresh()
        self.entry.mark_dirty()
        self.assertTrue(self.refresh().rebuilt)
        self.assertMatchesFresh()

        with self.entry.plain.open("ab") as stream:
            stream.write(b"\0" * SIZE)
        self.assertTrue(self.refresh().rebuilt)
        self.entry.plain.unlink()
        self.assertTrue(self.refresh().rebuilt)
        self.assertMatchesFresh()

        salted = Sealer(salt=b"T" * 16, seed=2)
        self.source.sealer, self.source.plain = salted, []
        self.source.write_main(pages)
        codec = make_codec(salt=b"T" * 16)
        self.assertTrue(self.refresh(codec).rebuilt)
        self.assertMatchesFresh(codec)

        master = "5c" * 32
        self.source.sealer, self.source.plain = Sealer(master, b"T" * 16, 3), []
        self.source.write_main(pages)
        codec = make_codec(master, b"T" * 16)
        self.assertTrue(self.refresh(codec).rebuilt)
        self.assertMatchesFresh(codec)
        self.assertNotIn(master, self.entry.metadata_path.read_text("utf-8"))

    def test_changed_page_failing_hmac_raises_and_stays_dirty(self):
        pages = random_pages(self.rng, 12)
        self.source.write_main(pages)
        self.refresh()
        corrupt = bytearray(self.sealer.seal(6, self.rng.randbytes(SIZE)))
        corrupt[100] ^= 1
        self.source.sealed[5] = bytes(corrupt)
        self.source.path.write_bytes(b"".join(self.source.sealed))
        os.utime(self.source.path, ns=(5, 5))
        with self.assertRaisesRegex(EncryptedPageError, "authentication failed"):
            self.entry.refresh(
                self.codec, self.source.path, self.source.wal,
                _file_signature(self.source.path),
            )
        self.assertEqual(self.entry.read_metadata()["state"], "dirty")

    def test_metadata_writes_need_the_lock(self):
        other = PlaintextCacheEntry(self.root / "cache", "a" * 64, "message/message_0.db")
        self.assertFalse(other.acquire(timeout=0))
        with self.assertRaises(wechat_plaincache.PlaintextCacheError):
            other.mark_dirty()


def build_wal_database(path: Path) -> sqlite3.Connection:
    """A real SQLite database with SQLCipher's 80-byte reserve, in WAL mode.

    The bundled API cannot set the reserve, so it is written into the header
    of a database whose only page has no b-tree cells yet (moving that page's
    empty cell area to the usable end); later pages honour it.
    """
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA page_size=4096")
        connection.execute("PRAGMA user_version=1")
    data = bytearray(path.read_bytes())
    assert len(data) == SIZE
    data[20] = 80
    data[105:107] = struct.pack(">H", SIZE - 80)
    path.write_bytes(data)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        return _populate(connection)
    except BaseException:
        connection.close()
        raise


def _populate(connection: sqlite3.Connection) -> sqlite3.Connection:
    connection.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, body TEXT)")
    connection.execute("CREATE INDEX items_body ON items(body)")
    connection.execute("BEGIN")
    connection.executemany(
        "INSERT INTO items(body) VALUES(?)",
        [(f"base-{index:05d}-" + "x" * 300,) for index in range(400)],
    )
    connection.execute("COMMIT")
    for pragma in ("journal_mode=WAL", "wal_autocheckpoint=0", "wal_checkpoint(TRUNCATE)"):
        connection.execute(f"PRAGMA {pragma}").fetchall()
    return connection


class ReaderCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = self.root / "db_storage" / "message"
        self.storage.mkdir(parents=True)
        self.cache_root = self.root / "cache"
        plain_dir = self.root / "plain"
        plain_dir.mkdir()
        self.plain = plain_dir / "plain.db"
        self.connection = build_wal_database(self.plain)
        self.addCleanup(self.connection.close)
        self.source = EncryptedSource(self.storage / "message_0.db", Sealer())
        self.next_id = 1000
        self.publish()

    def publish(self, *, checkpoint: str | None = None, inserts: int = 0) -> None:
        for _ in range(inserts):
            self.next_id += 1
            self.connection.execute(
                "INSERT INTO items(id, body) VALUES(?, ?)",
                (self.next_id, f"new-{self.next_id}-" + "y" * 200),
            )
        if checkpoint:
            busy = self.connection.execute(
                f"PRAGMA wal_checkpoint({checkpoint})"
            ).fetchone()[0]
            self.assertEqual(busy, 0)
        main = self.plain.read_bytes()
        self.source.write_main(
            [main[offset : offset + SIZE] for offset in range(0, len(main), SIZE)]
        )
        wal = self.plain.with_name(self.plain.name + "-wal")
        self.source.write_plain_wal(wal.read_bytes() if wal.exists() else None)

    def reader(self, environment: dict[str, str] | None = None) -> DirectWeChatReader:
        environment = (
            {"WECHAT_DIRECT_CACHE_ROOT": str(self.cache_root)}
            if environment is None
            else environment
        )
        with (
            mock.patch(
                "wechat_source.load_direct_source_identity",
                return_value=(self.root, MASTER, "synthetic-self"),
            ),
            mock.patch.dict(
                os.environ, {"WECHAT_DIRECT_TEMP_ROOT": str(self.root), **environment}
            ),
        ):
            reader = DirectWeChatReader(
                config_path=self.root / "unused-config",
                local_state_path=self.root / "unused-state",
                snapshot_cutoff_s=1000,
            )
        self.addCleanup(reader.close)
        return reader

    def expected_rows(self) -> list[tuple]:
        return self.connection.execute(
            "SELECT id, body FROM items ORDER BY id"
        ).fetchall()

    def scratch_bytes(self) -> bytes:
        reader = self.reader({"WECHAT_DIRECT_CACHE": "0"})
        try:
            path = reader._prepare(self.source.path)
            self.assertTrue(path.is_relative_to(self.root / "wechat-direct-scratch"))
            return path.read_bytes()
        finally:
            reader.close()

    def read_cached(self) -> Path:
        reader = self.reader()
        path = reader._prepare(self.source.path)
        self.assertTrue(path.is_relative_to(self.cache_root))
        self.assertEqual(path.read_bytes(), self.scratch_bytes())
        rows = reader._open(self.source.path).execute(
            "SELECT id, body FROM items ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], self.expected_rows())
        reader.close()
        return path

    def metadata(self) -> dict:
        (path,) = self.cache_root.rglob("metadata.json")
        return json.loads(path.read_text("utf-8"))

    def test_cached_snapshots_track_wal_checkpoints_and_restarts(self):
        rebuild = mock.patch.object(
            PlaintextCacheEntry,
            "_rebuild",
            autospec=True,
            side_effect=PlaintextCacheEntry._rebuild,
        )
        with rebuild as rebuilt:
            self.publish(inserts=3)
            self.read_cached()
            self.assertEqual(rebuilt.call_count, 1)
            self.publish(inserts=40)
            self.read_cached()
            self.publish(checkpoint="PASSIVE")
            self.read_cached()
            self.publish(inserts=5)  # WAL restarts over older frames
            self.read_cached()
            self.publish(inserts=2, checkpoint="TRUNCATE")
            self.read_cached()
            self.connection.execute("DELETE FROM items WHERE id < 300")
            self.publish(checkpoint="TRUNCATE")
            self.connection.execute("VACUUM")
            self.publish(checkpoint="TRUNCATE")
            self.read_cached()
            self.publish(inserts=1)
            self.read_cached()
            self.assertEqual(rebuilt.call_count, 1)
        self.assertEqual(self.metadata()["state"], "clean")

    def test_quick_check_runs_only_after_main_pages_change(self):
        checks = mock.patch.object(
            PlaintextCacheEntry,
            "quick_check",
            autospec=True,
            side_effect=PlaintextCacheEntry.quick_check,
        )
        with checks as checked:
            self.read_cached()
            self.assertEqual(checked.call_count, 1)
            self.publish(inserts=10)
            self.read_cached()
            self.assertEqual(checked.call_count, 1)
            self.publish(checkpoint="TRUNCATE")
            self.read_cached()
            self.assertEqual(checked.call_count, 2)

    def test_failed_quick_check_falls_back_and_leaves_entry_dirty(self):
        with mock.patch.object(PlaintextCacheEntry, "quick_check", return_value=False):
            reader = self.reader()
            path = reader._prepare(self.source.path)
        self.assertTrue(path.is_relative_to(self.root / "wechat-direct-scratch"))
        self.assertEqual(path.read_bytes(), self.scratch_bytes())
        self.assertEqual(self.metadata()["state"], "dirty")
        reader.close()
        self.read_cached()
        self.assertEqual(self.metadata()["state"], "clean")

    def test_hmac_failure_reports_error_and_entry_is_not_clean(self):
        self.read_cached()
        corrupt = bytearray(self.source.sealed[2])
        corrupt[SIZE - 1] ^= 1  # changed tag: the page is re-read and rejected
        self.source.sealed[2] = bytes(corrupt)
        self.source.path.write_bytes(b"".join(self.source.sealed))
        os.utime(self.source.path, ns=(7, 7))
        reader = self.reader()
        with self.assertRaisesRegex(SnapshotCopyError, "could not be prepared"):
            reader._prepare(self.source.path)
        self.assertNotEqual(self.metadata()["state"], "clean")

    def test_disabled_cache_uses_scratch_and_writes_no_cache(self):
        reader = self.reader({"WECHAT_DIRECT_CACHE": "0", "WECHAT_DIRECT_CACHE_ROOT": str(self.cache_root)})
        path = reader._prepare(self.source.path)
        self.assertTrue(path.is_relative_to(self.root / "wechat-direct-scratch"))
        self.assertFalse(self.cache_root.exists())

    @unittest.skipUnless(os.name == "nt", "Windows byte-range lock")
    def test_lock_held_elsewhere_falls_back_to_scratch(self):
        self.read_cached()
        (lock,) = self.cache_root.rglob("lock")
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import msvcrt, sys\n"
                "stream = open(sys.argv[1], 'r+b')\n"
                "msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)\n"
                "print('locked', flush=True)\n"
                "sys.stdin.read()\n",
                str(lock),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait, 10)
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        self.publish(inserts=4)
        with mock.patch.object(wechat_plaincache, "LOCK_TIMEOUT_S", 0.3):
            reader = self.reader()
            path = reader._prepare(self.source.path)
        self.assertTrue(path.is_relative_to(self.root / "wechat-direct-scratch"))
        rows = reader._open(self.source.path).execute(
            "SELECT id, body FROM items ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], self.expected_rows())
        reader.close()
        holder.stdin.close()
        holder.wait(10)
        self.read_cached()

    def test_busy_locks_share_one_wait_budget_per_reader(self):
        self.read_cached()
        offered: list[float] = []

        def busy(entry, timeout=None):
            offered.append(timeout)
            time.sleep(min(timeout, 0.2))
            return False

        with (
            mock.patch.object(wechat_plaincache, "LOCK_TIMEOUT_S", 0.3),
            mock.patch.object(wechat_plaincache.PlaintextCacheEntry, "acquire", busy),
        ):
            reader = self.reader()
            self.assertIsNone(reader._prepare_cached(self.source.path))
            self.assertIsNone(reader._prepare_cached(self.source.path))
            self.assertIsNone(reader._prepare_cached(self.source.path))
        reader.close()
        self.assertAlmostEqual(offered[0], 0.3, places=6)
        self.assertLess(offered[1], 0.15)
        self.assertEqual(offered[2], 0.0)

    def test_reader_holds_lock_until_close_and_keeps_snapshot(self):
        first = self.reader()
        cached = first._prepare(self.source.path)
        second = self.reader()
        started = time.monotonic()
        fallback = second._prepare(self.source.path)
        self.assertLess(time.monotonic() - started, 2.0)  # same process: no wait
        self.assertTrue(fallback.is_relative_to(self.root / "wechat-direct-scratch"))
        self.assertEqual(fallback.read_bytes(), cached.read_bytes())
        (lock,) = self.cache_root.rglob("lock")
        entry = PlaintextCacheEntry(self.cache_root, "x", "y")
        entry.lock_path, entry.directory = lock, lock.parent
        with mock.patch.object(wechat_plaincache, "_HELD", set()):
            self.assertFalse(entry.acquire(timeout=0))
        first.close()
        second.close()
        self.assertTrue(cached.is_file())
        self.assertTrue(entry.acquire(timeout=0))
        entry.release()

    def test_source_change_during_refresh_retries(self):
        self.read_cached()
        refresh, commit = PlaintextCacheEntry.refresh, PlaintextCacheEntry.commit
        calls, commits = [], []

        def change_once(entry, *args):
            outcome = refresh(entry, *args)
            calls.append(outcome)
            if len(calls) == 1:
                self.publish(inserts=6)
            return outcome

        def record_commit(entry, outcome, *, check_pending):
            commit(entry, outcome, check_pending=check_pending)
            commits.append(entry.read_metadata()["state"])

        self.publish(inserts=2)
        with (
            mock.patch.object(
                PlaintextCacheEntry, "refresh", autospec=True, side_effect=change_once
            ),
            mock.patch.object(
                PlaintextCacheEntry, "commit", autospec=True, side_effect=record_commit
            ),
        ):
            reader = self.reader()
            path = reader._prepare(self.source.path)
        self.assertEqual(len(calls), 2)
        # The unstable first round is still committed clean, so the retry is
        # incremental rather than a rebuild.
        self.assertEqual(commits, ["clean", "clean"])
        self.assertEqual([outcome.rebuilt for outcome in calls], [False, False])
        self.assertEqual(path.read_bytes(), self.scratch_bytes())
        reader.close()

    def test_torn_page_read_mid_refresh_retries_with_rebuild(self):
        self.read_cached()
        self.publish(inserts=3)
        finished = self.source.sealer.seal(3, self.source.plain[2])
        torn = bytearray(finished)
        torn[:100] = self.source.sealed[2][:100]  # new tag, half-old body
        self.source.sealed[2] = bytes(torn)
        self.source.path.write_bytes(b"".join(self.source.sealed))
        self.source._touch(self.source.path)
        refresh = PlaintextCacheEntry.refresh
        calls = []

        def finish_after_first(entry, *args):
            calls.append(None)
            try:
                return refresh(entry, *args)
            finally:
                if len(calls) == 1:  # the writer completes the page
                    self.source.sealed[2] = finished
                    self.source.path.write_bytes(b"".join(self.source.sealed))
                    self.source._touch(self.source.path)

        rebuild = mock.patch.object(
            PlaintextCacheEntry,
            "_rebuild",
            autospec=True,
            side_effect=PlaintextCacheEntry._rebuild,
        )
        with (
            mock.patch.object(
                PlaintextCacheEntry, "refresh", autospec=True,
                side_effect=finish_after_first,
            ),
            rebuild as rebuilt,
        ):
            reader = self.reader()
            path = reader._prepare(self.source.path)
        self.assertEqual(len(calls), 2)
        self.assertEqual(rebuilt.call_count, 1)  # dirty after the failed round
        self.assertTrue(path.is_relative_to(self.cache_root))
        self.assertEqual(path.read_bytes(), self.scratch_bytes())
        reader.close()

    def test_source_that_never_settles_reports_existing_error(self):
        self.read_cached()
        refresh = PlaintextCacheEntry.refresh

        def always_change(entry, *args):
            outcome = refresh(entry, *args)
            self.publish(inserts=1)
            return outcome

        with mock.patch.object(
            PlaintextCacheEntry, "refresh", autospec=True, side_effect=always_change
        ):
            reader = self.reader()
            with self.assertRaisesRegex(SnapshotCopyError, "changed throughout"):
                reader._prepare(self.source.path)
        reader.close()
        self.read_cached()


class CacheSettingsTests(unittest.TestCase):
    def test_root_priority_and_switch(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            self.assertEqual(resolve_cache_root("configured"), Path("configured"))
            with mock.patch.object(wechat_plaincache, "DEFAULT_ROOT", None):
                self.assertEqual(
                    resolve_cache_root(None),
                    Path(tempfile.gettempdir()) / "WeChatDirect" / "plain-cache",
                )
            with mock.patch.dict(os.environ, {"WECHAT_DIRECT_CACHE_ROOT": "env"}):
                self.assertEqual(resolve_cache_root("configured"), Path("env"))
                with mock.patch.dict(os.environ, {"WECHAT_DIRECT_CACHE": "0"}):
                    self.assertIsNone(resolve_cache_root("configured"))

    def test_local_setting_is_accepted_and_passed_to_the_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / ".wechatdirect.local.json"
            settings.write_text(
                json.dumps({"plaintextCacheRoot": "settings-cache"}), encoding="utf-8"
            )
            with mock.patch.object(wechat_cli, "LOCAL_SETTINGS_PATH", settings):
                self.assertEqual(wechat_cli._plaintext_cache_setting(), "settings-cache")
                with mock.patch.dict(os.environ, {"WECHAT_DIRECT_CACHE": "0"}):
                    self.assertIsNone(wechat_cli._plaintext_cache_setting())
                with mock.patch.dict(os.environ, {"WECHAT_DIRECT_CACHE_ROOT": "e"}):
                    self.assertIsNone(wechat_cli._plaintext_cache_setting())
                captured = {}

                class Reader:
                    account_identity_commitment = "0" * 64

                    def __init__(self, **kwargs):
                        captured.update(kwargs)

                    def close(self):
                        pass

                account = {
                    "config_path": "c",
                    "local_state_path": "s",
                    "expected_moments_author_sha256": "sha256:" + "1" * 64,
                    "expected_source_identity_sha256": "sha256:" + "0" * 64,
                }
                with mock.patch.object(wechat_cli, "DirectWeChatReader", Reader):
                    wechat_cli._reader(account, 1)
                self.assertEqual(captured["plaintext_cache_root"], "settings-cache")

    def test_quick_check_uses_read_only_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            entry = PlaintextCacheEntry(Path(temporary), "a" * 64, "x.db")
            entry.directory.mkdir(parents=True)
            with closing(sqlite3.connect(entry.plain)) as connection:
                connection.execute("CREATE TABLE t(x)")
                connection.commit()
            before = entry.plain.read_bytes()
            self.assertTrue(entry.quick_check())
            self.assertEqual(entry.plain.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
