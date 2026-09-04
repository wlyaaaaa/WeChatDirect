"""Exact, read-only WeChat source reader for the standalone WeChat project.

This is not a service adapter or a general SQLite toolkit. It verifies one
configured account, takes bounded database/WAL snapshots, decrypts the fixed
SQLCipher form, then projects account-scoped sessions, messages and media after
their per-session native watermark. Source files are never opened for writing,
and the module does not log or print private row values.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
from dataclasses import dataclass
from functools import cached_property
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import tempfile
import time
import unicodedata
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import quote
import xml.etree.ElementTree as ET


PAGE_SIZE = 4096
RESERVE_SIZE = 80
PBKDF2_ROUNDS = 256_000
HMAC_SIZE = 64
AES_BLOCK_SIZE = 16
MAX_DECOMPRESSED_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_SCHEMA_PROBE_PAGES_PER_SHARD = 4_096
MAX_MESSAGE_SCHEMA_PROBE_WAL_FRAMES = 131_072
MESSAGE_FETCH_PAGE_SIZE = 512
# A group cursor is primarily a per-shard physical rowid receipt.  Re-reading a
# small tail protects against a source row being amended shortly after it first
# appeared, without turning normal group increments back into history scans.
GROUP_ANCHOR_TAIL_ROWS = 256
# This receipt is deliberately opaque: it lets a later bounded group delta
# classify the ambiguous native statuses without retaining a raw sender id in
# the durable session cursor.  It is a source-local compatibility receipt, not
# a general identity service.
# Frozen legacy wire identifier. Existing v1 state depends on these exact bytes;
# the name does not imply a runtime dependency on another project or service.
GROUP_SELF_SENDER_RECEIPT_ALGORITHM = "pkb.wechat.group-self-sender.v1"


class WeChatDirectError(RuntimeError):
    """Base class for deterministic, body-free direct-read failures."""


class CryptoUnavailableError(WeChatDirectError):
    """The optional AES implementation is not installed."""


class EncryptedPageError(WeChatDirectError):
    """A page could not be authenticated or decrypted."""


class SnapshotCopyError(WeChatDirectError):
    """A snapshot could not be copied into its disposable workspace."""


class DirectSchemaError(WeChatDirectError):
    """The requested table/watermark is not present in the snapshot."""


class DirectCredentialError(WeChatDirectError):
    """The existing local encrypted credential carrier is invalid."""


class SessionMessageDatabaseMissingError(WeChatDirectError):
    """A registered session has no readable native message table."""


class MediaNotOpenableError(WeChatDirectError):
    """The locator resolves, but this reader cannot open the media payload."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi_unprotect(ciphertext: bytes) -> bytes:
    if os.name != "nt":  # pragma: no cover - this source is Windows-only
        raise DirectCredentialError("Windows DPAPI is required")
    source_buffer = ctypes.create_string_buffer(ciphertext)
    source = _DataBlob(
        len(ciphertext), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    clear = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(clear)
    ):
        raise DirectCredentialError("DPAPI credential unwrap failed")
    try:
        return ctypes.string_at(clear.data, clear.size)
    finally:
        kernel32.LocalFree(clear.data)


def _safe_storage_key(local_state_path: Path | str) -> bytes:
    try:
        payload = json.loads(Path(local_state_path).read_text(encoding="utf-8-sig"))
        wrapped = base64.b64decode(payload["os_crypt"]["encrypted_key"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DirectCredentialError("local encrypted key carrier is invalid") from exc
    if not wrapped.startswith(b"DPAPI"):
        raise DirectCredentialError("local encrypted key carrier has an unknown format")
    key = _dpapi_unprotect(wrapped[5:])
    if len(key) not in {16, 24, 32}:
        raise DirectCredentialError("local encrypted key has an invalid length")
    return key


def _decode_safe_value(value: object, key: bytes) -> str:
    if not isinstance(value, str):
        raise DirectCredentialError("required source credential is missing")
    if not value.startswith("safe:"):
        return value
    try:
        encrypted = base64.b64decode(value[5:])
        if not encrypted.startswith(b"v10") or len(encrypted) < 3 + 12 + 16:
            raise ValueError("invalid safe value")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        clear = AESGCM(key).decrypt(encrypted[3:15], encrypted[15:], None)
        return clear.decode("utf-8")
    except (ImportError, ValueError, UnicodeDecodeError) as exc:
        raise DirectCredentialError("local source credential decode failed") from exc


def load_direct_source_identity(
    config_path: Path | str, local_state_path: Path | str
) -> tuple[Path, str, str]:
    """Return account root, master key and native identity without logging values."""

    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DirectCredentialError("local source configuration is invalid") from exc
    if not isinstance(config, dict):
        raise DirectCredentialError("local source configuration is not an object")
    safe_key = _safe_storage_key(local_state_path)
    db_path = Path(_decode_safe_value(config.get("dbPath"), safe_key))
    master_hex = _decode_safe_value(config.get("decryptKey"), safe_key).strip()
    identity = _decode_safe_value(config.get("myWxid"), safe_key).strip()
    if len(master_hex) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in master_hex):
        raise DirectCredentialError("local source database key is invalid")
    if not identity:
        raise DirectCredentialError("local source account identity is missing")

    candidates: list[Path] = []
    if (db_path / "db_storage").is_dir():
        candidates.append(db_path)
    if db_path.is_dir():
        for child in db_path.iterdir():
            if child.is_dir() and (child / "db_storage").is_dir():
                candidates.append(child)
    unique = {candidate.resolve() for candidate in candidates}
    storage_suffix = re.compile(
        re.escape(identity) + r"_[0-9a-fA-F]{4}"
    )
    matching = [
        candidate
        for candidate in unique
        if candidate.name == identity or storage_suffix.fullmatch(candidate.name)
    ]
    if len(matching) != 1:
        raise DirectCredentialError("local source account directory is ambiguous")
    account_root = matching[0]
    return account_root, master_hex.lower(), identity


def load_direct_media_keys(
    config_path: Path | str, local_state_path: Path | str, identity: str
) -> tuple[bytes | None, int | None]:
    """Read the same account's existing protected media fields in memory only."""

    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise DirectCredentialError("local media configuration is unavailable") from exc
    if not isinstance(config, Mapping):
        raise DirectCredentialError("local media configuration is invalid")
    safe_key = _safe_storage_key(local_state_path)
    if _decode_safe_value(config.get("myWxid"), safe_key).strip() != identity:
        raise DirectCredentialError("local media account identity changed")
    scopes = config.get("wxidConfigs") or {}
    scoped = scopes.get(identity, {}) if isinstance(scopes, Mapping) else {}
    if not isinstance(scoped, Mapping):
        scoped = {}
    aes_value = scoped.get("imageAesKey") or config.get("imageAesKey")
    xor_value = scoped.get("imageXorKey")
    if xor_value in (None, ""):
        xor_value = config.get("imageXorKey")
    aes_key = None
    xor_key = None
    if aes_value:
        decoded = _decode_safe_value(aes_value, safe_key).strip()
        if len(decoded.encode("utf-8")) == 16:
            aes_key = decoded.encode("utf-8")
        elif re.fullmatch(r"[0-9a-fA-F]{32}", decoded):
            aes_key = bytes.fromhex(decoded)
    if xor_value is not None and xor_value != "":
        decoded = _decode_safe_value(str(xor_value), safe_key).strip()
        try:
            xor_key = int(decoded, 16 if decoded.lower().startswith("0x") or re.search(r"[a-f]", decoded, re.I) else 10)
        except ValueError:
            pass
        if xor_key is not None and not 0 <= xor_key <= 255:
            xor_key = None
    return aes_key, xor_key


def _as_bytes(value: bytes | bytearray | memoryview | str, *, hex_text: bool = False) -> bytes:
    if isinstance(value, str):
        text = value.strip()
        if hex_text:
            try:
                return bytes.fromhex(text)
            except ValueError as exc:
                raise ValueError("hex value is invalid") from exc
        return text.encode("utf-8")
    return bytes(value)


def derive_page_key(
    master_hex: str | bytes,
    salt: bytes | bytearray | memoryview | str,
    *,
    iterations: int = PBKDF2_ROUNDS,
) -> bytes:
    """Derive the 32-byte page key used by the direct reader.

    ``master_hex`` is deliberately decoded as hex rather than treated as a
    password string.  Salt may be raw bytes or a hex string when the caller's
    receipt stores it that way.
    """

    if isinstance(master_hex, str):
        try:
            password = bytes.fromhex(master_hex.strip())
        except ValueError as exc:
            raise ValueError("master_hex is not valid hexadecimal") from exc
    else:
        password = bytes(master_hex)
    if not password:
        raise ValueError("master_hex is empty")
    salt_bytes = _as_bytes(salt)
    if not salt_bytes:
        raise ValueError("salt is empty")
    if iterations <= 0:
        raise ValueError("iterations must be positive")
    return hashlib.pbkdf2_hmac("sha512", password, salt_bytes, iterations, 32)


def _aes_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    if len(iv) != AES_BLOCK_SIZE:
        raise EncryptedPageError("AES-CBC IV must be 16 bytes")
    if len(ciphertext) % AES_BLOCK_SIZE:
        raise EncryptedPageError("encrypted page payload is not block aligned")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise CryptoUnavailableError(
            "cryptography is required for the encrypted direct-read path"
        ) from exc
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


@dataclass(frozen=True)
class EncryptedPageCodec:
    """Decrypt fixed-size pages without touching the source file.

    The source format reserves 80 bytes at the end of every 4096-byte page:
    16 bytes for the IV and 64 bytes for an HMAC-SHA512 tag.  The default
    ``hmac_mode=None`` follows the native WCDB reader: the key is validated
    once from page 1, then pages are decrypted without inventing a second
    integrity protocol.  ``page_be_ciphertext``, ``page_le_ciphertext`` and
    ``ciphertext`` are available for a receipt that explicitly requires
    per-page HMAC validation.

    ``key_derivation='pbkdf2'`` implements the source handoff contract
    ``PBKDF2-HMAC-SHA512(master_hex, salt, 256000, 32)``.  The open-source
    WCDB replica instead supplies a validated raw 32-byte key (or key+salt),
    which is supported by ``key_derivation='raw'`` without memory scanning.
    """

    master_hex: str | bytes
    salt: bytes | bytearray | memoryview | str | None
    page_size: int = PAGE_SIZE
    reserve_size: int = RESERVE_SIZE
    iterations: int = PBKDF2_ROUNDS
    hmac_mode: str | None = None
    key_derivation: str = "pbkdf2"
    plaintext_header: bool | None = None
    iv_offset: int = 0
    tag_offset: int = AES_BLOCK_SIZE

    def __post_init__(self) -> None:
        if self.page_size <= 0 or self.page_size % AES_BLOCK_SIZE:
            raise ValueError("page_size must be a positive AES-block multiple")
        if self.reserve_size < AES_BLOCK_SIZE + HMAC_SIZE:
            raise ValueError("reserve_size must contain IV and HMAC")
        if self.page_size <= self.reserve_size:
            raise ValueError("reserve_size must be smaller than page_size")
        if self.iv_offset < 0 or self.tag_offset < 0:
            raise ValueError("reserve offsets must be non-negative")
        if self.tag_offset + HMAC_SIZE > self.reserve_size:
            raise ValueError("HMAC tag exceeds page reserve")
        if self.iv_offset + AES_BLOCK_SIZE > self.reserve_size:
            raise ValueError("IV exceeds page reserve")
        if self.hmac_mode not in {
            None,
            "page_be_ciphertext",
            "page_le_ciphertext",
            "ciphertext",
            "sqlcipher",
        }:
            raise ValueError("unsupported HMAC mode")
        if self.key_derivation not in {"pbkdf2", "raw"}:
            raise ValueError("unsupported key derivation")
        if self.key_derivation == "pbkdf2" and self.salt is None:
            raise ValueError("salt is required for PBKDF2")

    @cached_property
    def key(self) -> bytes:
        if self.key_derivation == "raw":
            if isinstance(self.master_hex, str):
                try:
                    value = bytes.fromhex(self.master_hex.strip())
                except ValueError as exc:
                    raise ValueError("raw master key is not valid hexadecimal") from exc
            else:
                value = bytes(self.master_hex)
            if len(value) not in {32, 48}:
                raise ValueError("raw master key must be 32 or 48 bytes")
            return value
        if self.salt is None:  # defensive; __post_init__ catches this
            raise ValueError("salt is required for PBKDF2")
        return derive_page_key(self.master_hex, self.salt, iterations=self.iterations)

    def _authentication_input(self, page_number: int, ciphertext: bytes) -> bytes:
        if self.hmac_mode == "sqlcipher":
            raise ValueError("sqlcipher HMAC uses the page layout, not this helper")
        if self.hmac_mode == "ciphertext":
            return ciphertext
        endian = "big" if self.hmac_mode == "page_be_ciphertext" else "little"
        return page_number.to_bytes(4, endian, signed=False) + ciphertext

    def decrypt_page(self, page_number: int, page: bytes) -> bytes:
        """Return one normal SQLite page; reserve bytes are cleared."""

        if page_number <= 0:
            raise EncryptedPageError("page number must be positive")
        if len(page) != self.page_size:
            raise EncryptedPageError("encrypted page has an unexpected size")
        payload_size = self.page_size - self.reserve_size
        if page_number == 1:
            ciphertext = page[AES_BLOCK_SIZE:payload_size]
        else:
            ciphertext = page[:payload_size]
        reserve = page[payload_size:]
        iv = reserve[self.iv_offset : self.iv_offset + AES_BLOCK_SIZE]
        if self.hmac_mode is not None:
            if not self.verify_page_hmac(page_number, page):
                raise EncryptedPageError("encrypted page authentication failed")
        plaintext = _aes_decrypt(ciphertext, self.key[:32], iv)
        if page_number == 1:
            if self.plaintext_header is None:
                preserve_header = len(self.key) == 48
            else:
                preserve_header = self.plaintext_header
            header = page[:AES_BLOCK_SIZE] if preserve_header else b"SQLite format 3\x00"
            return header + plaintext + (b"\x00" * self.reserve_size)
        return plaintext + (b"\x00" * self.reserve_size)

    def verify_page_hmac(self, page_number: int, page: bytes) -> bool:
        """Verify a configured page tag without decrypting or returning data."""

        if len(page) != self.page_size or page_number <= 0:
            return False
        payload_size = self.page_size - self.reserve_size
        reserve = page[payload_size:]
        tag = reserve[self.tag_offset : self.tag_offset + HMAC_SIZE]
        if self.hmac_mode == "sqlcipher":
            key = self.key[:32]
            if len(self.key) == 48:
                salt = self.key[32:48]
            elif self.salt is not None:
                salt = _as_bytes(self.salt)
            else:
                raise ValueError("SQLCipher HMAC requires the database salt")
            mac_salt = bytes(value ^ 0x3A for value in salt)
            mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, 32)
            hmac_data = page[16:payload_size + 16] if page_number == 1 else page[:payload_size + 16]
            expected = hmac.new(
                mac_key,
                hmac_data + struct.pack("<I", page_number),
                hashlib.sha512,
            ).digest()
            return hmac.compare_digest(expected, tag)
        if self.hmac_mode is None:
            return True
        ciphertext = page[16:payload_size] if page_number == 1 else page[:payload_size]
        expected = hmac.new(
            self.key[:32],
            self._authentication_input(page_number, ciphertext),
            hashlib.sha512,
        ).digest()
        return hmac.compare_digest(expected, tag)

    def decrypt_database(self, source: Path | str, destination: Path | str) -> Path:
        """Decrypt a database to ``destination`` using an atomic temp file."""

        source_path = Path(source)
        destination_path = Path(destination)
        if not source_path.is_file():
            raise SnapshotCopyError("source database does not exist")
        size = source_path.stat().st_size
        if size == 0 or size % self.page_size:
            raise SnapshotCopyError("encrypted database is not page aligned")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination_path.with_name(destination_path.name + ".part")
        try:
            with source_path.open("rb") as source_file, temporary.open("wb") as output:
                page_number = 1
                while True:
                    page = source_file.read(self.page_size)
                    if not page:
                        break
                    output.write(self.decrypt_page(page_number, page))
                    page_number += 1
            temporary.replace(destination_path)
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        return destination_path

    def merge_wal(
        self,
        database: Path | str,
        wal: Path | str,
        *,
        from_frame: int = 0,
    ) -> int:
        """Overlay encrypted WCDB WAL frames onto a decrypted database copy.

        WCDB's frame headers are big-endian and use an eight-byte generation
        salt at offsets 8..16.  Old frames can remain after a checkpoint, so
        only frames from the current WAL generation are considered.  A
        non-zero ``db_size`` marks a transaction commit.  Frames after the
        last complete, structurally valid commit are intentionally ignored,
        and the disposable database is truncated to that commit's page count.
        The source WAL is never changed.
        """

        database_path = Path(database)
        wal_path = Path(wal)
        if from_frame < 0:
            raise ValueError("from_frame must be non-negative")
        if not database_path.is_file() or not wal_path.is_file():
            raise SnapshotCopyError("database or WAL does not exist")
        frame_size = 24 + self.page_size
        with wal_path.open("rb") as wal_file:
            header = wal_file.read(32)
            if len(header) != 32:
                raise SnapshotCopyError("WAL header is truncated")
            generation = header[16:24]
            total_frames = max(0, (wal_path.stat().st_size - 32) // frame_size)
            existing_pages = (
                database_path.stat().st_size + self.page_size - 1
            ) // self.page_size
            max_page_seen = existing_pages
            last_commit_frame: int | None = None
            committed_pages: int | None = None

            # Metadata is scanned first so an uncommitted tail is never
            # decrypted or written.  A commit cannot claim pages that neither
            # the base database nor any preceding current-generation frame
            # supplies; this also bounds a corrupt sparse-file expansion.
            for index in range(from_frame, total_frames):
                wal_file.seek(32 + index * frame_size)
                frame_header = wal_file.read(24)
                encrypted_page = wal_file.read(self.page_size)
                if len(frame_header) != 24 or len(encrypted_page) != self.page_size:
                    break
                page_number, database_size = struct.unpack(">II", frame_header[:8])
                if frame_header[8:16] != generation:
                    continue
                if page_number <= 0:
                    break
                try:
                    plaintext = self.decrypt_page(page_number, encrypted_page)
                except EncryptedPageError:
                    break
                if page_number == 1 and plaintext[:16] != b"SQLite format 3\x00":
                    break
                max_page_seen = max(max_page_seen, page_number)
                if database_size:
                    if page_number > database_size or database_size > max_page_seen:
                        break
                    last_commit_frame = index
                    committed_pages = database_size

            if last_commit_frame is None or committed_pages is None:
                return from_frame

            with database_path.open("r+b") as output:
                for index in range(from_frame, last_commit_frame + 1):
                    wal_file.seek(32 + index * frame_size)
                    frame_header = wal_file.read(24)
                    encrypted_page = wal_file.read(self.page_size)
                    if len(frame_header) != 24 or len(encrypted_page) != self.page_size:
                        raise SnapshotCopyError("WAL commit frame is truncated")
                    if frame_header[8:16] != generation:
                        continue
                    page_number = struct.unpack(">I", frame_header[:4])[0]
                    if page_number <= 0:
                        raise SnapshotCopyError("WAL commit contains an invalid page")
                    plaintext = self.decrypt_page(page_number, encrypted_page)
                    if page_number == 1 and plaintext[:16] != b"SQLite format 3\x00":
                        raise SnapshotCopyError("WAL page one is not a SQLite database page")
                    output.seek((page_number - 1) * self.page_size)
                    output.write(plaintext)

                output.truncate(committed_pages * self.page_size)
                output.seek(0)
                page_one = output.read(self.page_size)
                if len(page_one) != self.page_size or page_one[:16] != b"SQLite format 3\x00":
                    raise SnapshotCopyError("merged WAL has no valid SQLite page one")
                output.seek(28)
                output.write(struct.pack(">I", committed_pages))
                output.flush()
            return last_commit_frame + 1


def _read_sqlite_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(9):
        position = offset + index
        if position >= len(data):
            raise SnapshotCopyError("SQLite schema varint is truncated")
        byte = data[position]
        if index == 8:
            return (value << 8) | byte, position + 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, position + 1
    raise SnapshotCopyError("SQLite schema varint is invalid")


def _sqlite_serial_size(serial_type: int) -> int:
    fixed = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8, 7: 8, 8: 0, 9: 0}
    if serial_type in fixed:
        return fixed[serial_type]
    if serial_type in {10, 11} or serial_type < 0:
        raise SnapshotCopyError("SQLite schema serial type is invalid")
    return (serial_type - 12) // 2


