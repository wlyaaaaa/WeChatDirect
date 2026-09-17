"""One streaming SQLite/SQLCipher WAL interpretation for probe and snapshot.

The checksums cover the stored (encrypted for SQLCipher) page bytes. SQLCipher
calls sqlcipherPagerCodec before walEncodeFrame. Format references:
https://www.sqlite.org/fileformat2.html#walformat
https://github.com/sqlcipher/sqlcipher/blob/master/src/wal.c
No source file is opened for writing and no SQLite connection is made to it.
"""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import struct


class WalError(ValueError):
    """Unsupported or inconsistent snapshot input; never contains source paths."""


@dataclass(frozen=True)
class WalScan:
    # page -> (zero-based frame index, file offset of stored page bytes)
    offsets: dict[int, tuple[int, int]]
    committed_frames: int
    database_pages: int | None
    commit_boundaries: frozenset[int]
    tail_status: str


def checksum(
    data: bytes, state: tuple[int, int] = (0, 0), *, big_endian: bool = False
) -> tuple[int, int]:
    if len(data) % 8:
        raise WalError("wal_checksum_input_invalid")
    a, b = state
    for x, y in struct.iter_unpack(">II" if big_endian else "<II", data):
        a = (a + x + b) & 0xFFFFFFFF
        b = (b + y + a) & 0xFFFFFFFF
    return a, b


def scan_wal(
    path: Path, *, page_size: int, base_page_count: int, max_frames: int = 131072
) -> WalScan:
    """Return only the last valid contiguous committed prefix, with bounded memory.

    Old-generation/preallocated/torn tails end the prefix, never resume it.
    Every frame checksum binds its page number and transaction commit size.
    No decrypt is attempted for uncommitted frames. A malformed nonempty header
    is an explicit failure, not an empty WAL or a different supported format.
    """
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return WalScan({}, 0, None, frozenset(), "empty")
    if (
        page_size < 512
        or page_size > 65536
        or page_size & (page_size - 1)
        or base_page_count < 0
    ):
        raise WalError("wal_database_geometry_invalid")
    committed: dict[int, tuple[int, int]] = {}
    pending: dict[int, tuple[int, int]] = {}
    boundaries: set[int] = set()
    last = 0
    pages = None
    tail = "end"
    covered = base_page_count
    beyond: set[int] = set()
    with path.open("rb") as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise WalError("wal_header_truncated")
        magic, version, declared, _checkpoint = struct.unpack(">IIII", header[:16])
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000:
            raise WalError("wal_header_format_unsupported")
        if declared != page_size:
            raise WalError("wal_page_size_mismatch")
        big = bool(magic & 1)
        sums = checksum(header[:24], big_endian=big)
        if sums != struct.unpack(">II", header[24:]):
            raise WalError("wal_header_checksum_mismatch")
        generation = header[16:24]
        index = 0
        while True:
            frame = stream.read(24)
            if not frame:
                break
            if len(frame) != 24:
                tail = "truncated_frame"
                break
            if frame[8:16] != generation:
                tail = "generation_end"
                break
            if index >= max_frames:
                raise WalError("wal_frame_limit_exceeded")
            stored = stream.read(page_size)
            if len(stored) != page_size:
                tail = "truncated_page"
                break
            number, size = struct.unpack(">II", frame[:8])
            if number == 0 or number > 0xFFFFFFFE:
                tail = "invalid_page"
                break
            next_sums = checksum(
                stored, checksum(frame[:8], sums, big_endian=big), big_endian=big
            )
            if next_sums != struct.unpack(">II", frame[16:24]):
                tail = "checksum_end"
                break
            sums = next_sums
            pending[number] = (index, 32 + index * (24 + page_size) + 24)
            if number > covered:
                beyond.add(number)
                while covered + 1 in beyond:
                    covered += 1
                    beyond.remove(covered)
            if size:
                # All newly extended pages must actually be supplied, not just
                # a single distant page advertising a huge sparse database.
                if number > size or size > covered:
                    raise WalError("wal_commit_geometry_invalid")
                committed.update(pending)
                committed = {n: loc for n, loc in committed.items() if n <= size}
                pending.clear()
                covered = size
                beyond.clear()
                last = index + 1
                pages = size
                boundaries.add(last)
            index += 1
        if tail == "end" and pending:
            tail = "uncommitted_tail"
    return WalScan(committed, last, pages, frozenset(boundaries), tail)


def copy_plain_snapshot(source: Path, destination: Path, wal: Path) -> None:
    """Copy and overlay a plaintext source without source-side WAL/SHM writes."""
    source = Path(source)
    destination = Path(destination)
    if source.resolve() == destination.resolve() or (
        destination.exists() and os.path.samefile(source, destination)
    ):
        raise WalError("snapshot_source_destination_same")
    with source.open("rb") as stream:
        header = stream.read(100)
    if len(header) != 100 or header[:16] != b"SQLite format 3\0":
        raise WalError("sqlite_header_invalid")
    size = struct.unpack(">H", header[16:18])[0]
    size = 65536 if size == 1 else size
    length = source.stat().st_size
    if not size or length % size:
        raise WalError("sqlite_page_geometry_invalid")
    scan = scan_wal(wal, page_size=size, base_page_count=length // size)
    shutil.copy2(source, destination)
    if scan.database_pages is None:
        return
    with Path(wal).open("rb") as stream, destination.open("r+b") as out:
        for number, (_index, offset) in sorted(scan.offsets.items()):
            stream.seek(offset)
            payload = stream.read(size)
            if len(payload) != size:
                raise WalError("wal_changed_during_snapshot")
            out.seek((number - 1) * size)
            out.write(payload)
        out.truncate(scan.database_pages * size)
        out.seek(28)
        out.write(struct.pack(">I", scan.database_pages))
        out.flush()
        os.fsync(out.fileno())
