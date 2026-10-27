"""Persistent decrypted snapshot cache for encrypted local WeChat databases.

The owner accepts long-lived local plaintext on this machine, so the cache adds
no encryption, masking or expiry; the whole root may be deleted at any time and
is rebuilt on demand. It is separate from task scratch and ``temp-status``.

One entry per account identity commitment and source relative path holds the
decrypted main database with committed WAL frames applied, the reserve bytes
(IV and HMAC tag) of every main-database page, JSON metadata and a lock file.
SQLCipher writes a fresh IV and tag with every page write, and the tag binds
the ciphertext and page number, so an unchanged reserve proves an unchanged
page without decrypting it. Source files are only ever opened for reading.

An OS byte-range lock serialises refreshes and is held for the whole read, so
no other process rewrites a snapshot an open reader still uses. Metadata is
marked dirty before any snapshot write; any failure leaves it dirty, which
forces a full rebuild on the next refresh.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from typing import Any, BinaryIO, Iterable, Sequence
from urllib.parse import quote
import weakref

from wechat_storage import open_source_read


CACHE_FORMAT = "wechat-direct-plaintext-cache.v1"
# None means ``<TEMP>/WeChatDirect/plain-cache``; tests point this elsewhere.
DEFAULT_ROOT: Path | None = None
LOCK_TIMEOUT_S = 5.0
LOCK_POLL_S = 0.05
COMPARE_CHUNK_PAGES = 2048
RESTORE_RUN_PAGES = 256

_HELD: set[str] = set()
_HELD_GUARD = threading.Lock()


class PlaintextCacheError(RuntimeError):
    """A cache refresh could not complete; never contains source paths."""


def resolve_cache_root(configured: Path | str | None = None) -> Path | None:
    """Return the cache root, or ``None`` when ``WECHAT_DIRECT_CACHE=0``.

    Priority: ``WECHAT_DIRECT_CACHE_ROOT`` > the local setting passed in as
    ``configured`` > ``<TEMP>/WeChatDirect/plain-cache``.
    """

    if os.environ.get("WECHAT_DIRECT_CACHE", "").strip() == "0":
        return None
    value = os.environ.get("WECHAT_DIRECT_CACHE_ROOT", "").strip()
    if value:
        return Path(value)
    if configured is not None and str(configured).strip():
        return Path(configured)
    if DEFAULT_ROOT is not None:
        return Path(DEFAULT_ROOT)
    return Path(tempfile.gettempdir()) / "WeChatDirect" / "plain-cache"


def _try_lock(stream: BinaryIO) -> bool:
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_UN)


def _release_lock(stream: BinaryIO, key: str) -> None:
    try:
        try:
            _unlock(stream)
        except OSError:
            pass  # closing the handle releases the lock as well
        stream.close()
    finally:
        with _HELD_GUARD:
            _HELD.discard(key)


def _encode_pages(pages: Iterable[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for page in sorted(set(pages)):
        if runs and runs[-1][1] + 1 == page:
            runs[-1][1] = page
        else:
            runs.append([page, page])
    return runs


def _decode_pages(value: object) -> frozenset[int] | None:
    if not isinstance(value, list):
        return None
    pages: set[int] = set()
    for run in value:
        if (
            not isinstance(run, list)
            or len(run) != 2
            or not all(type(item) is int for item in run)
            or not 1 <= run[0] <= run[1]
        ):
            return None
        pages.update(range(run[0], run[1] + 1))
    return frozenset(pages)


def _runs(numbers: Sequence[int], limit: int) -> Iterable[tuple[int, int]]:
    """Yield (first, count) for contiguous runs in sorted ``numbers``."""

    index = 0
    while index < len(numbers):
        first = numbers[index]
        count = 1
        while (
            index + count < len(numbers)
            and count < limit
            and numbers[index + count] == first + count
        ):
            count += 1
        yield first, count
        index += count


def _fsync_path(path: Path) -> None:
    # FlushFileBuffers needs write access on Windows.
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _wal_header(wal: Path) -> str | None:
    """The WAL header (salts, checkpoint sequence) or ``None`` when empty."""

    try:
        with open_source_read(wal) as stream:
            header = stream.read(32)
    except FileNotFoundError:
        return None
    return header.hex() if len(header) == 32 else None


@dataclass(frozen=True)
class _EntryState:
    signature: list[int] | None
    main_pages: int
    overlay: frozenset[int]
    file_pages: int
    check_pending: bool
    wal_header: str | None


@dataclass(frozen=True)
class RefreshOutcome:
    """Metadata to commit once the caller confirms the sources were stable."""

    metadata: dict[str, Any]
    needs_check: bool
    rebuilt: bool
    decrypted_pages: int


class PlaintextCacheEntry:
    """One account-bound decrypted snapshot, its page reserves and lock."""

    def __init__(self, root: Path | str, account_commitment: str, relative: str):
        self.relative = relative
        self.directory = (
            Path(root)
            / account_commitment
            / hashlib.sha256(relative.encode("utf-8")).hexdigest()
        )
        self.plain = self.directory / "plain.sqlite3"
        self.reserves = self.directory / "reserves.dat"
        self.metadata_path = self.directory / "metadata.json"
        self.lock_path = self.directory / "lock"
        self._lock_stream: BinaryIO | None = None
        self._release: weakref.finalize | None = None

    def __repr__(self) -> str:
        return "PlaintextCacheEntry(<private local cache>)"

    # Locking -------------------------------------------------------------

    @property
    def locked(self) -> bool:
        return self._lock_stream is not None

    def acquire(self, timeout: float | None = None) -> bool:
        """Take the cross-process lock, waiting up to ``timeout`` seconds.

        A second reader in this process never waits: it falls back at once.
        """

        if self._lock_stream is not None:
            return True
        wait = LOCK_TIMEOUT_S if timeout is None else timeout
        key = os.path.normcase(os.path.abspath(self.lock_path))
        with _HELD_GUARD:
            if key in _HELD:
                return False
            _HELD.add(key)
        stream: BinaryIO | None = None
        acquired = False
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            stream = self.lock_path.open("a+b")
            stream.seek(0, 2)
            if not stream.tell():
                stream.write(b"0")
                stream.flush()
            deadline = time.monotonic() + wait
            while not _try_lock(stream):
                if time.monotonic() >= deadline:
                    return False
                time.sleep(LOCK_POLL_S)
            acquired = True
        finally:
            if not acquired:
                if stream is not None:
                    stream.close()
                with _HELD_GUARD:
                    _HELD.discard(key)
        self._lock_stream = stream
        # An entry dropped without release() still frees its lock and the
        # in-process registration, so later readers are not refused.
        self._release = weakref.finalize(self, _release_lock, stream, key)
        return True

    def release(self) -> None:
        release, self._release = self._release, None
        self._lock_stream = None
        if release is not None:
            release()

    # Metadata ------------------------------------------------------------

    def read_metadata(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _write_metadata(self, value: dict[str, Any]) -> None:
        temporary = self.metadata_path.with_name(self.metadata_path.name + ".new")
        with temporary.open("wb") as stream:
            stream.write(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
        temporary.replace(self.metadata_path)

    def mark_dirty(self) -> None:
        """Durably mark the entry dirty; always done before snapshot writes.

        Overwritten in place and flushed rather than renamed: a rename is not
        ordered before later in-place snapshot writes on power loss, while a
        torn in-place record is unreadable and therefore also forces a rebuild.
        """

        self._require_lock()
        with self.metadata_path.open("wb") as stream:
            stream.write(
                json.dumps(
                    {"format": CACHE_FORMAT, "source": self.relative, "state": "dirty"},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())

    def commit(self, outcome: RefreshOutcome, *, check_pending: bool) -> None:
        """Record a completed refresh as clean.

        Snapshot and reserve data were flushed before this point. Losing this
        record in a power cut only reverts the entry to its durable dirty
        state (or unreadable metadata), both of which force a rebuild.
        """

        self._require_lock()
        self._write_metadata(
            {
                **outcome.metadata,
                "state": "clean",
                "checkPending": bool(check_pending),
                "updatedAtS": int(time.time()),
            }
        )

    def _require_lock(self) -> None:
        if self._lock_stream is None:
            raise PlaintextCacheError("plaintext_cache_lock_not_held")

    def _usable_state(
        self, metadata: dict[str, Any] | None, *, salt: str, key: str, codec: Any
    ) -> _EntryState | None:
        if (
            metadata is None
            or metadata.get("format") != CACHE_FORMAT
            or metadata.get("state") != "clean"
            or metadata.get("source") != self.relative
            or metadata.get("salt") != salt
            or metadata.get("keyFingerprint") != key
            or metadata.get("pageSize") != codec.page_size
            or metadata.get("reserveSize") != codec.reserve_size
        ):
            return None
        main_pages = metadata.get("mainPages")
        file_pages = metadata.get("walFilePages")
        if type(main_pages) is not int or type(file_pages) is not int:
            return None
        if main_pages < 1 or file_pages < 1:
            return None
        overlay = _decode_pages(metadata.get("walPages"))
        if overlay is None:
            return None
        wal_header = metadata.get("walHeader")
        if wal_header is not None and not isinstance(wal_header, str):
            return None
        signature = metadata.get("mainSignature")
        if signature is not None and not (
            isinstance(signature, list)
            and len(signature) == 4
            and all(type(item) is int for item in signature)
        ):
            return None
        try:
            if (
                self.plain.stat().st_size != file_pages * codec.page_size
                or self.reserves.stat().st_size != main_pages * codec.reserve_size
            ):
                return None
        except OSError:
            return None
        return _EntryState(
            signature,
            main_pages,
            overlay,
            file_pages,
            metadata.get("checkPending") is True,
            wal_header,
        )

    # Refresh -------------------------------------------------------------

    def refresh(
        self,
        codec: Any,
        source: Path,
        wal: Path,
        main_signature: Sequence[int] | None,
    ) -> RefreshOutcome:
        """Bring the snapshot up to the current main database plus its WAL.

        The entry stays dirty on return; ``commit`` marks it clean after the
        caller has confirmed source stability and any required quick_check.
        ``main_signature`` must be taken before this call.
        """

        self._require_lock()
        salt = bytes(codec.salt).hex()
        key = codec.key_fingerprint
        state = self._usable_state(
            self.read_metadata(), salt=salt, key=key, codec=codec
        )
        # Read before any merge, like ``main_signature``. A new WAL header
        # means frames left the WAL through a checkpoint, so main pages may
        # have changed even if the main signature's time resolution hid it.
        wal_header = _wal_header(wal)
        self.mark_dirty()
        if state is None:
            main_pages = self._rebuild(codec, source)
            needs_check, decrypted, written = True, main_pages, True
        else:
            main_pages, changed, decrypted, written = self._incremental(
                codec, source, state, main_signature, wal_header != state.wal_header
            )
            needs_check = changed or state.check_pending
        overlay, file_pages, merged = self._apply_wal(codec, wal, main_pages)
        if written and not merged:
            # A WAL merge flushes the whole file itself.
            _fsync_path(self.plain)
        metadata = {
            "format": CACHE_FORMAT,
            "source": self.relative,
            "state": "dirty",
            "pageSize": codec.page_size,
            "reserveSize": codec.reserve_size,
            "salt": salt,
            "keyFingerprint": key,
            "mainSignature": None if main_signature is None else list(main_signature),
            "mainPages": main_pages,
            "walPages": _encode_pages(overlay),
            "walFilePages": file_pages,
            "walHeader": wal_header,
        }
        return RefreshOutcome(metadata, needs_check, state is None, decrypted)

    def _rebuild(self, codec: Any, source: Path) -> int:
        part = self.plain.with_name(self.plain.name + ".part")
        reserve_part = self.reserves.with_name(self.reserves.name + ".part")
        part.unlink(missing_ok=True)
        reserve_part.unlink(missing_ok=True)
        try:
            with reserve_part.open("xb") as sink:
                codec.decrypt_database(source, self.plain, reserve_output=sink)
                sink.flush()
                os.fsync(sink.fileno())
            reserve_part.replace(self.reserves)
        finally:
            reserve_part.unlink(missing_ok=True)
        pages = self.plain.stat().st_size // codec.page_size
        if pages < 1 or self.reserves.stat().st_size != pages * codec.reserve_size:
            raise PlaintextCacheError("plaintext_cache_rebuild_inconsistent")
        return pages

    def _incremental(
        self,
        codec: Any,
        source: Path,
        state: _EntryState,
        main_signature: Sequence[int] | None,
        wal_restarted: bool,
    ) -> tuple[int, bool, int, bool]:
        """Restore WAL-overlaid pages and apply changed main pages in place.

        Returns (main pages, whether main content changed, pages decrypted,
        whether the snapshot file was written). Reserves are flushed here;
        the snapshot is flushed by the caller.
        """

        page_size, reserve_size = codec.page_size, codec.reserve_size
        size = source.stat().st_size
        if size == 0 or size % page_size:
            raise PlaintextCacheError("plaintext_cache_source_not_page_aligned")
        pages = size // page_size
        main_changed = (
            wal_restarted
            or main_signature is None
            or state.signature is None
            or list(main_signature) != state.signature
            or pages != state.main_pages
        )
        with (
            open_source_read(source) as stream,
            self.plain.open("r+b") as output,
            self.reserves.open("r+b") as reserves,
        ):
            changed = False
            decrypted = 0
            if not main_changed:
                changed, decrypted = self._restore_pass(
                    codec, stream, output, reserves, state, pages
                )
                # A restored page with a new reserve means the main database
                # changed within the signature's time resolution; compare all.
                main_changed = changed
            if main_changed:
                differs, compared = self._compare_pass(
                    codec, stream, output, reserves, state, pages
                )
                changed = changed or differs or pages != state.main_pages
                decrypted += compared
            output.truncate(pages * page_size)
            reserves.truncate(pages * reserve_size)
            # Both passes rewrite reserves exactly when ``changed`` is set.
            if changed or pages != state.main_pages:
                reserves.flush()
                os.fsync(reserves.fileno())
        written = bool(decrypted) or pages != state.file_pages
        return pages, changed, decrypted, written

    @staticmethod
    def _write_decrypted(
        codec: Any,
        output: BinaryIO,
        numbers: Sequence[int],
        encrypted: Sequence[bytes],
    ) -> None:
        if not numbers:
            return
        plaintext = codec.decrypt_pages(numbers, encrypted)
        page_size = codec.page_size
        position = 0
        for first, count in _runs(numbers, len(numbers)):
            output.seek((first - 1) * page_size)
            output.write(plaintext[position : position + count * page_size])
            position += count * page_size

    def _restore_pass(
        self,
        codec: Any,
        stream: BinaryIO,
        output: BinaryIO,
        reserves: BinaryIO,
        state: _EntryState,
        pages: int,
    ) -> tuple[bool, int]:
        """Main unchanged: re-decrypt only pages the last WAL overlay touched."""

        page_size, reserve_size = codec.page_size, codec.reserve_size
        payload = page_size - reserve_size
        targets = sorted(
            {page for page in state.overlay if page <= pages}
            | set(range(state.file_pages + 1, pages + 1))
        )
        changed = False
        for first, count in _runs(targets, RESTORE_RUN_PAGES):
            stream.seek((first - 1) * page_size)
            data = stream.read(count * page_size)
            if len(data) != count * page_size:
                raise PlaintextCacheError("plaintext_cache_source_truncated")
            encrypted = [
                data[index * page_size : (index + 1) * page_size]
                for index in range(count)
            ]
            current = b"".join(page[payload:] for page in encrypted)
            reserves.seek((first - 1) * reserve_size)
            if reserves.read(count * reserve_size) != current:
                # Same signature yet different pages: record and re-check.
                changed = True
                reserves.seek((first - 1) * reserve_size)
                reserves.write(current)
            self._write_decrypted(
                codec, output, range(first, first + count), encrypted
            )
        return changed, len(targets)

    def _compare_pass(
        self,
        codec: Any,
        stream: BinaryIO,
        output: BinaryIO,
        reserves: BinaryIO,
        state: _EntryState,
        pages: int,
    ) -> tuple[bool, int]:
        """Main changed: decrypt pages whose reserve changed, plus restores."""

        page_size, reserve_size = codec.page_size, codec.reserve_size
        payload = page_size - reserve_size
        reserves.seek(0)
        previous_all = reserves.read()
        overlay = sorted(state.overlay)
        changed = False
        decrypted = 0
        stream.seek(0)
        for first in range(1, pages + 1, COMPARE_CHUNK_PAGES):
            count = min(COMPARE_CHUNK_PAGES, pages - first + 1)
            last = first + count - 1
            data = stream.read(count * page_size)
            if len(data) != count * page_size:
                raise PlaintextCacheError("plaintext_cache_source_truncated")
            current = b"".join(
                data[index * page_size + payload : (index + 1) * page_size]
                for index in range(count)
            )
            low = (first - 1) * reserve_size
            previous = previous_all[low : low + count * reserve_size]
            position = bisect_left(overlay, first)
            restore_inside = (
                position < len(overlay) and overlay[position] <= last
            ) or last > state.file_pages
            if current == previous and not restore_inside:
                continue
            numbers: list[int] = []
            encrypted: list[bytes] = []
            for index in range(count):
                number = first + index
                start = index * reserve_size
                differs = (
                    current[start : start + reserve_size]
                    != previous[start : start + reserve_size]
                )
                if differs:
                    changed = True
                if differs or number > state.file_pages or (
                    restore_inside and number in state.overlay
                ):
                    numbers.append(number)
                    encrypted.append(data[index * page_size : (index + 1) * page_size])
            self._write_decrypted(codec, output, numbers, encrypted)
            decrypted += len(numbers)
            if current != previous:
                reserves.seek(low)
                reserves.write(current)
        return changed, decrypted

    def _apply_wal(
        self, codec: Any, wal: Path, main_pages: int
    ) -> tuple[frozenset[int], int, bool]:
        """Return (pages written, resulting page count, whether merged)."""

        try:
            if wal.stat().st_size == 0:
                return frozenset(), main_pages, False
        except FileNotFoundError:
            return frozenset(), main_pages, False
        result = codec.merge_wal_detailed(self.plain, wal)
        if result.database_pages is None:
            return frozenset(), main_pages, False
        return result.pages_written, result.database_pages, True

    def quick_check(self) -> bool:
        connection = sqlite3.connect(
            f"file:{quote(str(self.plain), safe='/:\\\\')}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            return str(connection.execute("PRAGMA quick_check").fetchone()[0]) == "ok"
        finally:
            connection.close()