def _sqlite_schema_record_identity(
    payload: bytes, text_encoding: str
) -> tuple[str | None, str | None]:
    header_size, position = _read_sqlite_varint(payload, 0)
    if header_size < position or header_size > len(payload):
        raise SnapshotCopyError("SQLite schema record header is invalid")
    serial_types: list[int] = []
    while position < header_size:
        serial_type, position = _read_sqlite_varint(payload, position)
        if position > header_size:
            raise SnapshotCopyError("SQLite schema record header is truncated")
        serial_types.append(serial_type)
    if len(serial_types) < 2:
        raise SnapshotCopyError("SQLite schema record has too few columns")

    data_position = header_size
    values: list[str | None] = []
    for serial_type in serial_types[:2]:
        size = _sqlite_serial_size(serial_type)
        end = data_position + size
        if end > len(payload):
            raise SnapshotCopyError("SQLite schema record payload is truncated")
        if serial_type >= 13 and serial_type % 2 == 1:
            try:
                values.append(payload[data_position:end].decode(text_encoding))
            except UnicodeDecodeError as exc:
                raise SnapshotCopyError(
                    "SQLite schema record text is invalid"
                ) from exc
        else:
            values.append(None)
        data_position = end
    return values[0], values[1]


def _committed_wal_page_offsets(
    wal: Path,
    *,
    base_page_count: int,
    page_size: int,
) -> tuple[dict[int, int], int | None]:
    if not wal.is_file() or wal.stat().st_size <= 32:
        return {}, None
    frame_size = 24 + page_size
    with wal.open("rb") as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise SnapshotCopyError("WAL header is truncated")
        declared_page_size = struct.unpack(">I", header[8:12])[0]
        if declared_page_size not in {0, page_size}:
            raise SnapshotCopyError("WAL page size does not match the database")
        generation = header[16:24]
        available_frames = max(0, (wal.stat().st_size - 32) // frame_size)
        headers: list[tuple[int, int, int]] = []
        max_page_seen = base_page_count
        last_commit: int | None = None
        committed_pages: int | None = None
        for index in range(available_frames):
            if index == MAX_MESSAGE_SCHEMA_PROBE_WAL_FRAMES:
                raise SnapshotCopyError("message shard WAL probe limit exceeded")
            frame_offset = 32 + index * frame_size
            stream.seek(frame_offset)
            frame_header = stream.read(24)
            if len(frame_header) != 24:
                break
            if frame_header[8:16] != generation:
                # Current-generation WAL frames are a contiguous prefix.  A
                # preallocated tail may still contain zeroes or an old salt.
                break
            page_number, database_size = struct.unpack(">II", frame_header[:8])
            if page_number <= 0:
                break
            max_page_seen = max(max_page_seen, page_number)
            headers.append((page_number, database_size, frame_offset + 24))
            if database_size:
                if page_number > database_size or database_size > max_page_seen:
                    break
                last_commit = len(headers) - 1
                committed_pages = database_size
        if last_commit is None or committed_pages is None:
            return {}, None
        offsets: dict[int, int] = {}
        for page_number, _database_size, page_offset in headers[: last_commit + 1]:
            offsets[page_number] = page_offset
        return offsets, committed_pages


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _sqlite_schema_message_tables_once(
    source: Path,
    master_hex: str,
    *,
    wanted_tables: frozenset[str] | None,
) -> tuple[set[str], int]:
    size = source.stat().st_size
    if size == 0 or size % PAGE_SIZE:
        raise SnapshotCopyError("message shard is not page aligned")
    base_page_count = size // PAGE_SIZE
    wal = source.with_name(source.name + "-wal")
    wal_offsets, committed_pages = _committed_wal_page_offsets(
        wal,
        base_page_count=base_page_count,
        page_size=PAGE_SIZE,
    )
    with source.open("rb") as database:
        first_encrypted = database.read(PAGE_SIZE)
        if len(first_encrypted) != PAGE_SIZE:
            raise SnapshotCopyError("message shard first page is incomplete")
        encrypted = not first_encrypted.startswith(b"SQLite format 3\x00")
        codec = (
            EncryptedPageCodec(
                master_hex=master_hex,
                salt=first_encrypted[:16],
                hmac_mode="sqlcipher",
                plaintext_header=False,
            )
            if encrypted
            else None
        )
        wal_stream = wal.open("rb") if wal_offsets else None
        pages: dict[int, bytes] = {}
        try:
            def read_page(page_number: int) -> bytes:
                if page_number in pages:
                    return pages[page_number]
                if len(pages) == MAX_MESSAGE_SCHEMA_PROBE_PAGES_PER_SHARD:
                    raise SnapshotCopyError("message shard schema probe limit exceeded")
                if page_number <= 0 or page_number > (committed_pages or base_page_count):
                    raise SnapshotCopyError("SQLite schema page is outside the database")
                if page_number in wal_offsets:
                    if wal_stream is None:  # pragma: no cover - defensive
                        raise SnapshotCopyError("WAL page stream is unavailable")
                    wal_stream.seek(wal_offsets[page_number])
                    raw = wal_stream.read(PAGE_SIZE)
                else:
                    database.seek((page_number - 1) * PAGE_SIZE)
                    raw = database.read(PAGE_SIZE)
                if len(raw) != PAGE_SIZE:
                    raise SnapshotCopyError("SQLite schema page is truncated")
                clear = codec.decrypt_page(page_number, raw) if codec else raw
                pages[page_number] = clear
                return clear

            page_one = read_page(1)
            if page_one[:16] != b"SQLite format 3\x00":
                raise SnapshotCopyError("message shard has no SQLite header")
            reserved = int(page_one[20])
            usable_size = PAGE_SIZE - reserved
            if usable_size < 480:
                raise SnapshotCopyError("SQLite schema usable page size is invalid")
            text_encoding = {1: "utf-8", 2: "utf-16le", 3: "utf-16be"}.get(
                struct.unpack(">I", page_one[56:60])[0]
            )
            if text_encoding is None:
                raise SnapshotCopyError("SQLite schema text encoding is unsupported")

            pending = [1]
            visited: set[int] = set()
            table_names: set[str] = set()
            while pending:
                page_number = pending.pop()
                if page_number in visited:
                    raise SnapshotCopyError("SQLite schema B-tree contains a cycle")
                visited.add(page_number)
                page = read_page(page_number)
                header_offset = 100 if page_number == 1 else 0
                page_type = page[header_offset]
                if page_type not in {0x05, 0x0D}:
                    raise SnapshotCopyError("SQLite schema B-tree page type is invalid")
                cell_count = struct.unpack(">H", page[header_offset + 3 : header_offset + 5])[0]
                header_size = 12 if page_type == 0x05 else 8
                pointer_start = header_offset + header_size
                pointer_end = pointer_start + cell_count * 2
                if pointer_end > usable_size:
                    raise SnapshotCopyError("SQLite schema cell pointers are invalid")
                cell_offsets = [
                    struct.unpack(">H", page[offset : offset + 2])[0]
                    for offset in range(pointer_start, pointer_end, 2)
                ]
                if page_type == 0x05:
                    children: list[int] = []
                    for cell_offset in cell_offsets:
                        if cell_offset <= 0 or cell_offset + 4 > usable_size:
                            raise SnapshotCopyError("SQLite schema interior cell is invalid")
                        children.append(
                            struct.unpack(">I", page[cell_offset : cell_offset + 4])[0]
                        )
                    children.append(
                        struct.unpack(
                            ">I", page[header_offset + 8 : header_offset + 12]
                        )[0]
                    )
                    if any(
                        child <= 0 or child > (committed_pages or base_page_count)
                        for child in children
                    ):
                        raise SnapshotCopyError("SQLite schema child page is invalid")
                    # Pop the right-most (newest schema rows) first.
                    pending.extend(children)
                    continue

                max_local = usable_size - 35
                min_local = ((usable_size - 12) * 32 // 255) - 23
                for cell_offset in cell_offsets:
                    if cell_offset <= 0 or cell_offset >= usable_size:
                        raise SnapshotCopyError("SQLite schema leaf cell is invalid")
                    payload_size, position = _read_sqlite_varint(page, cell_offset)
                    _rowid, position = _read_sqlite_varint(page, position)
                    local_size = payload_size
                    if payload_size > max_local:
                        local_size = min_local + (
                            (payload_size - min_local) % (usable_size - 4)
                        )
                        if local_size > max_local:
                            local_size = min_local
                    payload_end = position + local_size
                    if payload_end > usable_size:
                        raise SnapshotCopyError("SQLite schema cell payload is invalid")
                    record_type, record_name = _sqlite_schema_record_identity(
                        page[position:payload_end], text_encoding
                    )
                    if (
                        record_type != "table"
                        or record_name is None
                        or re.fullmatch(r"Msg_[0-9a-f]{32}", record_name) is None
                    ):
                        continue
                    if wanted_tables is None:
                        table_names.add(record_name)
                    elif record_name in wanted_tables:
                        table_names.add(record_name)
                        if table_names == wanted_tables:
                            return table_names, len(pages)
            return table_names, len(pages)
        finally:
            if wal_stream is not None:
                wal_stream.close()


def _sqlite_schema_contains_table_once(
    source: Path,
    table: str,
    master_hex: str,
) -> tuple[bool, int]:
    table_names, pages_read = _sqlite_schema_message_tables_once(
        source,
        master_hex,
        wanted_tables=frozenset({table}),
    )
    return table in table_names, pages_read


def _sqlite_schema_contains_table(
    source: Path,
    table: str,
    *,
    master_hex: str,
) -> tuple[bool, int]:
    if re.fullmatch(r"Msg_[0-9a-f]{32}", table) is None:
        raise SnapshotCopyError("message table name is invalid")
    wal = source.with_name(source.name + "-wal")
    last_error: SnapshotCopyError | None = None
    for _attempt in range(3):
        before = (_file_signature(source), _file_signature(wal))
        try:
            result = _sqlite_schema_contains_table_once(source, table, master_hex)
        except SnapshotCopyError as exc:
            last_error = exc
            if before == (_file_signature(source), _file_signature(wal)):
                raise
            continue
        if before == (_file_signature(source), _file_signature(wal)):
            return result
    raise SnapshotCopyError(
        "message shard changed throughout bounded schema probe retries"
    ) from last_error


def _sqlite_schema_message_table_catalog(
    source: Path,
    *,
    master_hex: str,
) -> tuple[frozenset[str], int]:
    """Enumerate one shard's message tables through a stable schema-only read."""

    wal = source.with_name(source.name + "-wal")
    last_error: SnapshotCopyError | None = None
    for _attempt in range(3):
        before = (_file_signature(source), _file_signature(wal))
        try:
            tables, pages_read = _sqlite_schema_message_tables_once(
                source,
                master_hex,
                wanted_tables=None,
            )
        except SnapshotCopyError as exc:
            last_error = exc
            if before == (_file_signature(source), _file_signature(wal)):
                raise
            continue
        if before == (_file_signature(source), _file_signature(wal)):
            return frozenset(tables), pages_read
    raise SnapshotCopyError(
        "message shard changed throughout bounded schema catalog retries"
    ) from last_error


_TYPE_NAMES = {
    1: "text",
    3: "image",
    34: "voice",
    43: "video",
    47: "emoji",
    48: "location",
    49: "app",
    50: "call",
    10000: "system",
}
_OUTGOING_MESSAGE_STATUS = 2
_INCOMING_MESSAGE_STATUS = 4
_CALIBRATED_MESSAGE_STATUSES = {0, 3, 5}


def _base_message_type(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number in _TYPE_NAMES:
        return number
    low = number & 0xFF
    return low if number > 0xFFFF and low in _TYPE_NAMES else number


def _message_sender_role(
    status: object,
    message_type: int | None,
    sender_id: object,
    calibrated_self_sender: str | None,
) -> tuple[str, str, bool | None]:
    """Return only mechanically proven role/direction values.

    Formal WCDB snapshots establish status 2 as outgoing and status 4 as
    incoming for both configured accounts.  A message shard may additionally
    calibrate one dominant self sender from its non-system status-2 rows.  In
    that shard only, statuses 0/3/5 can use the calibrated sender; all other
    cases remain unknown.  System is a message-type fact and always wins.
    """

    if message_type == 10000:
        return "system", "system", False
    try:
        native_status = int(status)
    except (TypeError, ValueError, OverflowError):
        return "unknown", "unknown", None
    if native_status == _OUTGOING_MESSAGE_STATUS:
        return "self", "outgoing", True
    if native_status == _INCOMING_MESSAGE_STATUS:
        return "other", "incoming", False
    sender_key = _valid_sender_key(sender_id)
    if (
        native_status in _CALIBRATED_MESSAGE_STATUSES
        and calibrated_self_sender is not None
        and sender_key is not None
    ):
        if sender_key == calibrated_self_sender:
            return "self", "outgoing", True
        return "other", "incoming", False
    return "unknown", "unknown", None


def _valid_sender_key(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return str(number) if number > 0 else None


def _text_from_message(value: object, message_type: int | None) -> str | None:
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            text = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            text = ""
            if raw[:4] == b"\x28\xb5\x2f\xfd":
                for offset in (10, *range(16)):
                    chunk = raw[offset:].split(b"\x01\x00", 1)[0]
                    try:
                        candidate = chunk.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    candidate = re.sub(r"[\x00-\x1f\x7f]+", "", candidate).strip()
                    if candidate:
                        text = candidate
                        break
    else:
        return None
    if not text:
        return None
    if message_type in {1, 10000}:
        return _safe_plain_text(text)
    if message_type == 50:
        call_status = _safe_plain_text(text)
        if (
            call_status
            and len(call_status) <= 128
            and not re.fullmatch(r"[0-9a-fA-F]{32,}", call_status)
        ):
            return call_status
        return None
    if message_type == 49:
        fields = []
        for tag in ("title", "des", "url", "filename"):
            match = re.search(fr"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", text, re.S)
            field = _safe_plain_text(match.group(1)) if match else None
            if field:
                fields.append(field)
        return "\n".join(dict.fromkeys(fields)) or None
    return None


def _safe_plain_text(value: object) -> str | None:
    """Reject binary/control projections without rewriting their meaning."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(
        unicodedata.category(character) == "Cc"
        and character not in "\n\r\t"
        for character in text
    ):
        return None
    return text


def _readable_payload_text(value: object) -> str | None:
    if isinstance(value, str):
        return _safe_plain_text(value)
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(value)
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return _safe_plain_text(text)


def _decompress_message_text(value: object) -> tuple[str | None, str | None]:
    """Return bounded zstd text plus a body-free gap code when it is unreadable."""

    if value in (None, b"", ""):
        return None, None
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return None, "compressed_content_invalid"
    raw = bytes(value)
    magic = b"\x28\xb5\x2f\xfd"
    offset = raw.find(magic, 0, 64)
    if offset < 0:
        return None, "compressed_content_format_unsupported"
    try:
        from compression import zstd
    except ImportError:  # pragma: no cover - Python before 3.14
        return None, "compressed_content_decoder_unavailable"
    try:
        decoder = zstd.ZstdDecompressor()
        clear = decoder.decompress(
            raw[offset:], max_length=MAX_DECOMPRESSED_MESSAGE_BYTES + 1
        )
    except (MemoryError, zstd.ZstdError):
        return None, "compressed_content_decode_failed"
    if len(clear) > MAX_DECOMPRESSED_MESSAGE_BYTES or not decoder.eof:
        return None, "compressed_content_exceeds_limit"
    try:
        text = clear.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None, "compressed_content_text_invalid"
    return (text or None), (None if text else "compressed_content_empty")


def _message_value_text(value: object) -> tuple[str | None, str | None]:
    """Read plain UTF-8 or one bounded zstd payload without guessing bytes."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if raw.find(b"\x28\xb5\x2f\xfd", 0, 64) >= 0:
            return _decompress_message_text(raw)
    return _readable_payload_text(value), None


def _message_payload_texts(
    row: sqlite3.Row,
) -> tuple[list[str], list[str], str | None]:
    body_texts: list[str] = []
    message_text, message_gap = _message_value_text(row["message_content"])
    if message_text:
        body_texts.append(message_text)
    compressed, compressed_gap = _message_value_text(row["compress_content"])
    if compressed and compressed not in body_texts:
        body_texts.append(compressed)
    all_texts = list(body_texts)
    for column in ("source", "packed_info_data", "origin_source"):
        text = _readable_payload_text(row[column])
        if text and text not in all_texts:
            all_texts.append(text)
    return body_texts, all_texts, message_gap or compressed_gap


def _message_content_projection(
    row: sqlite3.Row, message_type: int | None
) -> tuple[str | None, list[str], str | None]:
    """Project one bounded public body while retaining all relation payloads.

    Call event rows keep their human-readable outcome in ``source`` rather
    than ``message_content``.  Read only the dedicated source fields for that
    type so statuses such as an unanswered call or a call duration survive the
    public projection without exposing the opaque native body.
    """

    body_texts, payload_texts, compressed_gap = _message_payload_texts(row)
    if message_type == 49:
        content_texts = payload_texts
    elif message_type == 50:
        content_texts = []
        for column in ("source", "origin_source"):
            text = _readable_payload_text(row[column])
            if text and text not in content_texts:
                content_texts.append(text)
    else:
        content_texts = body_texts
    content = next(
        (
            candidate
            for candidate in (
                _text_from_message(text, message_type) for text in content_texts
            )
            if candidate
        ),
        None,
    )
    return content, payload_texts, compressed_gap


def _quote_identity(value: object) -> str | None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8", errors="ignore")
    if not isinstance(value, str) or "refermsg" not in value:
        return None
    match = re.search(
        r"<refermsg>.*?<(?:svrid|msgid|newmsgid)>(?:<!\[CDATA\[)?([^<\]]+)",
        value,
        re.S,
    )
    return match.group(1).strip() if match else None


def _quote_identities(value: object) -> set[str]:
    """Return every readable native quote id without choosing a winner."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8", errors="ignore")
    if not isinstance(value, str) or "refermsg" not in value:
        return set()
    return {
        match.group(1).strip()
        for match in re.finditer(
            r"<refermsg>.*?<(?:svrid|msgid|newmsgid)>(?:<!\[CDATA\[)?([^<\]]+)",
            value,
            re.S,
        )
        if match.group(1).strip()
    }


def _iter_cursor_pages(
    cursor: sqlite3.Cursor, *, page_size: int = MESSAGE_FETCH_PAGE_SIZE
) -> Iterator[list[sqlite3.Row]]:
    """Yield a query cursor in a fixed-size reader-local page."""

    while page := cursor.fetchmany(page_size):
        yield page


class DirectWeChatReader:
    """Source-local WeChat reader with no helper process or HTTP dependency."""

    def __init__(
        self,
        *,
        config_path: Path | str,
        local_state_path: Path | str,
        snapshot_cutoff_s: int | None = None,
    ):
        account_root, master_hex, identity = load_direct_source_identity(
            config_path, local_state_path
        )
        self._config_path = Path(config_path)
        self._local_state_path = Path(local_state_path)
        self._storage = account_root / "db_storage"
        if not self._storage.is_dir():
            raise SnapshotCopyError("local account database storage is missing")
        self._master_hex = master_hex
        self._identity = identity
        self.account_identity_commitment = hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()
        self.snapshot_cutoff_s = int(snapshot_cutoff_s or time.time())
        self._temporary = tempfile.TemporaryDirectory(prefix="wechat-direct-")
        self._prepared: dict[Path, Path] = {}
        self._connections: dict[Path, sqlite3.Connection] = {}
        self._message_connections_cache: list[tuple[Path, sqlite3.Connection]] | None = None
        self._message_table_sources_cache: dict[str, tuple[Path, ...]] = {}
        self._message_schema_probe_pages: dict[Path, int] = {}
        self._message_self_sender_cache: dict[tuple[Path, str | None], str | None] = {}
        self._sender_index_cache: dict[int, str] | None = None
        self._sender_index_by_message_directory_cache: (
            dict[Path, dict[int, str]] | None
        ) = None
        self._resource_index_cache: (
            dict[tuple[str, str, str], list[dict[str, Any]]] | None
        ) = None
        self._voice_index_cache: (
            dict[tuple[str, str, str], list[dict[str, Any]]] | None
        ) = None

    def __repr__(self) -> str:
        return "DirectWeChatReader(<private local databases>)"

    @property
    def self_native_id(self) -> str:
        """Return the account's source-proven native identity."""

        return self._identity

    @property
    def moments_self_native_id(self) -> str:
        """Return the native Moments author identity for this account.

        The configured database identity may carry a trailing underscore plus
        four hexadecimal characters that distinguishes the local account
        storage root.  Moments stores the same identity without that storage
        suffix.  Keep the unsuffixed form unchanged for compatible sources.
        """

        match = re.fullmatch(r"(.+)_([0-9a-fA-F]{4})", self._identity)
        return match.group(1) if match else self._identity

    def __enter__(self) -> "DirectWeChatReader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        for connection in self._connections.values():
            connection.close()
        self._connections.clear()
        self._temporary.cleanup()

    def _database_files(self) -> list[Path]:
        result = []
        for root, directories, files in os.walk(self._storage):
            directories[:] = [name for name in directories if name.casefold() != "migrate"]
            for name in files:
                if name.casefold().endswith(".db"):
                    result.append(Path(root) / name)
        return sorted(result, key=lambda item: item.relative_to(self._storage).as_posix())

    @staticmethod
    def _signature(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return stat.st_size, stat.st_mtime_ns

    def _prepare(self, source: Path) -> Path:
        cached = self._prepared.get(source)
        if cached is not None:
            return cached
        wal = source.with_name(source.name + "-wal")
        relative = source.relative_to(self._storage).as_posix()
        destination = Path(self._temporary.name) / (hashlib.sha256(relative.encode()).hexdigest() + ".sqlite3")
        for _attempt in range(3):
            before = (self._signature(source), self._signature(wal))
            try:
                with source.open("rb") as stream:
                    first_page = stream.read(PAGE_SIZE)
                if len(first_page) != PAGE_SIZE:
                    raise SnapshotCopyError("local database first page is incomplete")
                if first_page.startswith(b"SQLite format 3\x00"):
                    shutil.copy2(source, destination)
                else:
                    codec = EncryptedPageCodec(
                        master_hex=self._master_hex,
                        salt=first_page[:16],
                        hmac_mode="sqlcipher",
                        plaintext_header=False,
                    )
                    codec.decrypt_database(source, destination)
                    if wal.is_file() and wal.stat().st_size > 32:
                        codec.merge_wal(destination, wal)
            except (OSError, EncryptedPageError, sqlite3.DatabaseError) as exc:
                raise SnapshotCopyError("local database snapshot could not be prepared") from exc
            after = (self._signature(source), self._signature(wal))
            if before == after:
                connection = sqlite3.connect(f"file:{quote(str(destination), safe='/:\\\\')}?mode=ro", uri=True)
                try:
                    if str(connection.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
                        raise SnapshotCopyError("local database snapshot integrity failed")
                finally:
                    connection.close()
                self._prepared[source] = destination
                return destination
        raise SnapshotCopyError("local database changed throughout bounded snapshot retries")

    def _open(self, source: Path) -> sqlite3.Connection:
        cached = self._connections.get(source)
        if cached is not None:
            return cached
        snapshot = self._prepare(source)
        connection = sqlite3.connect(
            f"file:{quote(str(snapshot), safe='/:\\\\')}?mode=ro", uri=True, timeout=5.0
        )
        connection.row_factory = sqlite3.Row
        connection.text_factory = lambda data: data.decode("utf-8") if _is_utf8(data) else data
        connection.execute("PRAGMA query_only = ON")
        self._connections[source] = connection
        return connection

    def _named_databases(self, name: str) -> list[Path]:
        folded = name.casefold()
        return [path for path in self._database_files() if path.name.casefold() == folded]

    def _message_database_sources(self) -> list[Path]:
        return [
            path
            for path in self._database_files()
            if re.fullmatch(r"message_\d+\.db", path.name, re.I)
            and path.parent.name.casefold() == "message"
        ]

    def _metadata_fingerprint(self, sources: Iterable[Path]) -> dict[str, Any]:
        """Hash source file metadata without reading or exposing private bodies."""

        entries: list[tuple[str, int, int]] = []
        for source in sorted(set(sources), key=lambda item: item.as_posix()):
            for candidate in (source, source.with_name(source.name + "-wal")):
                signature = self._signature(candidate)
                if signature is None:
                    continue
                size, modified_ns = signature
                entries.append(
                    (
                        candidate.relative_to(self._storage).as_posix(),
                        int(size),
                        int(modified_ns),
                    )
                )
        encoded = json.dumps(
            entries,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
            "fileCount": len(entries),
            "bytes": sum(item[1] for item in entries),
            "latestMtimeNs": max((item[2] for item in entries), default=None),
        }

    def contact_source_fingerprint(self, session_native_id: str) -> dict[str, Any]:
        """Return a fast, body-free change fingerprint for one conversation."""

        if not isinstance(session_native_id, str) or not session_native_id:
            raise ValueError("contact_source_identity_invalid")
        table = "Msg_" + hashlib.md5(
            session_native_id.encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        message_sources = self._message_sources_for_table(table)
        source_catalog = [
            source.relative_to(self._storage).as_posix()
            for source in message_sources
        ]
        source_catalog_sha256 = "sha256:" + hashlib.sha256(
            json.dumps(
                source_catalog, ensure_ascii=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        sources: set[Path] = set(message_sources)
        for source in message_sources:
            resource = source.parent / "message_resource.db"
            if resource.is_file():
                sources.add(resource)
            sources.update(source.parent.glob("media_*.db"))
        # Contact labels may legitimately change the human-readable export.
        # Session databases, however, advance for unrelated conversations and
        # must not invalidate one selected contact's no-change fast path.
        sources.update(self._named_databases("contact.db"))
        result = self._metadata_fingerprint(sources)
        result.update(
            {
                "kind": "wechat-contact-source-metadata.v1",
                "messageSourceCount": len(message_sources),
                "messageSourceCatalogSha256": source_catalog_sha256,
                "schemaProbePages": sum(
                    self._message_schema_probe_pages.get(source, 0)
                    for source in self._message_database_sources()
                ),
            }
        )
        return result

    def moments_source_fingerprint(self) -> dict[str, Any]:
        """Return a body-free change fingerprint for the visible Moments cache."""

        sources: set[Path] = set(self._named_databases("sns.db"))
        sources.update(self._named_databases("contact.db"))
        result = self._metadata_fingerprint(sources)
        result.update(
            {
                "kind": "wechat-moments-source-metadata.v1",
                "snsSourceCount": len(self._named_databases("sns.db")),
            }
        )
        return result

    def _message_sources_for_table(self, table: str) -> tuple[Path, ...]:
        cached = self._message_table_sources_cache.get(table)
        if cached is not None:
            return cached
        for _attempt in range(3):
            candidates = self._message_database_sources()
            before = {
                source: (
                    _file_signature(source),
                    _file_signature(source.with_name(source.name + "-wal")),
                )
                for source in candidates
            }
            selected: list[Path] = []
            probe_pages: dict[Path, int] = {}
            for source in candidates:
                exists, pages_read = _sqlite_schema_contains_table(
                    source,
                    table,
                    master_hex=self._master_hex,
                )
                probe_pages[source] = pages_read
                if exists:
                    selected.append(source)
            after_candidates = self._message_database_sources()
            after = {
                source: (
                    _file_signature(source),
                    _file_signature(source.with_name(source.name + "-wal")),
                )
                for source in after_candidates
            }
            if candidates == after_candidates and before == after:
                result = tuple(selected)
                self._message_schema_probe_pages.update(probe_pages)
                self._message_table_sources_cache[table] = result
                return result
        raise SnapshotCopyError(
            "message shards changed throughout bounded catalog retries"
        )

    def prepare_message_catalog(
        self, session_native_ids: Iterable[str]
    ) -> dict[str, tuple[Path, ...]]:
        """Cache all requested ``Msg_<md5>`` origins with one schema pass/shard.

        Complete source runs know their session set up front.  Cataloguing those
        hashes once prevents every session from re-reading the encrypted schema
        pages of every message shard.  Ordinary one-session readers keep using
        ``_message_sources_for_table`` and its sparse probe unchanged.
        """

        requested_tables: set[str] = set()
        for session_native_id in session_native_ids:
            if not isinstance(session_native_id, str) or not session_native_id:
                raise ValueError("message_catalog_session_identity_invalid")
            requested_tables.add(
                "Msg_"
                + hashlib.md5(
                    session_native_id.encode("utf-8"), usedforsecurity=False
                ).hexdigest()
            )
        missing_tables = {
            table
            for table in requested_tables
            if table not in self._message_table_sources_cache
        }
        if not missing_tables:
            return {
                table: self._message_table_sources_cache[table]
                for table in sorted(requested_tables)
            }

        for _attempt in range(3):
            candidates = self._message_database_sources()
            before = {
                source: (
                    _file_signature(source),
                    _file_signature(source.with_name(source.name + "-wal")),
                )
                for source in candidates
            }
            sources_by_table = {table: [] for table in missing_tables}
            probe_pages: dict[Path, int] = {}
            for source in candidates:
                tables, pages_read = _sqlite_schema_message_table_catalog(
                    source,
                    master_hex=self._master_hex,
                )
                probe_pages[source] = pages_read
                for table in tables & missing_tables:
                    sources_by_table[table].append(source)
            after_candidates = self._message_database_sources()
            after = {
                source: (
                    _file_signature(source),
                    _file_signature(source.with_name(source.name + "-wal")),
                )
                for source in after_candidates
            }
            if candidates != after_candidates or before != after:
                continue
            for table, sources in sources_by_table.items():
                self._message_table_sources_cache[table] = tuple(sources)
            self._message_schema_probe_pages.update(probe_pages)
            return {
                table: self._message_table_sources_cache[table]
                for table in sorted(requested_tables)
            }
        raise SnapshotCopyError(
            "message shards changed throughout bounded bulk catalog retries"
        )

    def _message_connections(
        self, table: str | None = None
    ) -> list[tuple[Path, sqlite3.Connection]]:
        if table is not None:
            return [
                (path, self._open(path))
                for path in self._message_sources_for_table(table)
            ]
        if self._message_connections_cache is None:
            self._message_connections_cache = [
                (path, self._open(path)) for path in self._message_database_sources()
            ]
        return self._message_connections_cache

    def _calibrated_self_sender(
        self,
        source: Path,
        connection: sqlite3.Connection,
        *,
        message_table: str | None = None,
    ) -> str | None:
        cache_key = (source, message_table)
        if cache_key in self._message_self_sender_cache:
            return self._message_self_sender_cache[cache_key]
        counts: dict[str, int] = {}
        total_samples = 0
        try:
            if message_table is not None:
                tables = [message_table]
            else:
                tables = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name LIKE 'Msg_%'"
                    )
                ]
            for table in tables:
                quoted_table = _quote_identifier(table)
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        f"PRAGMA table_info({quoted_table})"
                    )
                }
                if not {"status", "real_sender_id", "local_type"} <= columns:
                    continue
                for sender_id, local_type, count in connection.execute(
                    "SELECT real_sender_id, local_type, count(*) "
                    f"FROM {quoted_table} WHERE status=? "
                    "GROUP BY real_sender_id, local_type",
                    (_OUTGOING_MESSAGE_STATUS,),
                ):
                    if _base_message_type(local_type) == 10000:
                        continue
                    sample_count = int(count)
                    total_samples += sample_count
                    sender_key = _valid_sender_key(sender_id)
                    if sender_key is not None:
                        counts[sender_key] = counts.get(sender_key, 0) + sample_count
        except sqlite3.DatabaseError:
            self._message_self_sender_cache[cache_key] = None
            return None

        # A sender receipt is an authorization boundary, not a classifier.
        # Two status=2 sender IDs are conflicting source evidence even if one
        # appears much more often; a majority vote would silently turn an
        # ambiguous row into ``self``.  Keep the cache but require exactly one
        # valid outgoing sender across this physical source.
        calibrated: str | None = None
        if len(counts) == 1 and total_samples > 0:
            calibrated = next(iter(counts))
        self._message_self_sender_cache[cache_key] = calibrated
        return calibrated

    @staticmethod
    def _opaque_sha256_commitment(*parts: str) -> str:
        return "sha256:" + hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()

    @staticmethod
    def _is_opaque_sha256_commitment(value: object) -> bool:
        return isinstance(value, str) and re.fullmatch(
            r"sha256:[0-9a-f]{64}", value
        ) is not None

    def _group_self_sender_receipt(
        self,
        *,
        shard_key: str,
        calibrated_sender: str | None,
    ) -> dict[str, str | None]:
        """Build an opaque, shard-bound sender calibration receipt.

        ``calibrated_sender`` never leaves this reader.  ``None`` is a
        deliberate unproven state: known native directions can still progress,
        while a later ambiguous native status fails closed rather than causing
        a fresh all-history calibration during a normal increment.
        """

        account_commitment = "sha256:" + self.account_identity_commitment
        sender_commitment = (
            self._opaque_sha256_commitment(
                GROUP_SELF_SENDER_RECEIPT_ALGORITHM,
                account_commitment,
                shard_key,
                calibrated_sender,
            )
            if calibrated_sender is not None
            else None
        )
        receipt_commitment = self._opaque_sha256_commitment(
            GROUP_SELF_SENDER_RECEIPT_ALGORITHM,
            account_commitment,
            shard_key,
            sender_commitment or "unproven",
        )
        return {
            "algorithm": GROUP_SELF_SENDER_RECEIPT_ALGORITHM,
            "accountIdentityCommitment": account_commitment,
            "selfSenderCommitment": sender_commitment,
            "receiptCommitment": receipt_commitment,
        }

    def _validated_group_self_sender_receipt(
        self,
        *,
        shard_key: str,
        value: object,
    ) -> dict[str, str | None] | None:
        """Return an exact opaque receipt or ``None`` on any drift/tampering."""

        if not isinstance(value, Mapping) or set(value) != {
            "algorithm",
            "accountIdentityCommitment",
            "selfSenderCommitment",
            "receiptCommitment",
        }:
            return None
        algorithm = value.get("algorithm")
        account_commitment = value.get("accountIdentityCommitment")
        sender_commitment = value.get("selfSenderCommitment")
        receipt_commitment = value.get("receiptCommitment")
        expected_account = "sha256:" + self.account_identity_commitment
        if (
            algorithm != GROUP_SELF_SENDER_RECEIPT_ALGORITHM
            or account_commitment != expected_account
            or not self._is_opaque_sha256_commitment(receipt_commitment)
            or (
                sender_commitment is not None
                and not self._is_opaque_sha256_commitment(sender_commitment)
            )
        ):
            return None
        expected_receipt = self._opaque_sha256_commitment(
            GROUP_SELF_SENDER_RECEIPT_ALGORITHM,
            expected_account,
            shard_key,
            sender_commitment or "unproven",
        )
        if not hmac.compare_digest(str(receipt_commitment), expected_receipt):
            return None
        return {
            "algorithm": GROUP_SELF_SENDER_RECEIPT_ALGORITHM,
            "accountIdentityCommitment": expected_account,
            "selfSenderCommitment": sender_commitment,
            "receiptCommitment": expected_receipt,
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all local sessions, including hidden groups, for source truth."""

        sessions: dict[str, dict[str, Any]] = {}
        sources = self._named_databases("session.db")
        if not sources:
            raise DirectSchemaError("session_database_unavailable")
        invalid_source = False
        usable_sources = 0
        for source in sources:
            connection = self._open(source)
            try:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(SessionTable)"
                    )
                }
                if "username" not in columns:
                    invalid_source = True
                    continue
                usable_sources += 1
                type_expr = "type" if "type" in columns else "NULL"
                last_expr = "last_timestamp" if "last_timestamp" in columns else "NULL"
                sort_expr = "sort_timestamp" if "sort_timestamp" in columns else "NULL"
                hidden_expr = "is_hidden" if "is_hidden" in columns else "0"
                order = (
                    "ORDER BY sort_timestamp DESC"
                    if "sort_timestamp" in columns
                    else ""
                )
                for row in connection.execute(
                    f"SELECT username, {type_expr} AS session_type, "
                    f"{last_expr} AS last_timestamp, {sort_expr} AS sort_timestamp, "
                    f"{hidden_expr} AS is_hidden FROM SessionTable {order}"
                ):
                    if row["username"]:
                        username = str(row["username"])
                        folded_username = username.casefold()
                        session_type = (
                            "channel"
                            if folded_username.startswith("gh_")
                            else "group"
                            if folded_username.endswith("@chatroom")
                            else "contact"
                        )
                        sessions[username] = {
                            "id": username,
                            "username": username,
                            "type": session_type,
                            "nativeType": row["session_type"],
                            "lastTimestamp": row["last_timestamp"],
                            "sortTimestamp": row["sort_timestamp"],
                            "isHidden": bool(row["is_hidden"]),
                        }
            except sqlite3.DatabaseError:
                invalid_source = True
        if invalid_source or not usable_sources:
            raise DirectSchemaError("session_database_unavailable")

        def sort_key(item: Mapping[str, Any]) -> tuple[bool, int, str]:
            raw_timestamp = item.get("sortTimestamp")
            try:
                timestamp = int(raw_timestamp)
            except (TypeError, ValueError, OverflowError):
                timestamp = -1
            return (
                raw_timestamp is not None,
                timestamp,
                str(item.get("id") or ""),
            )

        return sorted(sessions.values(), key=sort_key, reverse=True)

    def list_contacts(
        self, *, include_unregistered: bool = False
    ) -> list[dict[str, Any]]:
        """Return native contact labels, normally limited to chat sessions.

        This is a source lookup only.  It does not infer a relationship or
        choose among duplicate names; callers must treat every non-unique
        match as ambiguous.  include_unregistered is used only by the Moments
        selector because a cached publisher need not have a current chat
        session.
        """

        session_gap: str | None = None
        try:
            session_items = self.list_sessions()
        except DirectSchemaError:
            if not include_unregistered:
                raise
            session_items = []
            session_gap = "session_database_unavailable"
        sessions = {
            str(item["id"]): item for item in session_items if item.get("id")
        }
        contacts: dict[str, dict[str, Any]] = {}
        contact_sources = self._named_databases("contact.db")
        contact_gap: str | None = (
            None if contact_sources else "contact_database_unavailable"
        )
        for source in contact_sources:
            connection = self._open(source)
            try:
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(contact)")
                }
                if "username" not in columns:
                    contact_gap = "contact_database_unavailable"
                    continue
                missing_label_columns = tuple(
                    column
                    for column in ("remark", "nick_name", "alias")
                    if column not in columns
                )
                label_expressions = [
                    column if column in columns else f"NULL AS {column}"
                    for column in ("alias", "remark", "nick_name")
                ]
                rows = connection.execute(
                    "SELECT username, "
                    + ", ".join(label_expressions)
                    + " FROM contact"
                )
                for row in rows:
                    native_id = str(row["username"] or "")
                    session = sessions.get(native_id)
                    if not native_id or (
                        session is None and not include_unregistered
                    ):
                        continue
                    remark = str(row["remark"] or "").strip()
                    nickname = str(row["nick_name"] or "").strip()
                    alias = str(row["alias"] or "").strip()
                    contact = {
                        "nativeId": native_id,
                        "sessionType": (
                            str(session.get("type") or "unknown")
                            if session is not None
                            else "contact"
                        ),
                        "displayName": remark or nickname or alias or native_id,
                        "remark": remark or None,
                        "nickname": nickname or None,
                        "alias": alias or None,
                        "lastTimestamp": (
                            session.get("lastTimestamp")
                            if session is not None
                            else None
                        ),
                    }
                    if missing_label_columns:
                        contact["labelGap"] = "contact_label_fields_unavailable"
                    elif not (remark or nickname or alias):
                        contact["labelGap"] = "contact_label_unavailable"
                    if session_gap:
                        contact["sessionGap"] = session_gap
                    contacts[native_id] = contact
            except sqlite3.DatabaseError:
                contact_gap = "contact_database_unavailable"

        if contact_gap and not include_unregistered:
            raise DirectSchemaError(contact_gap)
        if contact_gap and include_unregistered:
            for contact in contacts.values():
                contact.setdefault("labelGap", "contact_database_incomplete")

        for native_id, session in sessions.items():
            fallback = {
                "nativeId": native_id,
                "sessionType": str(session.get("type") or "unknown"),
                "displayName": native_id,
                "remark": None,
                "nickname": None,
                "alias": None,
                "lastTimestamp": session.get("lastTimestamp"),
                "labelGap": contact_gap or "contact_row_unavailable",
            }
            if session_gap:
                fallback["sessionGap"] = session_gap
            contacts.setdefault(native_id, fallback)
        return list(contacts.values())

    def list_group_member_labels(
        self, session_native_id: str
    ) -> list[dict[str, Any]]:
        """Read current labels only for one selected local chatroom."""

        if not isinstance(session_native_id, str) or not session_native_id.casefold().endswith(
            "@chatroom"
        ):
            raise ValueError("group_member_labels_require_chatroom")
        candidates: dict[str, list[dict[str, Any]]] = {}
        for source in self._named_databases("contact.db"):
            connection = self._open(source)
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not {"chat_room", "chatroom_member", "contact"} <= tables:
                    continue
                rows = connection.execute(
                    "SELECT c.username, c.alias, c.remark, c.nick_name "
                    "FROM chat_room r "
                    "JOIN chatroom_member m ON m.room_id=r.id "
                    "JOIN contact c ON c.id=m.member_id "
                    "WHERE r.username=?",
                    (session_native_id,),
                )
                for row in rows:
                    native_id = str(row["username"] or "")
                    if not native_id:
                        continue
                    candidates.setdefault(native_id, []).append(
                        {
                            "remark": str(row["remark"] or "").strip() or None,
                            "nickname": str(row["nick_name"] or "").strip() or None,
                            "alias": str(row["alias"] or "").strip() or None,
                        }
                    )
            except sqlite3.DatabaseError:
                continue
        result: list[dict[str, Any]] = []
        for native_id, labels in sorted(candidates.items()):
            shapes = {
                (item["remark"], item["nickname"], item["alias"])
                for item in labels
            }
            if len(shapes) == 1:
                remark, nickname, alias = next(iter(shapes))
                display_name = remark or nickname or alias
                label_gap = None if display_name else "group_member_label_unavailable"
            else:
                remark = nickname = alias = None
                display_name = None
                label_gap = "group_member_label_conflicting"
            result.append(
                {
                    "nativeId": native_id,
                    "sessionType": "group_member",
                    "displayName": display_name or "群成员（昵称未取到）",
                    "remark": remark,
                    "nickname": nickname,
                    "alias": alias,
                    "labelScope": "selected_group_member_directory",
                    "labelGap": label_gap,
                }
            )
        return result

    def list_moments(
        self,
        *,
        since_s: int,
        end_s: int,
        username: str | None = None,
        limit: int | None = 20,
    ) -> dict[str, Any]:
        """Read a bounded view of the Moments currently cached on this device."""

        if limit is not None:
            limit = int(limit)
        if limit is not None and (limit < 1 or limit > 50):
            raise ValueError("moments_limit_invalid")
        if int(since_s) > int(end_s):
            raise ValueError("moments_time_window_reversed")

        candidates: dict[str, dict[str, Any]] = {}
        gaps: list[dict[str, Any]] = []
        scanned = 0
        visible_cutoff: int | None = None
        target_cached_rows = 0
        target_latest_time_s: int | None = None
        target_earliest_time_s: int | None = None
        database_found = False
        for source in self._named_databases("sns.db"):
            connection = self._open(source)
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='SnsTimeLine'"
            ).fetchone()
            if table is None:
                continue
            database_found = True
            # A person-scoped request must search the complete current local
            # cache before applying its return limit. Truncating the global
            # feed first can falsely report that an older cached post by the
            # selected author does not exist.
            if limit is None or username is not None:
                rows = connection.execute(
                    "SELECT tid, user_name, content FROM SnsTimeLine "
                    "ORDER BY tid DESC"
                )
            else:
                scan_limit = min(max(limit * 8, 100), 500)
                rows = connection.execute(
                    "SELECT tid, user_name, content FROM SnsTimeLine "
                    "ORDER BY tid DESC LIMIT ?",
                    (scan_limit,),
                )
            for row in rows:
                scanned += 1
                raw = str(row["content"] or "")
                try:
                    root = ET.fromstring(raw)
                except (ET.ParseError, ValueError):
                    gaps.append(
                        {
                            "kind": "moment_xml_unreadable",
                            "nativeRowId": str(row["tid"]),
                        }
                    )
                    continue
                timeline = root.find("TimelineObject")
                if timeline is None:
                    gaps.append(
                        {
                            "kind": "moment_timeline_missing",
                            "nativeRowId": str(row["tid"]),
                        }
                    )
                    continue

                def text(path: str) -> str | None:
                    value = timeline.findtext(path)
                    value = str(value or "").strip()
                    return value or None

                native_id = text("id") or str(row["tid"])
                native_username = text("username") or str(row["user_name"] or "")
                try:
                    create_time = int(text("createTime") or 0)
                except ValueError:
                    create_time = 0
                if create_time:
                    visible_cutoff = max(visible_cutoff or create_time, create_time)
                if username is None or native_username == username:
                    target_cached_rows += 1
                    if create_time:
                        target_latest_time_s = max(
                            target_latest_time_s or create_time, create_time
                        )
                        target_earliest_time_s = min(
                            target_earliest_time_s or create_time, create_time
                        )
                if username and native_username != username:
                    continue
                if create_time < int(since_s) or create_time > int(end_s):
                    continue

                media_manifest: list[dict[str, Any]] = []
                for media in timeline.findall("./ContentObject/mediaList/media"):
                    url_element = media.find("url")
                    url = str(url_element.text or "").strip() if url_element is not None else ""
                    size = media.find("size")
                    media_manifest.append(
                        {
                            "kind": "moment_media",
                            "nativeType": str(media.findtext("type") or "") or None,
                            "nativeId": str(media.findtext("id") or "") or None,
                            "declaredBytes": (
                                int(size.attrib.get("totalSize") or 0)
                                if size is not None
                                and str(size.attrib.get("totalSize") or "").isdigit()
                                else None
                            ),
                            "declaredMd5": (
                                str(url_element.attrib.get("md5") or "")
                                if url_element is not None
                                else ""
                                or None
                            ),
                            "locatorSha256": (
                                "sha256:" + hashlib.sha256(url.encode("utf-8")).hexdigest()
                                if url
                                else None
                            ),
                            "openable": False,
                            "open_status": "remote_locator_not_opened",
                        }
                    )
                item = {
                    "nativeId": native_id,
                    "username": native_username,
                    "nickname": str(
                        root.findtext("./LocalExtraInfo/nickname") or ""
                    ).strip()
                    or None,
                    "createTime": create_time or None,
                    "content": text("contentDesc"),
                    "contentType": text("./ContentObject/type"),
                    "title": text("./ContentObject/title"),
                    "description": text("./ContentObject/description"),
                    "media_manifest": media_manifest,
                    "sourceSha256": "sha256:"
                    + hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                }
                previous = candidates.get(native_id)
                if previous is not None and previous != item:
                    raise DirectSchemaError("moment_identity_is_conflicting")
                candidates[native_id] = item
        if not database_found:
            raise DirectSchemaError("moments_database_unavailable")
        ordered = sorted(
            candidates.values(),
            key=lambda item: (int(item.get("createTime") or 0), str(item["nativeId"])),
            reverse=True,
        )
        return {
            "moments": ordered if limit is None else ordered[:limit],
            "sourceVisibleCutoffS": visible_cutoff,
            "scannedRows": scanned,
            "matchedRows": len(ordered),
            "targetCachedRows": target_cached_rows,
            "targetLatestTimeS": target_latest_time_s,
            "targetEarliestTimeS": target_earliest_time_s,
            "hasMoreCurrentCache": False if limit is None else len(ordered) > limit,
            "historyScope": "current_local_cache_only",
            "gaps": gaps,
        }

    def _session_is_registered(self, session_native_id: str) -> bool:
        for source in self._named_databases("session.db"):
            connection = self._open(source)
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='SessionTable'"
            ).fetchone()
            if exists is None:
                continue
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(SessionTable)")
            }
            if "username" not in columns:
                continue
            if connection.execute(
                "SELECT 1 FROM SessionTable WHERE username=? LIMIT 1",
                (session_native_id,),
            ).fetchone():
                return True
        return False

    def _sender_index(self) -> dict[int, str]:
        if self._sender_index_cache is None:
            result: dict[int, str] = {}
            ambiguous: set[int] = set()
            for source in self._named_databases("message_resource.db"):
                connection = self._open(source)
                try:
                    rows = connection.execute(
                        "SELECT rowid, user_name FROM SenderName2Id"
                    )
                    for rowid, username in rows:
                        if username:
                            sender_id = int(rowid)
                            sender = str(username)
                            if sender_id in ambiguous:
                                continue
                            previous = result.get(sender_id)
                            if previous is not None and previous != sender:
                                result.pop(sender_id, None)
                                ambiguous.add(sender_id)
                            else:
                                result[sender_id] = sender
                except sqlite3.DatabaseError:
                    # Sender dictionaries are shard-local metadata.  An old
                    # resource shard without this optional table must not
                    # override the config/root account boundary.
                    continue
            self._sender_index_cache = result
        return self._sender_index_cache

    def _sender_index_for_message_source(self, source: Path) -> dict[int, str]:
        """Return only unambiguous native sender names beside one message shard.

        ``real_sender_id`` is local to a physical message directory.  The
        existing global index is useful for display, but an ID from another
        directory must not change a private message's direction.  This map
        therefore uses only the sibling ``message_resource.db``.
        """

        if self._sender_index_by_message_directory_cache is None:
            self._sender_index_by_message_directory_cache = {}
        cached = self._sender_index_by_message_directory_cache.get(source.parent)
        if cached is not None:
            return cached
        resource_source = source.parent / "message_resource.db"
        if not resource_source.is_file():
            self._sender_index_by_message_directory_cache[source.parent] = {}
            return {}
        try:
            rows = self._open(resource_source).execute(
                "SELECT rowid, user_name FROM SenderName2Id"
            )
            result = {
                int(rowid): str(username)
                for rowid, username in rows
                if username
            }
        except sqlite3.DatabaseError:
            # An optional or unreadable local sender dictionary cannot
            # establish direction for the paired message shard.
            result = {}
        self._sender_index_by_message_directory_cache[source.parent] = result
        return result

    def _resource_index(
        self,
    ) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
        if self._resource_index_cache is None:
            self._sender_index()
            result: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
            for source in self._named_databases("message_resource.db"):
                connection = self._open(source)
                try:
                    rows = connection.execute(
                        "SELECT i.rowid AS info_rowid, i.message_id, i.chat_id, "
                        "i.message_local_id, i.message_svr_id, i.message_local_type, "
                        "c.user_name AS chat_username, d.rowid AS detail_rowid, "
                        "d.resource_id, d.type, d.size FROM MessageResourceInfo i "
                        "JOIN ChatName2Id c ON c.rowid=i.chat_id "
                        "LEFT JOIN MessageResourceDetail d "
                        "ON d.message_id=i.message_id ORDER BY i.rowid, d.rowid"
                    )
                    for row in rows:
                        entry = {
                            "database": source.relative_to(self._storage).as_posix(),
                            "info_rowid": row["info_rowid"],
                            "detail_rowid": row["detail_rowid"],
                            "message_id": row["message_id"],
                            "chat_id": row["chat_id"],
                            "session_id": str(row["chat_username"]),
                            "message_local_id": row["message_local_id"],
                            "message_svr_id": row["message_svr_id"],
                            "resource_id": row["resource_id"],
                            "resource_type": row["type"],
                            "size": row["size"],
                            "message_type": row["message_local_type"],
                        }
                        if row["message_svr_id"] not in (None, 0, "0"):
                            result.setdefault(
                                (
                                    "session_server",
                                    str(row["chat_username"]),
                                    str(row["message_svr_id"]),
                                ),
                                [],
                            ).append(entry)
                        if row["message_local_id"] not in (None, 0, "0"):
                            result.setdefault(
                                (
                                    "session_local",
                                    str(row["chat_username"]),
                                    str(row["message_local_id"]),
                                ),
                                [],
                            ).append(entry)
                except sqlite3.DatabaseError:
                    continue
            self._resource_index_cache = result
        return self._resource_index_cache

    def _voice_index(
        self,
    ) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
        if self._voice_index_cache is None:
            result: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
            for source in self._database_files():
                if not re.fullmatch(r"media_\d+\.db", source.name, re.I):
                    continue
                connection = self._open(source)
                try:
                    for rowid, chat_id, session_id, local_id, server_id, size in connection.execute(
                        "SELECT v.rowid, v.chat_name_id, n.user_name, v.local_id, "
                        "v.svr_id, length(v.voice_data) FROM VoiceInfo v "
                        "JOIN Name2Id n ON n.rowid=v.chat_name_id "
                        "WHERE v.voice_data IS NOT NULL"
                    ):
                        entry = {
                            "database": source.relative_to(self._storage).as_posix(),
                            "rowid": int(rowid),
                            "chat_id": chat_id,
                            "session_id": str(session_id),
                            "local_id": local_id,
                            "size": int(size or 0),
                            "server_id": (
                                str(server_id)
                                if server_id not in (None, 0, "0")
                                else None
                            ),
                        }
                        if server_id not in (None, 0, "0"):
                            result.setdefault(
                                (
                                    "session_server",
                                    str(session_id),
                                    str(server_id),
                                ),
                                [],
                            ).append(entry)
                        if local_id not in (None, 0, "0"):
                            result.setdefault(
                                ("session_local", str(session_id), str(local_id)),
                                [],
                            ).append(entry)
                except sqlite3.DatabaseError:
                    continue
            self._voice_index_cache = result
        return self._voice_index_cache

    @staticmethod
    def _index_with_leading_columns(
        connection: sqlite3.Connection,
        table: str,
        columns: tuple[str, ...],
    ) -> str | None:
        """Return one existing exact-lookup index, never a scan fallback."""

        # ``table`` is derived from the fixed native schema, not user input.  It
        # is still encoded as a SQLite string literal so no identifier can leak
        # into this metadata-only PRAGMA.
        try:
            index_rows = connection.execute(
                "PRAGMA index_list(" + json.dumps(table) + ")"
            )
            for index_row in index_rows:
                index_name = str(index_row[1])
                index_columns = tuple(
                    str(item[2])
                    for item in connection.execute(
                        "PRAGMA index_info(" + json.dumps(index_name) + ")"
                    )
                )
                if index_columns[: len(columns)] == columns:
                    return index_name
        except sqlite3.DatabaseError:
            return None
        return None

    def _exact_resource_rows(
        self,
        *,
        session_id: str,
        server_id: object,
        message_source: Path,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Look up one selected message's resource rows through native indexes.

        ``False`` means the installed source schema has no mechanically proven
        exact route.  It deliberately does *not* mean that a resource was
        looked for and is absent.
        """

        if server_id in (None, 0, "0"):
            return [], False
        result: list[dict[str, Any]] = []
        resource_source = message_source.parent / "message_resource.db"
        candidates = [resource_source] if resource_source.is_file() else []
        if not candidates:
            return [], False
        for source in candidates:
            connection = self._open(source)
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not {
                    "ChatName2Id",
                    "MessageResourceInfo",
                    "MessageResourceDetail",
                } <= tables:
                    return [], False
                info_index = self._index_with_leading_columns(
                    connection,
                    "MessageResourceInfo",
                    ("chat_id", "message_svr_id"),
                )
                detail_index = self._index_with_leading_columns(
                    connection,
                    "MessageResourceDetail",
                    ("message_id",),
                )
                chat_index = self._index_with_leading_columns(
                    connection, "ChatName2Id", ("user_name",)
                )
                if info_index is None or detail_index is None or chat_index is None:
                    return [], False
                quoted_chat = _quote_identifier(chat_index)
                chat_rowids = sorted(
                    {
                        int(row[0])
                        for row in connection.execute(
                            "SELECT rowid FROM ChatName2Id INDEXED BY "
                            f"{quoted_chat} WHERE user_name=?",
                            (session_id,),
                        )
                    }
                )
                if not chat_rowids:
                    # This candidate was conclusively checked through its
                    # exact index; absence here is not an unavailable route.
                    continue
                quoted_info = _quote_identifier(info_index)
                source_infos: list[sqlite3.Row] = []
                for chat_rowid in chat_rowids:
                    source_infos.extend(
                        connection.execute(
                            "SELECT rowid, message_id, message_local_id, message_svr_id, "
                            "message_local_type FROM MessageResourceInfo INDEXED BY "
                            f"{quoted_info} WHERE chat_id=? AND message_svr_id=?",
                            (chat_rowid, server_id),
                        )
                    )
                source_infos.sort(key=lambda item: int(item[0]))
                # Several ChatName2Id rows may be stale aliases.  They are
                # safe only when their exact message identity agrees; never
                # pick an arbitrary first alias.
                info_shapes = {
                    (item[1], item[2], item[3], item[4]) for item in source_infos
                }
                if len(info_shapes) > 1:
                    return [], False
                for info in source_infos:
                    quoted_detail = _quote_identifier(detail_index)
                    details = list(
                        connection.execute(
                            "SELECT rowid, resource_id, type, size FROM "
                            "MessageResourceDetail INDEXED BY "
                            f"{quoted_detail} WHERE message_id=?",
                            (info[1],),
                        )
                    )
                    if not details:
                        details = [(None, None, None, None)]
                    for detail in details:
                        result.append(
                            {
                                "database": source.relative_to(self._storage).as_posix(),
                                "info_rowid": int(info[0]),
                                "detail_rowid": (
                                    int(detail[0]) if detail[0] is not None else None
                                ),
                                "session_id": session_id,
                                "message_local_id": info[2],
                                "message_svr_id": info[3],
                                "resource_id": detail[1],
                                "resource_type": detail[2],
                                "size": detail[3],
                                "message_type": info[4],
                            }
                        )
            except sqlite3.DatabaseError:
                # Every candidate shard must support the exact lookup.  One
                # unavailable/failed shard means we cannot honestly report a
                # global exact not-found result.
                return [], False
        result.sort(
            key=lambda item: (
                str(item["database"]),
                int(item["info_rowid"]),
                int(item["detail_rowid"] or 0),
            )
        )
        return result, True

    def _exact_voice_rows(
        self,
        *,
        session_id: str,
        server_id: object,
        message_source: Path,
    ) -> tuple[list[dict[str, Any]], bool]:
        if server_id in (None, 0, "0"):
            return [], False
        result: list[dict[str, Any]] = []
        candidates = sorted(message_source.parent.glob("media_*.db"))
        if not candidates:
            return [], False
        verified_candidate = False
        uncertain_candidate = False
        for source in candidates:
            connection = self._open(source)
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not {"Name2Id", "VoiceInfo"} <= tables:
                    continue
                voice_index = self._index_with_leading_columns(
                    connection,
                    "VoiceInfo",
                    ("chat_name_id", "svr_id"),
                )
                chat_index = self._index_with_leading_columns(
                    connection, "Name2Id", ("user_name",)
                )
                if voice_index is None or chat_index is None:
                    uncertain_candidate = True
                    continue
                verified_candidate = True
                quoted_chat = _quote_identifier(chat_index)
                chat_rowids = sorted(
                    {
                        int(row[0])
                        for row in connection.execute(
                            "SELECT rowid FROM Name2Id INDEXED BY "
                            f"{quoted_chat} WHERE user_name=?",
                            (session_id,),
                        )
                    }
                )
                if not chat_rowids:
                    continue
                quoted_index = _quote_identifier(voice_index)
                source_voices: list[sqlite3.Row] = []
                for chat_rowid in chat_rowids:
                    source_voices.extend(
                        connection.execute(
                            "SELECT rowid, chat_name_id, local_id, svr_id, "
                            "length(voice_data) AS size FROM VoiceInfo INDEXED BY "
                            f"{quoted_index} WHERE chat_name_id=? AND svr_id=? "
                            "AND voice_data IS NOT NULL AND length(voice_data)>0",
                            (chat_rowid, server_id),
                        )
                    )
                source_voices.sort(key=lambda item: int(item[0]))
                for voice in source_voices:
                    result.append(
                        {
                            "database": source.relative_to(self._storage).as_posix(),
                            "rowid": int(voice[0]),
                            "chat_id": int(voice[1]),
                            "session_id": session_id,
                            "local_id": voice[2],
                            "server_id": (
                                str(voice[3])
                                if voice[3] not in (None, 0, "0")
                                else None
                            ),
                            "size": int(voice[4] or 0),
                        }
                    )
            except sqlite3.DatabaseError:
                uncertain_candidate = True
        result.sort(
            key=lambda item: (
                str(item["database"]),
                int(item["chat_id"]),
                int(item["rowid"]),
            )
        )
        return result, verified_candidate and not uncertain_candidate

    def _exact_media_entries(
        self,
        *,
        row: sqlite3.Row,
        session_id: str,
        message_source: Path,
        message_table: str,
        kind: str,
    ) -> list[dict[str, Any]]:
        """Emit only an exact resource result or an explicit unprocessed gap.

        Group projection must never populate the old all-history resource/voice
        caches merely because one selected message happens to carry media.
        """

        server_id = row["server_id"]
        local_id = row["local_id"]
        fallback_locator = self._make_locator(
            {
                "record": "message",
                "kind": kind,
                "message_database": message_source.relative_to(self._storage).as_posix(),
                "message_table": message_table,
                "local_id": local_id,
                "server_id": server_id,
            }
        )
        if kind in {"image", "emoji", "video", "file"}:
            payload = self._decode_locator(fallback_locator)
            payload["record"] = "native_media"
            if "_rowid" in row.keys():
                payload["message_rowid"] = row["_rowid"]
            locator = self._make_locator(payload)
            details = self._describe_native_media(payload)
            return [{
                "kind": kind, "mediaId": str(server_id or local_id or ""),
                "locator": locator, **details,
            }]
        def exact_lookup_unavailable(reason: str) -> list[dict[str, Any]]:
            # Do not turn a route that was not mechanically verified into a
            # ``not_openable`` fact.  The message event remains useful, but
            # its media attachment has only an honest unprocessed locator.
            return [
                {
                    "kind": kind,
                    "mediaId": str(server_id or f"{session_id}:{local_id}"),
                    "locator": fallback_locator,
                    "openable": None,
                    "open_status": "unknown",
                    "processing_state": "unprocessed",
                    "exact_lookup_unavailable": True,
                    "resolution_gap": reason,
                }
            ]

        expected_local = _valid_sender_key(local_id)
        expected_server = _valid_sender_key(server_id)
        expected_type = _base_message_type(row["local_type"])
        if kind == "voice":
            voices, voice_lookup_available = self._exact_voice_rows(
                session_id=session_id,
                server_id=server_id,
                message_source=message_source,
            )
            if not voice_lookup_available:
                return exact_lookup_unavailable("voice_exact_lookup_unavailable")
            if len(voices) != 1:
                return exact_lookup_unavailable(
                    "voice_exact_lookup_not_found_or_ambiguous"
                )
            voice = voices[0]
            if (
                expected_local is None
                or expected_server is None
                or expected_type != 34
                or _valid_sender_key(voice.get("local_id")) != expected_local
                or _valid_sender_key(voice.get("server_id")) != expected_server
            ):
                return exact_lookup_unavailable("voice_exact_lookup_identity_mismatch")
            locator_payload = {
                "record": "voice",
                "kind": "voice",
                "voice_database": voice["database"],
                "voice_rowid": voice["rowid"],
                "voice_server_id": voice["server_id"],
                "voice_local_id": voice["local_id"],
                "voice_chat_id": voice["chat_id"],
                "voice_session": voice["session_id"],
            }
            return [
                {
                    "kind": "voice",
                    "mediaId": str(server_id),
                    "locator": self._make_locator(locator_payload),
                    "size": voice["size"],
                    "openable": True,
                    "open_status": "openable",
                    "processing_state": "available",
                }
            ]

        resources, resource_lookup_available = self._exact_resource_rows(
            session_id=session_id,
            server_id=server_id,
            message_source=message_source,
        )

        if not resource_lookup_available:
            return exact_lookup_unavailable("resource_exact_lookup_unavailable")
        if not resources:
            return [
                {
                    "kind": kind,
                    "mediaId": str(server_id or f"{session_id}:{local_id}"),
                    "locator": fallback_locator,
                    "openable": False,
                    "open_status": "not_openable",
                    "processing_state": "unprocessed",
                    "resolution_gap": "resource_exact_lookup_not_found",
                }
            ]

        # The indexed lookup key alone is insufficient.  A stale alias or
        # recycled native row can share SERVERID while pointing at a different
        # local message/type.  Only attach a resource after all identifiers
        # prove that it belongs to this selected message.
        if expected_local is None or expected_server is None or expected_type is None:
            return exact_lookup_unavailable("resource_exact_lookup_identity_unproven")
        for resource in resources:
            resource_local = _valid_sender_key(resource.get("message_local_id"))
            resource_server = _valid_sender_key(resource.get("message_svr_id"))
            resource_message_type = _base_message_type(resource.get("message_type"))
            detail_kind = _TYPE_NAMES.get(
                _base_message_type(resource.get("resource_type")) or -1
            )
            if (
                resource_local != expected_local
                or resource_server != expected_server
                or resource_message_type != expected_type
                or detail_kind != kind
            ):
                return exact_lookup_unavailable(
                    "resource_exact_lookup_identity_or_type_mismatch"
                )

        emitted_ids: set[str] = set()
        result: list[dict[str, Any]] = []
        for resource in resources:
            detail_kind = _TYPE_NAMES.get(
                _base_message_type(resource.get("resource_type")) or -1
            )
            if detail_kind not in {"image", "voice", "video", "emoji"}:
                detail_kind = kind
            native_id = str(
                resource.get("resource_id")
                or f"resource:{resource['info_rowid']}:{resource['detail_rowid']}"
            )
            if native_id in emitted_ids:
                native_id = f"{native_id}:detail:{resource['detail_rowid']}"
            emitted_ids.add(native_id)
            locator_payload: dict[str, Any] = {
                "record": "resource",
                "kind": detail_kind,
                "resource_database": resource["database"],
                "resource_session": resource["session_id"],
                "info_rowid": resource["info_rowid"],
                "detail_rowid": resource["detail_rowid"],
            }
            item: dict[str, Any] = {
                "kind": detail_kind,
                "mediaId": native_id,
                "locator": "",
                "size": resource.get("size"),
                "openable": False,
                "open_status": "not_openable",
                "processing_state": "unprocessed",
            }
            item["locator"] = self._make_locator(locator_payload)
            result.append(item)
        return result

    @cached_property
    def _media_keys(self) -> tuple[bytes | None, int | None]:
        if not hasattr(self, "_config_path") or not hasattr(self, "_local_state_path"):
            return None, None
        return load_direct_media_keys(self._config_path, self._local_state_path, self._identity)

    def _native_media_attributes(self, payload: Mapping[str, Any]) -> dict[str, str] | None:
        table = str(payload.get("message_table") or "")
        kind = str(payload.get("kind") or "")
        if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
            return None
        source = self._locator_source(payload.get("message_database"))
        connection = self._open(source)
        quoted = _quote_identifier(table)
        try:
            columns = {str(item[1]) for item in connection.execute(f"PRAGMA table_info({quoted})")}
            extra = "".join(
                f", NULL AS {name}" for name in
                ("message_content", "compress_content", "source", "packed_info_data", "origin_source")
                if name not in columns
            )
            where = "local_id IS ? AND server_id IS ?"
            values = [payload.get("local_id"), payload.get("server_id")]
            if payload.get("message_rowid") is not None:
                where += " AND rowid=?"
                values.append(payload["message_rowid"])
            rows = list(connection.execute(f"SELECT rowid AS _rowid, *{extra} FROM {quoted} WHERE {where} LIMIT 2", values))
        except sqlite3.DatabaseError:
            return None
        if len(rows) != 1:
            return None
        row = rows[0]
        expected_type = {"image": 3, "emoji": 47, "video": 43, "file": 49}.get(kind)
        if _base_message_type(row["local_type"]) != expected_type:
            return None
        bodies, _, _ = _message_payload_texts(row)
        candidates: list[dict[str, str]] = []
        tag = {"image": "img", "emoji": "emoji", "video": "videomsg", "file": "appmsg"}[kind]
        for body in bodies:
            start = body.find("<")
            if start < 0:
                continue
            try:
                root = ET.fromstring(body[start:])
            except ET.ParseError:
                continue
            node = root if root.tag == tag else root.find(tag)
            if node is None:
                continue
            attributes = dict(node.attrib)
            if kind == "file":
                if node.findtext("type") != "6":
                    continue
                attributes["md5"] = node.findtext("appattach/md5") or node.findtext("md5") or ""
                attributes["fileName"] = node.findtext("title") or ""
            if re.fullmatch(r"[0-9a-fA-F]{32}", attributes.get("md5", "")):
                attributes["md5"] = attributes["md5"].lower()
                candidates.append(attributes)
        if not candidates or len({item["md5"] for item in candidates}) != 1:
            return None
        return candidates[0]

    def _hardlink_media_paths(self, md5: str, kind: str) -> list[tuple[Path, str]]:
        table = {"image": "image_hardlink_info_v4", "video": "video_hardlink_info_v4", "file": "file_hardlink_info_v4"}[kind]
        root = self._storage.parent.resolve()
        result: list[tuple[Path, str]] = []
        for source in self._named_databases("hardlink.db"):
            connection = self._open(source)
            try:
                rows = connection.execute(
                    f"SELECT h.file_name,d1.username AS dir_one,d2.username AS dir_two FROM {table} h "
                    "LEFT JOIN dir2id d1 ON d1.rowid=h.dir1 LEFT JOIN dir2id d2 ON d2.rowid=h.dir2 "
                    "WHERE h.md5=? ORDER BY h.modify_time DESC", (md5,),
                )
                for row in rows:
                    name = str(row["file_name"] or "")
                    if not name:
                        continue
                    directories = [str(row[key]) for key in ("dir_one", "dir_two") if row[key]]
                    if kind == "image":
                        bases = [root.joinpath("msg", "attach", *directories, "Img")]
                    else:
                        bases = [root.joinpath("msg", kind, *directories)]
                        bases.extend(root / "msg" / kind / value for value in directories if re.fullmatch(r"\d{4}-\d{2}", value))
                    for base in bases:
                        names = [(name, "original")]
                        if kind == "image":
                            stem = re.sub(r"(?:_[ht])?\.dat$", "", name, flags=re.I)
                            if re.fullmatch(r"[0-9a-fA-F]{32}", stem):
                                names = [(stem + "_h.dat", "original"), (stem + ".dat", "original"), (stem + "_t.dat", "thumbnail")]
                            elif not Path(name).suffix:
                                names.append((name + ".dat", "original"))
                        for filename, quality in names:
                            path = (base / filename).resolve()
                            if path.is_relative_to(root) and path.is_file() and all(item[0] != path for item in result):
                                result.append((path, quality))
            except sqlite3.DatabaseError:
                continue
        return result

    def _emoji_source(self, attributes: Mapping[str, str]) -> tuple[Any, str | None, str | None]:
        from wechat_media import EmojiStoreRecord, _is_native_emoji_cdn_url

        store = None
        aes_key = None
        native_url = attributes.get("cdnurl")
        if not _is_native_emoji_cdn_url(native_url):
            native_url = None
        for source in self._named_databases("emoticon.db"):
            connection = self._open(source)
            try:
                row = connection.execute("SELECT md5,aes_key,cdn_url FROM kNonStoreEmoticonTable WHERE md5=? LIMIT 1", (attributes["md5"],)).fetchone()
                if row is not None:
                    aes_key = str(row["aes_key"] or "") or None
                    candidate_url = str(row["cdn_url"] or "") or None
                    if _is_native_emoji_cdn_url(candidate_url):
                        native_url = candidate_url
            except sqlite3.DatabaseError:
                pass
            try:
                row = connection.execute("SELECT package_id_,md5_,emoticon_offset_,emoticon_size_,thumb_offset_,thumb_size_ FROM kStoreEmoticonFilesTable WHERE md5_=? LIMIT 1", (attributes["md5"],)).fetchone()
                if row is not None:
                    store = EmojiStoreRecord(str(row[0]), str(row[1]), *(int(value or 0) for value in row[2:]))
            except (sqlite3.DatabaseError, TypeError, ValueError):
                pass
        return store, aes_key, native_url

    def _open_native_media(self, payload: Mapping[str, Any], *, allow_remote: bool = False) -> Any:
        from wechat_image import decode_wechat_dat
        from wechat_media import LocalMedia, _verify_pillow_visual, fetch_emoji_media, open_emoji_media

        attributes = self._native_media_attributes(payload)
        if attributes is None:
            return None
        kind = str(payload["kind"])
        if kind == "emoji":
            store, aes_key, native_url = self._emoji_source(attributes)
            image_aes, image_xor = self._media_keys
            result = open_emoji_media(
                account_root=self._storage.parent, emoji_md5=attributes["md5"], store=store,
                aes_key=aes_key,
                decode_blob=lambda data, _key: decode_wechat_dat(data, aes_key=image_aes, xor_key=image_xor),
            )
            if result is not None or not allow_remote or not native_url:
                return result
            declared = attributes.get("len")
            return fetch_emoji_media(
                emoji_md5=attributes["md5"], native_url=native_url,
                declared_size=int(declared) if declared and declared.isdigit() else None,
            )
        for path, quality in self._hardlink_media_paths(attributes["md5"], kind):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if kind == "image":
                aes_key, xor_key = self._media_keys
                data = decode_wechat_dat(data, aes_key=aes_key, xor_key=xor_key)
                if data is not None and data.startswith(b"wxgf"):
                    try:
                        from wechat_wxgf import wxgf_to_image
                    except ImportError:
                        continue
                    data = wxgf_to_image(data)
                visual = _verify_pillow_visual(data) if data is not None else None
                if visual is None:
                    continue
                mime_type, extension = visual
                return LocalMedia(data, attributes["md5"] + "." + extension, mime_type, quality)
            import mimetypes

            if hashlib.md5(data, usedforsecurity=False).hexdigest() != attributes["md5"]:
                continue
            name = attributes.get("fileName") or path.name
            return LocalMedia(data, name, mimetypes.guess_type(name)[0] or "application/octet-stream", quality)
        return None

    def _describe_native_media(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        from wechat_media import _is_native_emoji_cdn_url

        try:
            attributes = self._native_media_attributes(payload)
            if attributes is not None and payload.get("kind") in {"image", "video", "file"}:
                paths = self._hardlink_media_paths(attributes["md5"], str(payload["kind"]))
                if paths:
                    path, quality = paths[0]
                    return {"openable": None, "materializable": True, "requiresNetwork": False, "open_status": "not_requested", "processing_state": "unprocessed", "resolution_gap": "media_materialization_required", "fileName": attributes.get("fileName") or path.name, "quality": quality, "cachedBytes": path.stat().st_size, "materializationSource": "local"}
            media = self._open_native_media(payload)
            if media is not None:
                return {"openable": True, "open_status": "openable", "processing_state": "available", "fileName": media.file_name, "mimeType": media.mime_type, "quality": media.quality, "size": len(media.payload), "materializationSource": media.source}
            if payload.get("kind") == "emoji" and attributes is not None:
                _, _, native_url = self._emoji_source(attributes)
                if _is_native_emoji_cdn_url(native_url):
                    return {"openable": None, "materializable": True, "requiresNetwork": True, "open_status": "not_requested", "processing_state": "unprocessed", "resolution_gap": "media_materialization_required"}
        except (WeChatDirectError, OSError, ValueError):
            pass
        return {"openable": False, "open_status": "not_openable", "processing_state": "unprocessed", "resolution_gap": "native_media_missing_or_unreadable"}

    def _make_locator(self, payload: dict[str, Any]) -> str:
        body = dict(payload)
        body["version"] = 1
        encoded = base64.urlsafe_b64encode(
            json.dumps(
                body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return (
            f"wechat-db://{self.account_identity_commitment}/v1/{encoded}"
        )

    def _decode_locator(self, locator: str) -> dict[str, Any]:
        prefix = f"wechat-db://{self.account_identity_commitment}/v1/"
        if not isinstance(locator, str) or not locator.startswith(prefix):
            raise DirectSchemaError("media locator belongs to a different account")
        encoded = locator[len(prefix) :]
        if not encoded or len(encoded) > 16_384:
            raise DirectSchemaError("media locator payload is invalid")
        try:
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(
                base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
            )
        except (
            binascii.Error,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise DirectSchemaError("media locator payload is invalid") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise DirectSchemaError("media locator version is unsupported")
        return payload

    def _locator_source(self, relative: object) -> Path:
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise DirectSchemaError("media locator database is invalid")
        parts = relative.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise DirectSchemaError("media locator database is invalid")
        source = self._storage.joinpath(*parts)
        try:
            source.resolve().relative_to(self._storage.resolve())
        except (OSError, ValueError) as exc:
            raise DirectSchemaError("media locator database is outside account scope") from exc
        if not source.is_file():
            raise DirectSchemaError("media locator database is unavailable")
        return source

    def resolve_locator(self, locator: str) -> dict[str, Any]:
        """Resolve one locator without returning message bodies or native IDs."""

        payload = self._decode_locator(locator)
        if payload.get("record") == "native_media":
            cached = getattr(self, "_opened_native_media", {}).get(locator)
            return {"kind": payload.get("kind"), **(cached or self._describe_native_media(payload))}
        record = payload.get("record")
        kind = str(payload.get("kind") or "unknown")
        if record == "voice":
            source = self._locator_source(payload.get("voice_database"))
            connection = self._open(source)
            try:
                voice = connection.execute(
                    "SELECT length(v.voice_data) AS size FROM VoiceInfo v "
                    "JOIN Name2Id n ON n.rowid=v.chat_name_id "
                    "WHERE v.rowid=? AND v.chat_name_id=? AND n.user_name=? "
                    "AND v.local_id IS ? "
                    "AND (? IS NULL OR CAST(v.svr_id AS TEXT)=?) "
                    "AND v.voice_data IS NOT NULL AND length(v.voice_data)>0",
                    (
                        int(payload["voice_rowid"]),
                        int(payload["voice_chat_id"]),
                        str(payload["voice_session"]),
                        payload.get("voice_local_id"),
                        payload.get("voice_server_id"),
                        str(payload.get("voice_server_id")),
                    ),
                ).fetchone()
            except (KeyError, TypeError, ValueError, sqlite3.DatabaseError) as exc:
                raise DirectSchemaError("voice locator cannot be resolved") from exc
            if voice is None:
                raise DirectSchemaError("voice locator no longer resolves")
            return {
                "record": "voice",
                "kind": "voice",
                "size": int(voice["size"] or 0),
                "openable": True,
                "open_status": "openable",
                "processing_state": "available",
                "resolution_status": "resolved",
            }
        if record == "resource":
            source = self._locator_source(payload.get("resource_database"))
            connection = self._open(source)
            try:
                info_rowid = int(payload["info_rowid"])
                resource_session = str(payload["resource_session"])
                detail_rowid = payload.get("detail_rowid")
                if detail_rowid is None:
                    row = connection.execute(
                        "SELECT i.message_local_type AS native_type, NULL AS size "
                        "FROM MessageResourceInfo i JOIN ChatName2Id c "
                        "ON c.rowid=i.chat_id WHERE i.rowid=? AND c.user_name=?",
                        (info_rowid, resource_session),
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT i.message_local_type AS native_type, d.size AS size "
                        "FROM MessageResourceInfo i JOIN MessageResourceDetail d "
                        "ON d.message_id=i.message_id "
                        "JOIN ChatName2Id c ON c.rowid=i.chat_id "
                        "WHERE i.rowid=? AND d.rowid=? AND c.user_name=?",
                        (info_rowid, int(detail_rowid), resource_session),
                    ).fetchone()
            except (KeyError, TypeError, ValueError, sqlite3.DatabaseError) as exc:
                raise DirectSchemaError("media resource locator cannot be resolved") from exc
            if row is None:
                raise DirectSchemaError("media resource locator no longer resolves")
            result: dict[str, Any] = {
                "record": "resource",
                "kind": kind,
                "size": row["size"],
                "openable": False,
                "open_status": "not_openable",
                "processing_state": "unprocessed",
                "resolution_status": "resolved",
            }
            voice_database = payload.get("voice_database")
            voice_rowid = payload.get("voice_rowid")
            voice_server_id = payload.get("voice_server_id")
            voice_local_id = payload.get("voice_local_id")
            voice_chat_id = payload.get("voice_chat_id")
            voice_session = payload.get("voice_session")
            if (
                kind == "voice"
                and voice_database is not None
                and voice_rowid is not None
                and voice_chat_id is not None
                and voice_session is not None
            ):
                voice_source = self._locator_source(voice_database)
                voice_connection = self._open(voice_source)
                try:
                    voice = voice_connection.execute(
                        "SELECT length(v.voice_data) AS size FROM VoiceInfo v "
                        "JOIN Name2Id n ON n.rowid=v.chat_name_id "
                        "WHERE v.rowid=? AND v.chat_name_id=? AND n.user_name=? "
                        "AND v.local_id IS ? "
                        "AND (? IS NULL OR CAST(v.svr_id AS TEXT)=?) "
                        "AND v.voice_data IS NOT NULL",
                        (
                            int(voice_rowid),
                            int(voice_chat_id),
                            str(voice_session),
                            voice_local_id,
                            voice_server_id,
                            str(voice_server_id),
                        ),
                    ).fetchone()
                except (TypeError, ValueError, sqlite3.DatabaseError) as exc:
                    raise DirectSchemaError("voice locator cannot be resolved") from exc
                if voice is None:
                    raise DirectSchemaError("voice locator no longer resolves")
                result.update(
                    size=int(voice["size"] or 0),
                    openable=True,
                    open_status="openable",
                    processing_state="available",
                )
            return result

        if record == "message":
            source = self._locator_source(payload.get("message_database"))
            table = payload.get("message_table")
            if not isinstance(table, str) or re.fullmatch(r"Msg_[0-9a-f]{32}", table) is None:
                raise DirectSchemaError("message locator table is invalid")
            connection = self._open(source)
            quoted_table = _quote_identifier(table)
            try:
                local_id = payload["local_id"]
                server_id = payload.get("server_id")
                if server_id is None:
                    row = connection.execute(
                        f"SELECT local_type FROM {quoted_table} "
                        "WHERE local_id=? AND server_id IS NULL LIMIT 1",
                        (local_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        f"SELECT local_type FROM {quoted_table} "
                        "WHERE local_id=? AND CAST(server_id AS TEXT)=? LIMIT 1",
                        (local_id, str(server_id)),
                    ).fetchone()
            except (KeyError, sqlite3.DatabaseError) as exc:
                raise DirectSchemaError("message locator cannot be resolved") from exc
            if row is None:
                raise DirectSchemaError("message locator no longer resolves")
            return {
                "record": "message",
                "kind": kind,
                "size": None,
                "openable": False,
                "open_status": "not_openable",
                "processing_state": "unprocessed",
                "resolution_status": "resolved",
                "resolution_gap": "resource_context_unproven",
            }
        raise DirectSchemaError("media locator record type is unsupported")

    def open_locator(self, locator: str, *, allow_remote: bool = False) -> bytes:
        """Open one bound native media item; remote emoji reads require opt-in."""

        payload = self._decode_locator(locator)
        if payload.get("record") == "native_media":
            media = self._open_native_media(payload, allow_remote=allow_remote)
            if media is None:
                raise MediaNotOpenableError("bound native media is currently unavailable")
            if not hasattr(self, "_opened_native_media"):
                self._opened_native_media = {}
            self._opened_native_media[locator] = {"openable": True, "open_status": "openable", "processing_state": "available", "fileName": media.file_name, "mimeType": media.mime_type, "quality": media.quality, "size": len(media.payload), "materializationSource": media.source}
            return media.payload

        resolved = self.resolve_locator(locator)
        if not resolved["openable"]:
            raise MediaNotOpenableError(
                "media resource is not openable by the direct reader"
            )
        payload = self._decode_locator(locator)
        source = self._locator_source(payload.get("voice_database"))
        connection = self._open(source)
        try:
            row = connection.execute(
                "SELECT v.voice_data FROM VoiceInfo v "
                "JOIN Name2Id n ON n.rowid=v.chat_name_id "
                "WHERE v.rowid=? AND v.chat_name_id=? AND n.user_name=? "
                "AND v.local_id IS ? "
                "AND (? IS NULL OR CAST(v.svr_id AS TEXT)=?) "
                "AND v.voice_data IS NOT NULL AND length(v.voice_data)>0",
                (
                    int(payload["voice_rowid"]),
                    int(payload["voice_chat_id"]),
                    str(payload["voice_session"]),
                    payload.get("voice_local_id"),
                    payload.get("voice_server_id"),
                    str(payload.get("voice_server_id")),
                ),
            ).fetchone()
        except (KeyError, TypeError, ValueError, sqlite3.DatabaseError) as exc:
            raise DirectSchemaError("voice locator cannot be opened") from exc
        if row is None or not isinstance(row["voice_data"], (bytes, bytearray, memoryview)):
            raise DirectSchemaError("voice locator no longer resolves to a BLOB")
        return bytes(row["voice_data"])

    def _media_entries(
        self,
        row: sqlite3.Row,
        session_id: str,
        message_source: Path,
        message_table: str,
        payload_texts: Sequence[str],
        *,
        exact_lookup_only: bool = False,
    ) -> list[dict[str, Any]]:
        base_type = _base_message_type(row["local_type"])
        kind = _TYPE_NAMES.get(base_type or -1)
        if kind == "app":
            is_file = False
            for text in payload_texts:
                start = text.find("<")
                if start < 0:
                    continue
                try:
                    root = ET.fromstring(text[start:])
                except ET.ParseError:
                    continue
                app = root if root.tag == "appmsg" else root.find("appmsg")
                if app is not None and (app.findtext("type") or "").strip() == "6":
                    is_file = True
                    break
            if not is_file:
                return []
            kind = "file"
        if kind not in {"image", "voice", "video", "emoji", "file"}:
            return []
        server_id = row["server_id"]
        local_id = row["local_id"]
        if exact_lookup_only or kind in {"image", "emoji", "video", "file"}:
            return self._exact_media_entries(
                row=row,
                session_id=session_id,
                message_source=message_source,
                message_table=message_table,
                kind=kind,
            )
        resources = (
            self._resource_index().get(
                ("session_server", session_id, str(server_id)), []
            )
            if server_id not in (None, 0, "0")
            else []
        )
        if not resources and local_id not in (None, 0, "0"):
            resources = self._resource_index().get(
                ("session_local", session_id, str(local_id)), []
            )
        voices = (
            self._voice_index().get(
                ("session_server", session_id, str(server_id)), []
            )
            if kind == "voice" and server_id not in (None, 0, "0")
            else []
        )
        if kind == "voice" and not voices and local_id not in (None, 0, "0"):
            voices = self._voice_index().get(
                ("session_local", session_id, str(local_id)), []
            )
        if not resources:
            locator = self._make_locator(
                {
                    "record": "message",
                    "kind": kind,
                    "message_database": message_source.relative_to(
                        self._storage
                    ).as_posix(),
                    "message_table": message_table,
                    "local_id": local_id,
                    "server_id": server_id,
                }
            )
            return [
                {
                    "kind": kind,
                    "mediaId": str(server_id or f"{session_id}:{local_id}"),
                    "locator": locator,
                    "size": None,
                    "openable": False,
                    "open_status": "not_openable",
                    "processing_state": "unprocessed",
                    "resolution_gap": "resource_context_unproven",
                }
            ]

        emitted_ids: set[str] = set()
        media: list[dict[str, Any]] = []
        for resource in resources:
            detail_kind = _TYPE_NAMES.get(
                _base_message_type(resource.get("resource_type")) or -1
            )
            if detail_kind not in {"image", "voice", "video", "emoji"}:
                detail_kind = kind
            native_id = str(
                resource.get("resource_id")
                or f"resource:{resource['info_rowid']}:{resource['detail_rowid']}"
            )
            if native_id in emitted_ids:
                native_id = f"{native_id}:detail:{resource['detail_rowid']}"
            emitted_ids.add(native_id)
            locator_payload: dict[str, Any] = {
                "record": "resource",
                "kind": detail_kind,
                "resource_database": resource["database"],
                "resource_session": resource["session_id"],
                "info_rowid": resource["info_rowid"],
                "detail_rowid": resource["detail_rowid"],
            }
            item: dict[str, Any] = {
                "kind": detail_kind,
                "mediaId": native_id,
                "locator": "",
                "size": resource.get("size"),
                "openable": False,
                "open_status": "not_openable",
                "processing_state": "unprocessed",
            }
            if detail_kind == "voice" and len(voices) == 1:
                voice = voices[0]
                locator_payload.update(
                    voice_database=voice["database"],
                    voice_rowid=voice["rowid"],
                    voice_server_id=voice["server_id"],
                    voice_local_id=voice["local_id"],
                    voice_chat_id=voice["chat_id"],
                    voice_session=voice["session_id"],
                )
                item.update(
                    size=voice["size"],
                    openable=True,
                    open_status="openable",
                    processing_state="available",
                )
            elif detail_kind == "voice":
                item["resolution_gap"] = "voice_blob_missing_or_ambiguous"
            item["locator"] = self._make_locator(locator_payload)
            media.append(item)
        return media

    @staticmethod
    def _message_row_identity(row: sqlite3.Row) -> tuple[str, str] | None:
        server_id = row["server_id"]
        if server_id not in (None, 0, "0"):
            return "server", str(server_id)
        local_id = row["local_id"]
        if local_id not in (None, 0, "0"):
            return "local", str(local_id)
        return None

    def _private_message_record_identity(
        self, source: Path, table: str, row: sqlite3.Row
    ) -> tuple[str, str]:
        identity = self._message_row_identity(row)
        if identity is not None and identity[0] == "server":
            return identity
        shard_key = self._group_projection_shard_key(source, table)
        if identity is not None:
            return "local", f"{shard_key}\0{identity[1]}"
        try:
            rowid = int(row["_rowid"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise DirectSchemaError("message row lacks a stable physical identity") from exc
        return "row", f"{shard_key}\0{rowid}"

    def _private_message_candidate_signature(
        self, row: sqlite3.Row, message: Mapping[str, Any]
    ) -> tuple[Any, ...]:
        return (
            self._group_exact_field_digest(
                json.dumps(
                    self._group_canonical_semantic_output(message),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
            *(
                self._group_exact_field_digest(row[column])
                for column in (
                    "local_type",
                    "server_id",
                    "real_sender_id",
                    "create_time",
                    "message_content",
                    "source",
                    "packed_info_data",
                    "compress_content",
                    "sort_seq",
                    "status",
                    "origin_source",
                )
            ),
        )

    def _project_private_message_candidates(
        self,
        candidates: Sequence[tuple[Path, sqlite3.Connection, sqlite3.Row]],
        *,
        session_native_id: str,
        message_table: str,
        exact_media_lookup: bool,
    ) -> dict[str, Any]:
        projected: list[tuple[tuple[str, int, str], tuple[Any, ...], dict[str, Any]]] = []
        for source, connection, row in candidates:
            message = self._message_from_row(
                row=row,
                session_native_id=session_native_id,
                message_source=source,
                message_table=message_table,
                connection=connection,
                sender_index=self._sender_index_for_message_source(source),
                exact_media_lookup=exact_media_lookup,
            )
            try:
                rowid = int(row["_rowid"])
            except (KeyError, TypeError, ValueError, OverflowError):
                rowid = 0
            representative_key = (
                self._group_projection_shard_key(source, message_table),
                rowid,
                str(row["local_id"] or ""),
            )
            projected.append(
                (
                    representative_key,
                    self._private_message_candidate_signature(row, message),
                    message,
                )
            )
        if len({item[1] for item in projected}) != 1:
            raise DirectSchemaError("message_identity_is_conflicting")
        return min(projected, key=lambda item: item[0])[2]

    def _message_row_sender_role(
        self,
        source: Path,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        private_session: bool = False,
        message_table: str | None = None,
        strict_group_projection: bool = False,
        group_shard_key: str | None = None,
        group_self_sender_receipt: Mapping[str, Any] | None = None,
    ) -> tuple[int | None, str, str | None, bool]:
        base_type = _base_message_type(row["local_type"])
        try:
            native_status = int(row["status"])
        except (TypeError, ValueError, OverflowError):
            native_status = None
        if (
            strict_group_projection
            and base_type != 10000
            and native_status in _CALIBRATED_MESSAGE_STATUSES
        ):
            sender_key = _valid_sender_key(row["real_sender_id"])
            sender_commitment = (
                group_self_sender_receipt.get("selfSenderCommitment")
                if group_self_sender_receipt is not None
                else None
            )
            if sender_key is None or group_shard_key is None or sender_commitment is None:
                return base_type, "unknown", "unknown", None
            candidate = self._opaque_sha256_commitment(
                GROUP_SELF_SENDER_RECEIPT_ALGORITHM,
                "sha256:" + self.account_identity_commitment,
                group_shard_key,
                sender_key,
            )
            if hmac.compare_digest(str(sender_commitment), candidate):
                return base_type, "self", "outgoing", True
            return base_type, "other", "incoming", False
        calibrated_self_sender = (
            self._calibrated_self_sender(
                source,
                connection,
                # Generic group reads are scoped to one selected Msg table as
                # well.  Calibrating against every table in the physical
                # shard would make one ambiguous group row scan unrelated
                # conversations on every fresh reader.
                message_table=message_table,
            )
            if (
                not strict_group_projection
                and base_type != 10000
                and native_status in _CALIBRATED_MESSAGE_STATUSES
            )
            else None
        )
        # Private message shards can carry statuses that are not the formal
        # 2/4 direction pair.  Their ``real_sender_id`` still has a precise,
        # source-local relationship to ``SenderName2Id`` in the sibling
        # resource database.  A same-table status-2 calibration remains the
        # stronger fact for its known status family; the local dictionary only
        # fills the rows that calibration cannot reach.  No content,
        # chronology, or majority inference enters this decision.
        if (
            private_session
            and not strict_group_projection
            and base_type != 10000
            and native_status not in {_OUTGOING_MESSAGE_STATUS, _INCOMING_MESSAGE_STATUS}
            and not (
                native_status in _CALIBRATED_MESSAGE_STATUSES
                and calibrated_self_sender is not None
            )
        ):
            sender_key = _valid_sender_key(row["real_sender_id"])
            sender_name = (
                self._sender_index_for_message_source(source).get(int(sender_key))
                if sender_key is not None
                else None
            )
            if sender_name is not None:
                mapped_role = "self" if sender_name == self._identity else "other"
                return (
                    base_type,
                    mapped_role,
                    "outgoing" if mapped_role == "self" else "incoming",
                    mapped_role == "self",
                )
        sender_role, direction, is_send = _message_sender_role(
            row["status"],
            base_type,
            row["real_sender_id"],
            calibrated_self_sender,
        )
        return base_type, sender_role, direction, is_send

    @staticmethod
    def _row_structured_quote_resolution(
        row: sqlite3.Row,
    ) -> tuple[str | None, bool]:
        """Resolve native quote IDs only when all readable fields agree.

        Metadata-only selector rows lack ``message_content``; selected full rows
        include it, so the same resolver catches a body-vs-metadata conflict
        before a durable relation can be emitted.
        """

        if _base_message_type(row["local_type"]) != 49:
            return None, False
        identities: set[str] = set()
        for column in (
            "message_content",
            "source",
            "packed_info_data",
            "origin_source",
        ):
            try:
                value = row[column]
            except (IndexError, KeyError):
                continue
            identities.update(_quote_identities(_readable_payload_text(value)))
        if len(identities) == 1:
            return next(iter(identities)), False
        return None, len(identities) > 1

    def _message_from_row(
        self,
        *,
        row: sqlite3.Row,
        session_native_id: str,
        message_source: Path,
        message_table: str,
        connection: sqlite3.Connection,
        sender_index: Mapping[int, str],
        strict_group_projection: bool = False,
        exact_media_lookup: bool = False,
        group_shard_key: str | None = None,
        group_self_sender_receipt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build one public reader message only after its row was selected."""

        server_id = row["server_id"]
        local_id = row["local_id"]
        sender_id = row["real_sender_id"]
        try:
            sender_key = int(sender_id)
        except (TypeError, ValueError):
            sender_key = None
        base_type, sender_role, direction, is_send = self._message_row_sender_role(
            message_source,
            connection,
            row,
            private_session=(
                not session_native_id.casefold().endswith("@chatroom")
                and not session_native_id.casefold().startswith("gh_")
            ),
            message_table=message_table,
            strict_group_projection=strict_group_projection,
            group_shard_key=group_shard_key,
            group_self_sender_receipt=group_self_sender_receipt,
        )
        sender = (
            self._identity
            if sender_role == "self"
            else sender_index.get(sender_key)
            if sender_role == "other" and sender_key is not None
            else "system"
            if sender_role == "system"
            else None
        )
        content, payload_texts, compressed_gap = _message_content_projection(
            row, base_type
        )
        message: dict[str, Any] = {
            "serverId": server_id,
            "localId": local_id,
            "localType": row["local_type"],
            "sortSeq": row["sort_seq"],
            "createTime": row["create_time"],
            "content": content,
            "status": row["status"],
            "isSend": is_send,
            "isSystem": base_type == 10000,
            "senderRole": sender_role,
            "direction": direction,
            "type": _TYPE_NAMES.get(base_type or -1, "unknown"),
        }
        if sender:
            message["senderUsername"] = sender
        elif sender_role == "other":
            message["senderGap"] = "sender_mapping_unresolved"
        elif sender_role == "unknown":
            message["directionGap"] = "message_status_unproven"
        if content is None and base_type not in {3, 34, 43, 47}:
            message["contentGap"] = compressed_gap or (
                "call_status_unavailable"
                if base_type == 50
                else "message_type_unsupported"
                if base_type not in _TYPE_NAMES
                else "message_content_unparsed"
            )
        if strict_group_projection:
            quote_id, quote_conflict = self._row_structured_quote_resolution(row)
            if quote_conflict:
                raise DirectSchemaError("group projection structured quote is conflicting")
        else:
            quote_identities: set[str] = set()
            for text in payload_texts:
                quote_identities.update(_quote_identities(text))
            quote_id = (
                next(iter(quote_identities))
                if len(quote_identities) == 1
                else None
            )
            if len(quote_identities) > 1:
                message["quoteGap"] = "quote_identity_conflict"
        if quote_id:
            message["quote"] = {"platformMessageId": quote_id}
        media = self._media_entries(
            row,
            session_native_id,
            message_source,
            message_table,
            payload_texts,
            exact_lookup_only=(strict_group_projection or exact_media_lookup),
        )
        if media:
            message["media_manifest"] = media
        if strict_group_projection and local_id not in (None, 0, "0"):
            # The source's local id remains the stable output identity even if
            # a later native amendment fills in SERVERID.  SERVERID is still
            # used internally for reply/quote resolution and cross-shard
            # conflict detection, but must not rewrite an already committed
            # group message's logical identity.
            local_identity = str(local_id)
            if local_identity:
                shard_key = self._group_projection_shard_key(
                    message_source, message_table
                )
                message["shardLocalIdentity"] = "sha256:" + hashlib.sha256(
                    f"{shard_key}\0{local_identity}".encode("utf-8")
                ).hexdigest()
        return message

    @staticmethod
    def _group_projection_rowid(row: sqlite3.Row) -> int | None:
        """Return the exact physical row locator needed for a selected message."""

        try:
            rowid = int(row["_rowid"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        return rowid if rowid > 0 else None

    @staticmethod
    def _fetch_group_projection_row(
        *,
        connection: sqlite3.Connection,
        table: str,
        rowid: int,
        status_expr: str,
        origin_expr: str,
    ) -> sqlite3.Row:
        """Read one previously selected group row, including body and media fields."""

        quoted_table = _quote_identifier(table)
        try:
            row = connection.execute(
                "SELECT local_id, local_type, server_id, real_sender_id, create_time, "
                "message_content, source, packed_info_data, compress_content, sort_seq, "
                f"{status_expr} AS status, {origin_expr} AS origin_source "
                f"FROM {quoted_table} WHERE rowid=? LIMIT 1",
                (rowid,),
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise DirectSchemaError("selected group row could not be re-read") from exc
        if row is None:
            raise DirectSchemaError("selected group row disappeared from snapshot")
        return row

    def _group_projection_shard_key(self, source: Path, table: str) -> str:
        relative = source.relative_to(self._storage).as_posix()
        return "sha256:" + hashlib.sha256(
            f"{relative}\0{table}".encode("utf-8")
        ).hexdigest()

    def _group_projection_shards(
        self, session_native_id: str
    ) -> list[dict[str, Any]]:
        """Return a stable, metadata-only catalog for one group message table."""

        table = "Msg_" + hashlib.md5(
            session_native_id.encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        quoted_table = _quote_identifier(table)
        shards: list[dict[str, Any]] = []
        for source, connection in self._message_connections(table):
            columns = {
                str(column[1])
                for column in connection.execute(f"PRAGMA table_info({quoted_table})")
            }
            if not {"server_id", "local_id", "local_type", "create_time"} <= columns:
                raise DirectSchemaError("group projection message schema is incomplete")
            server_index = self._index_with_leading_columns(
                connection, table, ("server_id",)
            )
            shards.append(
                {
                    "key": self._group_projection_shard_key(source, table),
                    "source": source,
                    "connection": connection,
                    "table": table,
                    "session_native_id": session_native_id,
                    "status_expr": "status" if "status" in columns else "NULL",
                    "origin_expr": (
                        "origin_source" if "origin_source" in columns else "NULL"
                    ),
                    "server_index": server_index,
                }
            )
        if not shards and self._session_is_registered(session_native_id):
            raise SessionMessageDatabaseMissingError(
                "session message database is missing"
            )
        return sorted(shards, key=lambda item: str(item["key"]))

    @staticmethod
    def _group_projection_record(
        shard: Mapping[str, Any], row: sqlite3.Row
    ) -> dict[str, Any]:
        rowid = DirectWeChatReader._group_projection_rowid(row)
        if rowid is None:
            raise DirectSchemaError("group projection row lacks a stable physical locator")
        return {"shard": shard, "row": row, "rowid": rowid}

    def _group_record_identity(
        self, shard: Mapping[str, Any], row: sqlite3.Row
    ) -> tuple[str, str] | None:
        """Return a source-local identity without merging local ids across shards."""

        identity = self._message_row_identity(row)
        if identity is None or identity[0] == "server":
            return identity
        return "local", f"{shard['key']}\0{identity[1]}"

    def _group_message_sender_role(
        self, shard: Mapping[str, Any], row: sqlite3.Row
    ) -> tuple[int | None, str, str | None, bool]:
        return self._message_row_sender_role(
            shard["source"],
            shard["connection"],
            row,
            strict_group_projection=True,
            group_shard_key=str(shard["key"]),
            group_self_sender_receipt=shard.get("self_sender_receipt"),
        )

    def _group_record_signature(self, record: Mapping[str, Any]) -> tuple[Any, ...]:
        shard = record["shard"]
        row = record["row"]
        base_type, sender_role, _, _ = self._group_message_sender_role(shard, row)
        quote_id, quote_conflict = self._row_structured_quote_resolution(row)
        return (
            self._group_record_identity(shard, row),
            base_type,
            sender_role,
            row["status"],
            row["create_time"],
            quote_id,
            quote_conflict,
        )

    @staticmethod
    def _group_exact_field_digest(value: object) -> tuple[str, str] | None:
        """Fingerprint one exact row field without retaining its private bytes."""

        if value is None:
            return None
        if isinstance(value, memoryview):
            payload = value.tobytes()
        elif isinstance(value, bytes):
            payload = value
        else:
            payload = str(value).encode("utf-8", "surrogatepass")
        return type(value).__name__, hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _group_canonical_semantic_output(
        message: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Remove physical locator/provenance from a duplicate-server compare."""

        result = {
            str(key): value
            for key, value in message.items()
            # Both identifiers are physical locator provenance for a message
            # that already has one cross-shard SERVERID.  They must not make
            # byte-identical native copies look like incompatible content.
            if key not in {"localId", "shardLocalIdentity"}
        }
        media = result.get("media_manifest")
        if isinstance(media, list):
            result["media_manifest"] = [
                {
                    str(key): value
                    for key, value in item.items()
                    if key != "locator"
                }
                if isinstance(item, Mapping)
                else item
                for item in media
            ]
        return result

    def _group_record_exact_signature(
        self, record: Mapping[str, Any]
    ) -> tuple[Any, ...]:
        """Boundedly compare duplicate server rows before choosing a shard copy."""

        shard = record["shard"]
        row = self._fetch_group_projection_row(
            connection=shard["connection"],
            table=shard["table"],
            rowid=int(record["rowid"]),
            status_expr=shard["status_expr"],
            origin_expr=shard["origin_expr"],
        )
        if self._group_record_identity(shard, row) != self._group_record_identity(
            shard, record["row"]
        ):
            raise DirectSchemaError("selected group row identity changed")
        exact_record = {"shard": shard, "row": row, "rowid": record["rowid"]}
        # The raw field digests below identify storage divergence.  Hashing the
        # reader's own final projected shape additionally covers the fields the
        # caller would actually observe (including media locator/open-status)
        # without retaining a duplicate body or media payload in memory.
        exact_output = self._message_from_row(
            row=row,
            session_native_id=str(shard["session_native_id"]),
            message_source=shard["source"],
            message_table=str(shard["table"]),
            connection=shard["connection"],
            sender_index={},
            strict_group_projection=True,
            group_shard_key=str(shard["key"]),
            group_self_sender_receipt=shard.get("self_sender_receipt"),
        )
        return (
            self._group_record_signature(exact_record),
            self._group_exact_field_digest(
                json.dumps(
                    self._group_canonical_semantic_output(exact_output),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
            *(
                self._group_exact_field_digest(row[column])
                for column in (
                    "local_type",
                    "server_id",
                    "real_sender_id",
                    "create_time",
                    "message_content",
                    "source",
                    "packed_info_data",
                    "compress_content",
                    "sort_seq",
                    "status",
                    "origin_source",
                )
            ),
        )

    def _iter_group_projection_metadata_pages(
        self,
        shard: Mapping[str, Any],
        *,
        start_rowid: int,
        end_rowid: int,
        page_size: int,
    ) -> Iterator[list[sqlite3.Row]]:
        quoted_table = _quote_identifier(str(shard["table"]))
        try:
            cursor = shard["connection"].execute(
                "SELECT rowid AS _rowid, local_id, local_type, server_id, "
                "real_sender_id, create_time, source, packed_info_data, sort_seq, "
                f"{shard['status_expr']} AS status, "
                f"{shard['origin_expr']} AS origin_source "
                f"FROM {quoted_table} WHERE rowid>? AND rowid<=? ORDER BY rowid ASC",
                (int(start_rowid), int(end_rowid)),
            )
        except sqlite3.DatabaseError as exc:
            raise DirectSchemaError("group projection rowid cursor is unavailable") from exc
        yield from _iter_cursor_pages(cursor, page_size=page_size)

    def _exact_group_server_records(
        self,
        shards: Sequence[Mapping[str, Any]],
        server_ids: Iterable[str],
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        """Find exact group targets across all current shards without scanning."""

        wanted = sorted({str(value) for value in server_ids if str(value).strip()})
        if not wanted:
            return {}, set()
        raw: dict[str, list[dict[str, Any]]] = {}
        for shard in shards:
            index_name = shard.get("server_index")
            if not isinstance(index_name, str) or not index_name:
                raise DirectSchemaError("group projection server lookup index is unavailable")
            quoted_table = _quote_identifier(str(shard["table"]))
            quoted_index = _quote_identifier(index_name)
            for offset in range(0, len(wanted), 500):
                batch = wanted[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                try:
                    rows = shard["connection"].execute(
                        "SELECT rowid AS _rowid, local_id, local_type, server_id, "
                        "real_sender_id, create_time, source, packed_info_data, sort_seq, "
                        f"{shard['status_expr']} AS status, "
                        f"{shard['origin_expr']} AS origin_source "
                        f"FROM {quoted_table} INDEXED BY {quoted_index} "
                        f"WHERE server_id IN ({placeholders})",
                        batch,
                    )
                except sqlite3.DatabaseError as exc:
                    raise DirectSchemaError(
                        "group projection exact server lookup failed"
                    ) from exc
                for row in rows:
                    identity = self._message_row_identity(row)
                    if identity is None or identity[0] != "server":
                        continue
                    raw.setdefault(identity[1], []).append(
                        self._group_projection_record(shard, row)
                    )
        result: dict[str, dict[str, Any]] = {}
        conflicts: set[str] = set()
        for server_id, records in raw.items():
            signatures = {self._group_record_signature(record) for record in records}
            if len(signatures) != 1:
                conflicts.add(server_id)
                continue
            if len(records) > 1:
                try:
                    exact_signatures = {
                        self._group_record_exact_signature(record) for record in records
                    }
                except DirectSchemaError:
                    conflicts.add(server_id)
                    continue
                if len(exact_signatures) != 1:
                    conflicts.add(server_id)
                    continue
            result[server_id] = min(
                records,
                key=lambda record: (
                    str(record["shard"]["key"]),
                    int(record["rowid"]),
                ),
            )
        return result, conflicts

    def fetch_group_anchor_projection(
        self,
        session_native_id: str,
        *,
        end_s: int,
        page_size: int = 512,
        prior_shard_rowid_highs: Mapping[str, Any] | None = None,
        prior_shard_self_sender_receipts: Mapping[str, Any] | None = None,
        initialized: bool = False,
        full_reconcile: bool = False,
        tail_rows: int = GROUP_ANCHOR_TAIL_ROWS,
    ) -> dict[str, Any]:
        """Read one group as a closed, rowid-cursor projection.

        The first successful initialization may read the complete native group.
        Thereafter each shard is read from its persisted rowid cursor minus a
        fixed tail.  Reply/quote targets outside that bounded delta are resolved
        with an indexed exact ``server_id`` lookup across the group's shards.
        No normal increment loads a historical self map, sender calibration, or
        resource/voice index.  An initialized increment instead consumes the
        shard-bound opaque sender receipts produced by its first full scan.
        """

        if not session_native_id.casefold().endswith("@chatroom"):
            raise DirectSchemaError("group projection requires a chatroom session")
        if not isinstance(full_reconcile, bool):
            raise ValueError("group_projection_full_reconcile_invalid")
        # A fixed rowid tail cannot prove that an old already-committed row was
        # amended.  Callers must explicitly request this source-local full
        # pass; ordinary deltas never quietly claim that coverage.
        if full_reconcile:
            initialized = False
            prior_shard_rowid_highs = None
            prior_shard_self_sender_receipts = None
        page_size = int(page_size)
        tail_rows = int(tail_rows)
        if page_size < 1 or page_size > 512:
            raise ValueError("group_projection_page_size_invalid")
        if tail_rows < 0 or tail_rows > 4_096:
            raise ValueError("group_projection_tail_rows_invalid")

        shards = self._group_projection_shards(session_native_id)
        normalized_prior: dict[str, int] = {}
        invalid_prior = False
        for key, value in dict(prior_shard_rowid_highs or {}).items():
            try:
                rowid = int(value)
            except (TypeError, ValueError, OverflowError):
                invalid_prior = True
                continue
            if rowid < 0:
                invalid_prior = True
                continue
            normalized_prior[str(key)] = rowid

        pass_rows = [0, 0]
        page_count = 0
        max_page_rows = 0
        max_sort_seq: int | None = None
        hold_reasons: set[str] = set()
        held_shards: set[str] = set()
        shard_highs: dict[str, int] = {}
        scan_starts: dict[str, int] = {}
        scan_ends: dict[str, int] = {}
        current_shard_keys = {str(shard["key"]) for shard in shards}

        normalized_prior_receipts: dict[str, dict[str, str | None]] = {}
        receipt_input_invalid = False
        try:
            raw_prior_receipts = dict(prior_shard_self_sender_receipts or {})
        except (TypeError, ValueError):
            raw_prior_receipts = {}
            receipt_input_invalid = True

        def hold(shard_key: str | None, rowid: int | None, reason: str) -> None:
            hold_reasons.add(reason)
            if shard_key is None:
                return
            held_shards.add(shard_key)
            previous_high = normalized_prior.get(shard_key, 0)
            candidate = max(0, int(rowid or 0) - 1)
            # A tail re-read cannot roll an already durable cursor backwards;
            # it can only stop newly discovered rows from advancing it.
            candidate = max(previous_high, candidate) if initialized else candidate
            shard_highs[shard_key] = min(shard_highs.get(shard_key, candidate), candidate)

        if invalid_prior:
            hold(None, None, "group_anchor_cursor_invalid")
        if initialized and current_shard_keys != set(normalized_prior):
            hold(None, None, "group_anchor_shard_catalog_drift")
        if initialized and (
            receipt_input_invalid or current_shard_keys != set(raw_prior_receipts)
        ):
            hold(None, None, "group_anchor_self_sender_receipt_shard_mismatch")
        if initialized and not hold_reasons:
            for shard in shards:
                key = str(shard["key"])
                receipt = self._validated_group_self_sender_receipt(
                    shard_key=key,
                    value=raw_prior_receipts[key],
                )
                if receipt is None:
                    hold(key, None, "group_anchor_self_sender_receipt_invalid")
                    break
                normalized_prior_receipts[key] = receipt
                shard["self_sender_receipt"] = receipt
        elif not initialized:
            for shard in shards:
                key = str(shard["key"])
                # The group receipt uses the all-history physical-database
                # calibration (the ``message_table=None`` cache entry), so
                # source-local initialization never repeats it during this
                # reader run. Private sessions use table-scoped cache entries.
                shard["self_sender_receipt"] = self._group_self_sender_receipt(
                    shard_key=key,
                    calibrated_sender=self._calibrated_self_sender(
                        shard["source"], shard["connection"]
                    ),
                )

        receipt_upgrade_senders: dict[str, str] = {}

        def maybe_upgrade_unproven_sender_receipt(
            shard: Mapping[str, Any], row: sqlite3.Row
        ) -> None:
            """Check/upgrade a receipt only from exact status=2 evidence.

            A normal delta must never silently accept a new native ``self``
            sender after the opaque receipt was established.  The temporary
            in-memory upgrade for a previously unproven receipt is returned
            only if this whole projection succeeds; any later hold restores
            the prior durable receipt in the return path below.
            """

            if not initialized or hold_reasons:
                return
            receipt = shard.get("self_sender_receipt")
            if not isinstance(receipt, Mapping):
                return
            if _base_message_type(row["local_type"]) == 10000:
                return
            try:
                native_status = int(row["status"])
            except (TypeError, ValueError, OverflowError):
                return
            if native_status != _OUTGOING_MESSAGE_STATUS:
                return
            sender_key = _valid_sender_key(row["real_sender_id"])
            if sender_key is None:
                return
            key = str(shard["key"])
            candidate_commitment = self._opaque_sha256_commitment(
                GROUP_SELF_SENDER_RECEIPT_ALGORITHM,
                "sha256:" + self.account_identity_commitment,
                key,
                sender_key,
            )
            prior_commitment = receipt.get("selfSenderCommitment")
            if prior_commitment is not None:
                if not hmac.compare_digest(str(prior_commitment), candidate_commitment):
                    hold(
                        key,
                        self._group_projection_rowid(row),
                        "group_anchor_self_sender_drift",
                    )
                return
            previous = receipt_upgrade_senders.get(key)
            if previous is not None and previous != sender_key:
                hold(
                    key,
                    self._group_projection_rowid(row),
                    "group_anchor_self_sender_calibration_conflict",
                )
                return
            receipt_upgrade_senders[key] = sender_key
            shard["self_sender_receipt"] = self._group_self_sender_receipt(
                shard_key=key,
                calibrated_sender=sender_key,
            )
        for shard in shards:
            key = str(shard["key"])
            if shard.get("server_index") is None:
                hold(key, None, "group_anchor_server_lookup_index_unavailable")
                continue
            try:
                maximum = int(
                    shard["connection"].execute(
                        f"SELECT COALESCE(MAX(rowid),0) FROM {_quote_identifier(str(shard['table']))}"
                    ).fetchone()[0]
                )
            except (TypeError, ValueError, sqlite3.DatabaseError) as exc:
                raise DirectSchemaError("group projection rowid cursor is unavailable") from exc
            previous_high = normalized_prior.get(key, 0)
            if initialized and maximum < previous_high:
                hold(key, maximum + 1, "group_anchor_rowid_rollback")
                shard_highs[key] = previous_high
                continue
            if initialized:
                scan_starts[key] = max(0, previous_high - tail_rows)
                shard_highs[key] = previous_high
            else:
                scan_starts[key] = 0
                shard_highs[key] = 0
            scan_ends[key] = maximum

        missing_identity_rows = 0
        missing_identity_self_rows = 0
        self_signatures: dict[str, tuple[Any, ...]] = {}

        # Pass 1 is metadata-only and establishes a safe per-shard high water
        # mark before the selector opens any body or media payload.
        if not hold_reasons:
            for shard in shards:
                if hold_reasons:
                    break
                key = str(shard["key"])
                if key in held_shards:
                    continue
                stopped = False
                for page in self._iter_group_projection_metadata_pages(
                    shard,
                    start_rowid=scan_starts[key],
                    end_rowid=scan_ends[key],
                    page_size=page_size,
                ):
                    pass_rows[0] += len(page)
                    page_count += 1
                    max_page_rows = max(max_page_rows, len(page))
                    for row in page:
                        rowid = self._group_projection_rowid(row)
                        if rowid is None:
                            hold(key, None, "group_anchor_rowid_missing")
                            stopped = True
                            break
                        try:
                            native_time = int(row["create_time"])
                        except (TypeError, ValueError, OverflowError):
                            native_time = None
                        if native_time is not None and native_time > int(end_s):
                            hold(key, rowid, "group_anchor_future_cutoff_hole")
                            stopped = True
                            break
                        maybe_upgrade_unproven_sender_receipt(shard, row)
                        if hold_reasons:
                            stopped = True
                            break
                        _, relation_conflict = self._row_structured_quote_resolution(row)
                        if relation_conflict:
                            # A metadata-only app message can otherwise be
                            # silently skipped as unrelated while two native
                            # quote fields disagree about whether it touches
                            # the user.  Do not advance this shard cursor.
                            hold(key, rowid, "group_anchor_relation_conflict")
                            stopped = True
                            break
                        base_type, sender_role, _, _ = self._group_message_sender_role(
                            shard, row
                        )
                        if sender_role == "unknown":
                            hold(key, rowid, "group_anchor_sender_role_unproven")
                            stopped = True
                            break
                        if sender_role == "self" and native_time is None:
                            hold(key, rowid, "group_anchor_message_time_unproven")
                            stopped = True
                            break
                        identity = self._message_row_identity(row)
                        if identity is None:
                            missing_identity_rows += 1
                            if sender_role == "self":
                                missing_identity_self_rows += 1
                                hold(key, rowid, "group_anchor_self_identity_missing")
                                stopped = True
                                break
                        elif sender_role == "self" and identity[0] == "server":
                            signature = self._group_record_signature(
                                self._group_projection_record(shard, row)
                            )
                            existing = self_signatures.get(identity[1])
                            if existing is not None and existing != signature:
                                hold(key, rowid, "group_anchor_server_identity_conflict")
                                stopped = True
                                break
                            self_signatures[identity[1]] = signature
                        try:
                            sort_seq = int(row["sort_seq"])
                        except (TypeError, ValueError, OverflowError):
                            sort_seq = None
                        if sort_seq is not None:
                            max_sort_seq = (
                                sort_seq
                                if max_sort_seq is None
                                else max(max_sort_seq, sort_seq)
                            )
                        shard_highs[key] = max(shard_highs[key], rowid)
                    if stopped:
                        break

        selected_records: dict[tuple[str, str], dict[str, Any]] = {}

        def record_is_safe(record: Mapping[str, Any]) -> bool:
            key = str(record["shard"]["key"])
            return int(record["rowid"]) <= shard_highs.get(key, 0)

        def select_record(
            record: Mapping[str, Any],
            *,
            origin_shard_key: str,
            origin_rowid: int,
        ) -> bool:
            try:
                native_time = int(record["row"]["create_time"])
            except (TypeError, ValueError, OverflowError):
                hold(
                    origin_shard_key,
                    origin_rowid,
                    "group_anchor_message_time_unproven",
                )
                return False
            if native_time > int(end_s):
                hold(
                    origin_shard_key,
                    origin_rowid,
                    "group_anchor_future_cutoff_hole",
                )
                return False
            if not record_is_safe(record):
                hold(origin_shard_key, origin_rowid, "group_anchor_target_after_safe_cursor")
                return False
            _, relation_conflict = self._row_structured_quote_resolution(
                record["row"]
            )
            if relation_conflict:
                hold(origin_shard_key, origin_rowid, "group_anchor_relation_conflict")
                return False
            identity = self._group_record_identity(record["shard"], record["row"])
            if identity is None:
                hold(origin_shard_key, origin_rowid, "group_anchor_target_identity_missing")
                return False
            existing = selected_records.get(identity)
            if existing is not None:
                if (
                    str(existing["shard"]["key"]) == str(record["shard"]["key"])
                    and int(existing["rowid"]) == int(record["rowid"])
                ):
                    return True
                # The metadata signature catches the cheap common case, but a
                # same-server copy with an app body/title/media difference is
                # also an identity conflict.  Compare the exact selected
                # output inputs before choosing a shard deterministically.
                try:
                    same = (
                        self._group_record_exact_signature(existing)
                        == self._group_record_exact_signature(record)
                    )
                except DirectSchemaError:
                    same = False
                if not same:
                    hold(origin_shard_key, origin_rowid, "group_anchor_server_identity_conflict")
                    return False
            selected_records[identity] = dict(record)
            return True

        # Pass 2 performs the selection over exactly the safe interval.  Each
        # page asks the native SERVERID index only for explicit one-hop targets.
        if not hold_reasons:
            for shard in shards:
                if hold_reasons:
                    break
                key = str(shard["key"])
                if key in held_shards:
                    continue
                stopped = False
                for page in self._iter_group_projection_metadata_pages(
                    shard,
                    start_rowid=scan_starts[key],
                    end_rowid=shard_highs[key],
                    page_size=page_size,
                ):
                    pass_rows[1] += len(page)
                    page_count += 1
                    max_page_rows = max(max_page_rows, len(page))
                    records = [self._group_projection_record(shard, row) for row in page]
                    target_ids: set[str] = set()
                    for record in records:
                        row = record["row"]
                        quote_id, relation_conflict = self._row_structured_quote_resolution(
                            row
                        )
                        if relation_conflict:
                            hold(
                                key,
                                int(record["rowid"]),
                                "group_anchor_relation_conflict",
                            )
                            stopped = True
                            break
                        identity = self._message_row_identity(row)
                        if identity is not None and identity[0] == "server":
                            _, role, _, _ = self._group_message_sender_role(shard, row)
                            if role == "self":
                                target_ids.add(identity[1])
                        if quote_id:
                            target_ids.add(quote_id)
                    if stopped:
                        break
                    targets, conflicts = self._exact_group_server_records(shards, target_ids)
                    for record in records:
                        row = record["row"]
                        rowid = int(record["rowid"])
                        _, sender_role, _, _ = self._group_message_sender_role(
                            shard, row
                        )
                        if sender_role == "unknown":
                            hold(key, rowid, "group_anchor_sender_role_unproven")
                            stopped = True
                            break
                        quote_id, relation_conflict = self._row_structured_quote_resolution(
                            row
                        )
                        if relation_conflict:
                            hold(key, rowid, "group_anchor_relation_conflict")
                            stopped = True
                            break
                        identity = self._message_row_identity(row)
                        if identity is None:
                            if sender_role == "self":
                                hold(key, rowid, "group_anchor_self_identity_missing")
                                stopped = True
                                break
                            # A counterpart without either native identity
                            # can still be a direct reply/quote to the user.
                            # Resolve that explicit target before treating it
                            # as harmless unselected noise; otherwise the
                            # cursor would permanently skip relevant context.
                            if quote_id is not None:
                                if quote_id in conflicts:
                                    hold(
                                        key,
                                        rowid,
                                        "group_anchor_server_identity_conflict",
                                    )
                                    stopped = True
                                    break
                                target = targets.get(quote_id)
                                if target is None:
                                    hold(
                                        key,
                                        rowid,
                                        "group_anchor_relation_target_unresolved",
                                    )
                                    stopped = True
                                    break
                                _, target_role, _, _ = self._group_message_sender_role(
                                    target["shard"], target["row"]
                                )
                                if target_role == "unknown":
                                    hold(
                                        key,
                                        rowid,
                                        "group_anchor_relation_target_role_unproven",
                                    )
                                    stopped = True
                                    break
                                if target_role == "self":
                                    hold(
                                        key,
                                        rowid,
                                        "group_anchor_relation_identity_missing",
                                    )
                                    stopped = True
                                    break
                            continue
                        server_id = identity[1] if identity[0] == "server" else None
                        if sender_role == "self":
                            if server_id is not None:
                                if server_id in conflicts or server_id not in targets:
                                    hold(key, rowid, "group_anchor_server_identity_conflict")
                                    stopped = True
                                    break
                                if not select_record(
                                    targets[server_id],
                                    origin_shard_key=key,
                                    origin_rowid=rowid,
                                ):
                                    stopped = True
                                    break
                            elif not select_record(
                                record,
                                origin_shard_key=key,
                                origin_rowid=rowid,
                            ):
                                stopped = True
                                break
                            # A self quote/reply is already an explicit consent
                            # to include its one-hop context.  A truly absent old
                            # target stays an honest relation gap downstream;
                            # it does not cause a history scan.
                            if quote_id is not None:
                                if quote_id in conflicts:
                                    hold(key, rowid, "group_anchor_server_identity_conflict")
                                    stopped = True
                                    break
                                target = targets.get(quote_id)
                                if target is not None and not select_record(
                                    target,
                                    origin_shard_key=key,
                                    origin_rowid=rowid,
                                ):
                                    stopped = True
                                    break
                        elif quote_id is not None:
                            if quote_id in conflicts:
                                hold(key, rowid, "group_anchor_server_identity_conflict")
                                stopped = True
                                break
                            target = targets.get(quote_id)
                            if target is None:
                                # Whether this counterpart message touches the
                                # user is unknown, so no cursor may advance past
                                # it without a durable pending backlog.
                                hold(key, rowid, "group_anchor_relation_target_unresolved")
                                stopped = True
                                break
                            _, target_role, _, _ = self._group_message_sender_role(
                                target["shard"], target["row"]
                            )
                            if target_role == "unknown":
                                hold(key, rowid, "group_anchor_relation_target_role_unproven")
                                stopped = True
                                break
                            if target_role == "self":
                                if not select_record(
                                    record,
                                    origin_shard_key=key,
                                    origin_rowid=rowid,
                                ) or not select_record(
                                    target,
                                    origin_shard_key=key,
                                    origin_rowid=rowid,
                                ):
                                    stopped = True
                                    break
                    if stopped:
                        break

        # Metadata is sufficient for bounded selection, but the selected full
        # row is the only place where a body-level refermsg can disagree with
        # source/packed metadata.  Validate it before a cursor can be returned
        # or a durable quote relation emitted.
        selected_full_rows: dict[tuple[str, str], sqlite3.Row] = {}
        if not hold_reasons:
            for identity, record in selected_records.items():
                shard = record["shard"]
                row = self._fetch_group_projection_row(
                    connection=shard["connection"],
                    table=shard["table"],
                    rowid=int(record["rowid"]),
                    status_expr=shard["status_expr"],
                    origin_expr=shard["origin_expr"],
                )
                if self._group_record_identity(shard, row) != identity:
                    hold(
                        str(shard["key"]),
                        int(record["rowid"]),
                        "group_anchor_selected_identity_changed",
                    )
                    break
                _, relation_conflict = self._row_structured_quote_resolution(row)
                if relation_conflict:
                    hold(
                        str(shard["key"]),
                        int(record["rowid"]),
                        "group_anchor_relation_conflict",
                    )
                    break
                selected_full_rows[identity] = row

        # A held row makes this whole projection non-committable: callers have
        # one batch receipt, not independently durable shard receipts.  Return
        # only the prior durable cursors (or zero during first initialization)
        # and no selected bodies, so a later shard cannot be committed past a
        # held earlier shard.
        if hold_reasons:
            shard_highs = (
                dict(sorted(normalized_prior.items()))
                if initialized
                else {str(shard["key"]): 0 for shard in shards}
            )
            shard_self_sender_receipts = (
                dict(sorted(normalized_prior_receipts.items()))
                if initialized and len(normalized_prior_receipts) == len(shards)
                else {}
            )
            selected_records = {}
        else:
            selected_records = {
                identity: record
                for identity, record in selected_records.items()
                if record_is_safe(record)
            }
            selected_full_rows = {
                identity: row
                for identity, row in selected_full_rows.items()
                if identity in selected_records
            }
            shard_self_sender_receipts = {
                str(shard["key"]): dict(shard["self_sender_receipt"])
                for shard in shards
            }
        messages: dict[tuple[str, str], dict[str, Any]] = {}
        for identity, record in selected_records.items():
            shard = record["shard"]
            row = selected_full_rows.get(identity)
            if row is None:
                raise DirectSchemaError("selected group row is unavailable")
            messages[identity] = self._message_from_row(
                row=row,
                session_native_id=session_native_id,
                message_source=shard["source"],
                message_table=shard["table"],
                connection=shard["connection"],
                # Group selection needs roles but never an all-contact sender
                # map; a missing counterpart display name is an explicit gap.
                sender_index={},
                strict_group_projection=True,
                group_shard_key=str(shard["key"]),
                group_self_sender_receipt=shard.get("self_sender_receipt"),
            )
        ordered = sorted(
            messages.values(),
            key=lambda item: (
                item.get("createTime") is None,
                int(item.get("createTime") or 0),
                int(item.get("sortSeq") or 0),
                str(item.get("serverId") or item.get("localId") or ""),
            ),
        )
        return {
            "messages": ordered,
            "sync": {
                "hasMore": False,
                "watermark": end_s,
                "sortSeqWatermark": max_sort_seq,
                "scanMode": "group_anchor_two_pass_metadata",
                "scanPasses": 2,
                "pageSize": page_size,
                "pageCount": page_count,
                "maxPageRows": max_page_rows,
                "scannedRows": pass_rows,
                "retainedRows": len(messages),
                "missingIdentityRows": missing_identity_rows,
                "missingIdentitySelfRows": missing_identity_self_rows,
                "shardRowidHighs": dict(sorted(shard_highs.items())),
                "shardSelfSenderReceipts": dict(
                    sorted(shard_self_sender_receipts.items())
                ),
                "cursorHeld": bool(hold_reasons),
                "holdReasons": sorted(hold_reasons),
                "initialScanComplete": not initialized and not hold_reasons,
                "historicalMutationCoverage": (
                    "explicit_full_reconcile"
                    if full_reconcile
                    else "tail_only_requires_explicit_full_reconcile"
                    if initialized
                    else "initial_full_scan"
                ),
                "tailRows": tail_rows,
            },
        }

    def _private_messages_by_server_ids(
        self,
        *,
        session_native_id: str,
        message_table: str,
        message_connections: Sequence[tuple[Path, sqlite3.Connection]],
        server_ids: Iterable[str],
        exact_media_lookup: bool,
    ) -> dict[str, dict[str, Any]]:
        wanted = sorted({str(value).strip() for value in server_ids if str(value).strip()})
        if not wanted:
            return {}
        quoted_table = _quote_identifier(message_table)
        table_sources: list[
            tuple[Path, sqlite3.Connection, str, str, str | None]
        ] = []
        for source, connection in message_connections:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (message_table,),
            ).fetchone()
            if exists is None:
                continue
            columns = {
                str(column[1])
                for column in connection.execute(f"PRAGMA table_info({quoted_table})")
            }
            table_sources.append(
                (
                    source,
                    connection,
                    "status" if "status" in columns else "NULL",
                    "origin_source" if "origin_source" in columns else "NULL",
                    self._index_with_leading_columns(
                        connection, message_table, ("server_id",)
                    ),
                )
            )
        if not table_sources:
            raise SessionMessageDatabaseMissingError(
                "session message database is missing"
            )
        if len(table_sources) > 1 and any(item[4] is None for item in table_sources):
            raise DirectSchemaError("message_identity_lookup_index_unavailable")

        candidates: dict[
            str, list[tuple[Path, sqlite3.Connection, sqlite3.Row]]
        ] = {}
        for source, connection, status_expr, origin_expr, server_index in table_sources:
            indexed_by = (
                " INDEXED BY " + _quote_identifier(server_index)
                if server_index is not None
                else ""
            )
            for offset in range(0, len(wanted), 500):
                batch = wanted[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    "SELECT rowid AS _rowid, local_id, local_type, server_id, "
                    "real_sender_id, create_time, message_content, source, "
                    "packed_info_data, compress_content, sort_seq, "
                    f"{status_expr} AS status, {origin_expr} AS origin_source "
                    f"FROM {quoted_table}{indexed_by} "
                    f"WHERE server_id IN ({placeholders})",
                    batch,
                )
                for row in rows:
                    identity = self._message_row_identity(row)
                    if identity is not None and identity[0] == "server":
                        candidates.setdefault(identity[1], []).append(
                            (source, connection, row)
                        )
        return {
            server_id: self._project_private_message_candidates(
                records,
                session_native_id=session_native_id,
                message_table=message_table,
                exact_media_lookup=exact_media_lookup,
            )
            for server_id, records in candidates.items()
        }

    def fetch_message_by_server_id(
        self,
        session_native_id: str,
        server_id: str | int,
        *,
        exact_media_lookup: bool = False,
    ) -> dict[str, Any] | None:
        """Resolve one private-chat quote target by its native server ID."""

        folded = session_native_id.casefold()
        if folded.endswith("@chatroom") or folded.startswith("gh_"):
            raise DirectSchemaError(
                "exact quote lookup for this session requires an anchored projection"
            )
        if not self._session_is_registered(session_native_id):
            raise DirectSchemaError("session is not registered")
        normalized_id = str(server_id).strip()
        if not normalized_id:
            raise ValueError("message_server_id_invalid")
        table = "Msg_" + hashlib.md5(
            session_native_id.encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        messages = self._private_messages_by_server_ids(
            session_native_id=session_native_id,
            message_table=table,
            message_connections=self._message_connections(table),
            server_ids=[normalized_id],
            exact_media_lookup=exact_media_lookup,
        )
        return messages.get(normalized_id)

    def fetch_messages(
        self,
        session_native_id: str,
        *,
        since_s: int | None,
        end_s: int,
        since_sort_seq: int | None = None,
        limit: int | None = None,
        exact_media_lookup: bool = False,
        allow_unindexed_time_fallback: bool = False,
        before_key: Sequence[Any] | None = None,
        around_s: int | None = None,
    ) -> dict[str, Any]:
        if limit is not None:
            limit = int(limit)
            if limit < 1:
                raise ValueError("message_fetch_limit_invalid")
        if before_key is not None:
            key_offset = int(around_s is not None)
            if (
                limit is None
                or len(before_key) != 5 + key_offset
                or any(type(value) is not int for value in before_key[:3 + key_offset])
                or before_key[key_offset] not in (0, 1)
                or any(not isinstance(value, str) for value in before_key[3 + key_offset:])
                or before_key[3 + key_offset] not in {"server", "local", "row"}
            ):
                raise ValueError("message_page_cursor_invalid")
            before_key = tuple(before_key)
        table = "Msg_" + hashlib.md5(
            session_native_id.encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        quoted_table = _quote_identifier(table)

        def newest_key(item: tuple[Path, sqlite3.Row]) -> tuple[Any, ...]:
            row = item[1]
            identity = self._private_message_record_identity(item[0], table, row)
            key = (
                int(row["create_time"] is not None),
                int(row["create_time"]) if row["create_time"] is not None else -1,
                int(row["sort_seq"]) if row["sort_seq"] is not None else -1,
                *identity,
            )
            return (-abs(key[1] - around_s), *key) if around_s is not None else key

        rows: list[tuple[Path, sqlite3.Row]] = []
        table_found = False
        message_connections = self._message_connections(table)
        connection_by_source = dict(message_connections)
        for source, connection in message_connections:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists is None:
                continue
            table_found = True
            columns = {
                str(column[1])
                for column in connection.execute(f"PRAGMA table_info({quoted_table})")
            }
            status_expr = "status" if "status" in columns else "NULL"
            origin_expr = (
                "origin_source" if "origin_source" in columns else "NULL"
            )
            predicates: list[tuple[str, list[Any], str | None]] = []
            if since_s is not None and since_sort_seq is not None:
                sort_index = self._index_with_leading_columns(
                    connection, table, ("sort_seq",)
                )
                if sort_index is None:
                    raise DirectSchemaError("incremental_sort_seq_index_unavailable")
                current_max_sort = connection.execute(
                    f"SELECT max(sort_seq) FROM {quoted_table} INDEXED BY "
                    f"{_quote_identifier(sort_index)}"
                ).fetchone()[0]
                if (
                    current_max_sort is not None
                    and int(current_max_sort) < int(since_sort_seq)
                ):
                    raise DirectSchemaError(
                        "sort_seq_cursor_regressed_requires_full_reconcile"
                    )
                time_index = self._index_with_leading_columns(
                    connection, table, ("create_time",)
                )
                if time_index is None:
                    raise DirectSchemaError("incremental_time_index_unavailable")
                predicates.extend(
                    (
                        (
                            "sort_seq > ? AND (create_time <= ? OR create_time IS NULL)",
                            [int(since_sort_seq), int(end_s)],
                            sort_index,
                        ),
                        (
                            "sort_seq <= ? AND create_time >= ? AND create_time <= ?",
                            [int(since_sort_seq), int(since_s), int(end_s)],
                            time_index,
                        ),
                    )
                )
            elif since_sort_seq is not None:
                sort_index = self._index_with_leading_columns(
                    connection, table, ("sort_seq",)
                )
                if sort_index is None:
                    raise DirectSchemaError("incremental_sort_seq_index_unavailable")
                current_max_sort = connection.execute(
                    f"SELECT max(sort_seq) FROM {quoted_table} INDEXED BY "
                    f"{_quote_identifier(sort_index)}"
                ).fetchone()[0]
                if (
                    current_max_sort is not None
                    and int(current_max_sort) < int(since_sort_seq)
                ):
                    raise DirectSchemaError(
                        "sort_seq_cursor_regressed_requires_full_reconcile"
                    )
                predicates.append(
                    (
                        "sort_seq > ? AND (create_time <= ? OR create_time IS NULL)",
                        [int(since_sort_seq), int(end_s)],
                        sort_index,
                    )
                )
            elif since_s is not None:
                time_index = self._index_with_leading_columns(
                    connection, table, ("create_time",)
                )
                if time_index is None:
                    if not allow_unindexed_time_fallback or limit is None:
                        raise DirectSchemaError(
                            "incremental_time_index_unavailable"
                        )
                    sort_index = self._index_with_leading_columns(
                        connection, table, ("sort_seq",)
                    )
                    if sort_index is None:
                        raise DirectSchemaError(
                            "bounded_context_sort_index_unavailable"
                        )
                    predicates.append(
                        (
                            "create_time >= ? AND create_time <= ?",
                            [int(since_s), int(end_s)],
                            sort_index,
                        )
                    )
                else:
                    predicates.append(
                        (
                            "create_time >= ? AND create_time <= ?",
                            [int(since_s), int(end_s)],
                            time_index,
                        )
                    )
            else:
                predicates.append(
                    (
                        "(create_time <= ? OR create_time IS NULL)",
                        [int(end_s)],
                        None,
                    )
                )
            for where, params, index_name in predicates:
                order_by = (
                    " ORDER BY (create_time IS NOT NULL) DESC, "
                    "coalesce(create_time, -1) DESC, coalesce(sort_seq, -1) DESC, "
                    "CASE WHEN server_id IS NOT NULL AND CAST(server_id AS TEXT) != '0' "
                    "THEN 'server' WHEN local_id IS NOT NULL AND CAST(local_id AS TEXT) != '0' "
                    "THEN 'local' ELSE 'row' END COLLATE BINARY DESC, "
                    "CASE WHEN server_id IS NOT NULL AND CAST(server_id AS TEXT) != '0' "
                    "THEN CAST(server_id AS TEXT) "
                    "WHEN local_id IS NOT NULL AND CAST(local_id AS TEXT) != '0' "
                    "THEN CAST(local_id AS TEXT) ELSE CAST(rowid AS TEXT) END "
                    "COLLATE BINARY DESC"
                    if limit is not None
                    else ""
                )
                indexed_by = (
                    " INDEXED BY " + _quote_identifier(index_name)
                    if index_name is not None
                    else ""
                )
                if around_s is not None and limit is not None:
                    order_by = order_by.replace(
                        " ORDER BY ", " ORDER BY abs(coalesce(create_time, -1) - ?) ASC, ", 1
                    )
                    params = [*params, int(around_s)]
                cursor = connection.execute(
                    "SELECT rowid AS _rowid, local_id, local_type, server_id, "
                    "real_sender_id, create_time, "
                    "message_content, source, packed_info_data, compress_content, sort_seq, "
                    f"{status_expr} AS status, {origin_expr} AS origin_source "
                    f"FROM {quoted_table}{indexed_by} WHERE {where}{order_by}",
                    params,
                )
                predicate_identities: set[tuple[str, str]] = set()
                try:
                    for page in _iter_cursor_pages(
                        cursor, page_size=MESSAGE_FETCH_PAGE_SIZE
                    ):
                        eligible = [
                            (source, row)
                            for row in page
                            if before_key is None or newest_key((source, row)) < before_key
                        ]
                        rows.extend(eligible)
                        if limit is not None:
                            predicate_identities.update(
                                self._private_message_record_identity(source, table, row)
                                for _, row in eligible
                            )
                            if len(predicate_identities) > limit:
                                break
                finally:
                    cursor.close()
        if not table_found and self._session_is_registered(session_native_id):
            raise SessionMessageDatabaseMissingError(
                "session message database is missing"
            )
        has_more = False
        next_before_key = None
        if limit is not None:
            selected_identities: list[tuple[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for item in sorted(rows, key=newest_key, reverse=True):
                identity = self._private_message_record_identity(item[0], table, item[1])
                if identity in seen:
                    continue
                seen.add(identity)
                if len(selected_identities) == limit:
                    has_more = True
                    break
                selected_identities.append(identity)
                next_before_key = list(newest_key(item))
            selected = set(selected_identities)
            rows = [
                item
                for item in rows
                if self._private_message_record_identity(item[0], table, item[1])
                in selected
            ]
        grouped: dict[
            tuple[str, str], list[tuple[Path, sqlite3.Connection, sqlite3.Row]]
        ] = {}
        for message_source, row in rows:
            identity = self._private_message_record_identity(
                message_source, table, row
            )
            grouped.setdefault(identity, []).append(
                (message_source, connection_by_source[message_source], row)
            )
        messages: dict[tuple[str, str], dict[str, Any]] = {}
        page_keys = {
            identity: list(max(newest_key((source, row)) for source, _, row in candidates))
            for identity, candidates in grouped.items()
        } if limit is not None else {}
        exact_messages = (
            self._private_messages_by_server_ids(
                session_native_id=session_native_id,
                message_table=table,
                message_connections=message_connections,
                server_ids=[identity[1] for identity in grouped if identity[0] == "server"],
                exact_media_lookup=exact_media_lookup,
            )
            if limit is not None
            else {}
        )
        for identity, candidates in grouped.items():
            if limit is not None and identity[0] == "server":
                exact = exact_messages.get(identity[1])
                if exact is None:
                    raise DirectSchemaError("selected message identity disappeared")
                messages[identity] = exact
            else:
                messages[identity] = self._project_private_message_candidates(
                    candidates,
                    session_native_id=session_native_id,
                    message_table=table,
                    exact_media_lookup=exact_media_lookup,
                )
            if limit is not None:
                messages[identity]["_pageKey"] = page_keys[identity]
        ordered = sorted(
            messages.values(),
            key=lambda item: (
                tuple(item["_pageKey"][1:] if around_s is not None else item["_pageKey"])
                if limit is not None else (
                    item.get("createTime") is None,
                    int(item.get("createTime") or 0),
                    int(item.get("sortSeq") or 0),
                    str(item.get("serverId") or item.get("localId") or ""),
                )
            ),
        )
        sort_watermarks = []
        for message in ordered:
            try:
                sort_watermarks.append(int(message["sortSeq"]))
            except (KeyError, TypeError, ValueError):
                continue
        if since_sort_seq is not None:
            sort_watermarks.append(int(since_sort_seq))
        return {
            "messages": ordered,
            "sync": {
                "hasMore": has_more,
                "nextBeforeKey": next_before_key if has_more else None,
                "watermark": end_s if not has_more and before_key is None else None,
                "sortSeqWatermark": (
                    max(sort_watermarks, default=None)
                    if not has_more and before_key is None else None
                ),
            },
        }


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _quote_identifier(identifier: str) -> str:
    if not isinstance(identifier, str) or not identifier or "\x00" in identifier:
        raise DirectSchemaError("SQL identifier is invalid")
    return '"' + identifier.replace('"', '""') + '"'


__all__ = [
    "CryptoUnavailableError",
    "DirectCredentialError",
    "DirectWeChatReader",
    "DirectSchemaError",
    "EncryptedPageCodec",
    "EncryptedPageError",
    "MediaNotOpenableError",
    "PBKDF2_ROUNDS",
    "PAGE_SIZE",
    "RESERVE_SIZE",
    "SnapshotCopyError",
    "SessionMessageDatabaseMissingError",
    "WeChatDirectError",
    "derive_page_key",
    "load_direct_source_identity",
]
