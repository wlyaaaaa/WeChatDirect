"""Read precisely located local media payloads for WeChatDirect.

The module deliberately owns only small, source-local byte readers.  Database
lookups and message identity validation remain in :mod:`wechat_source` so a
file candidate can never establish a chat binding by itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import re
from typing import Final, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image


MediaQuality = Literal["original", "thumbnail"]
BlobDecoder = Callable[[bytes, str | None], bytes | None]

_MD5 = re.compile(r"[0-9a-f]{32}")
MAX_REMOTE_EMOJI_BYTES: Final = 32 * 1024 * 1024
_PIL_VISUAL_FORMATS: Final = {
    "BMP": ("image/bmp", "bmp"),
    "GIF": ("image/gif", "gif"),
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "TIFF": ("image/tiff", "tiff"),
    "WEBP": ("image/webp", "webp"),
}


@dataclass(frozen=True)
class LocalMedia:
    """A verified, directly usable local media payload."""

    payload: bytes
    file_name: str
    mime_type: str
    quality: MediaQuality
    source: str = "local"


@dataclass(frozen=True)
class EmojiStoreRecord:
    """The native byte ranges for one Store-emoticon package entry."""

    package_id: str
    md5: str
    emoticon_offset: int
    emoticon_size: int
    thumb_offset: int
    thumb_size: int


class _NoRedirect(HTTPRedirectHandler):
    """Turn an HTTP redirect into a bounded read failure."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def fetch_emoji_media(
    *,
    emoji_md5: str,
    native_url: str,
    declared_size: int | None = None,
    timeout: float = 15,
) -> LocalMedia | None:
    """Read one already-bound native emoji CDN asset without following redirects.

    The caller must have obtained ``native_url``, ``emoji_md5``, and optional
    ``declared_size`` from the same selected native message/record.  This
    function never derives URLs, retries, persists a cache, or returns URL
    details to consumers.  It validates the downloaded bytes before returning
    a remote ``LocalMedia`` value.
    """

    expected_md5 = _normalize_md5(emoji_md5)
    size_limit = _remote_size_limit(declared_size)
    if (
        expected_md5 is None
        or size_limit is None
        or not _is_native_emoji_cdn_url(native_url)
    ):
        return None
    try:
        timeout_seconds = float(timeout)
    except (TypeError, ValueError):
        return None
    if timeout_seconds <= 0:
        return None

    request = Request(
        native_url,
        headers={"User-Agent": "WeChatDirect/0.1 explicit-media-read"},
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=timeout_seconds) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                advertised_size = int(content_length)
                if advertised_size < 0 or advertised_size > size_limit:
                    return None
                if declared_size is not None and advertised_size != declared_size:
                    return None
            payload = response.read(size_limit + 1)
    except HTTPError as exc:
        exc.close()
        return None
    except (URLError, OSError, ValueError):
        return None

    if not 0 < len(payload) <= size_limit:
        return None
    if declared_size is not None and len(payload) != declared_size:
        return None
    if hashlib.md5(payload, usedforsecurity=False).hexdigest() != expected_md5:
        return None
    visual = _verify_pillow_visual(payload)
    if visual is None:
        return None
    mime_type, extension = visual
    return LocalMedia(
        payload=payload,
        file_name=f"emoji.{extension}",
        mime_type=mime_type,
        quality="original",
        source="remote",
    )


def open_emoji_media(
    *,
    account_root: Path | str,
    emoji_md5: str,
    store: EmojiStoreRecord | None = None,
    aes_key: str | None = None,
    decode_blob: BlobDecoder | None = None,
) -> LocalMedia | None:
    """Return one locally available emoji asset or ``None``.

    ``emoji_md5`` must already have been proven from the selected message and
    its native emoticon record.  The normal Persist asset is preferred over a
    thumbnail.  Store packages are read only through their native offsets.
    ``decode_blob`` is optional because some current WeChat builds wrap local
    assets in a version-specific envelope; it receives only the selected
    candidate bytes and optional per-message key, and its result must still
    have a standard visual signature before it is returned.
    """

    normalized_md5 = _normalize_md5(emoji_md5)
    if normalized_md5 is None:
        return None
    root = Path(account_root)
    try:
        root_resolved = root.resolve(strict=True)
    except OSError:
        return None

    if store is not None and _normalize_md5(store.md5) == normalized_md5:
        store_media = _open_store_emoji(
            root_resolved,
            store,
            aes_key=aes_key,
            decode_blob=decode_blob,
        )
        if store_media is not None:
            return store_media

    return _open_nonstore_emoji(
        root_resolved,
        normalized_md5,
        aes_key=aes_key,
        decode_blob=decode_blob,
    )


def _open_nonstore_emoji(
    root: Path,
    emoji_md5: str,
    *,
    aes_key: str | None,
    decode_blob: BlobDecoder | None,
) -> LocalMedia | None:
    base = root / "business" / "emoticon"
    candidates = (
        ("original", base / "Persist" / emoji_md5[:2] / emoji_md5),
        ("thumbnail", base / "Thumb" / emoji_md5[:2] / f"{emoji_md5}.thumb"),
    )
    for quality, candidate in candidates:
        payload = _read_file_within(root, candidate)
        if payload is None:
            continue
        media = _as_local_media(
            payload,
            quality=quality,
            aes_key=aes_key,
            decode_blob=decode_blob,
        )
        if media is not None:
            return media
    return None


def _open_store_emoji(
    root: Path,
    store: EmojiStoreRecord,
    *,
    aes_key: str | None,
    decode_blob: BlobDecoder | None,
) -> LocalMedia | None:
    if not isinstance(store.package_id, str) or not store.package_id:
        return None
    package_hash = hashlib.md5(
        store.package_id.encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    package = (
        root
        / "business"
        / "emoticon"
        / "PersistStore"
        / package_hash[:2]
        / package_hash
    )
    for quality, offset, size in (
        ("original", store.emoticon_offset, store.emoticon_size),
        ("thumbnail", store.thumb_offset, store.thumb_size),
    ):
        payload = _read_file_range_within(root, package, offset, size)
        if payload is None:
            continue
        media = _as_local_media(
            payload,
            quality=quality,
            aes_key=aes_key,
            decode_blob=decode_blob,
        )
        if media is not None:
            return media
    return None


def _as_local_media(
    payload: bytes,
    *,
    quality: MediaQuality,
    aes_key: str | None,
    decode_blob: BlobDecoder | None,
) -> LocalMedia | None:
    visual = _verify_pillow_visual(payload)
    if visual is None and decode_blob is not None:
        try:
            decoded = decode_blob(payload, aes_key)
        except Exception:
            decoded = None
        if isinstance(decoded, bytes):
            payload = decoded
            visual = _verify_pillow_visual(payload)
    if visual is None:
        return None
    mime_type, extension = visual
    return LocalMedia(
        payload=payload,
        file_name=f"emoji.{extension}",
        mime_type=mime_type,
        quality=quality,
    )


def _normalize_md5(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.casefold()
    return normalized if _MD5.fullmatch(normalized) else None


def _read_file_within(root: Path, candidate: Path) -> bytes | None:
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            return None
        return resolved.read_bytes()
    except (OSError, ValueError):
        return None


def _read_file_range_within(
    root: Path, candidate: Path, offset: object, size: object
) -> bytes | None:
    try:
        offset = int(offset)
        size = int(size)
    except (TypeError, ValueError, OverflowError):
        return None
    if offset < 0 or size <= 0:
        return None
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file() or offset + size > resolved.stat().st_size:
            return None
        with resolved.open("rb") as stream:
            stream.seek(offset)
            payload = stream.read(size)
    except (OSError, ValueError):
        return None
    return payload if len(payload) == size else None


def _remote_size_limit(declared_size: int | None) -> int | None:
    if declared_size is None:
        return MAX_REMOTE_EMOJI_BYTES
    if type(declared_size) is not int or not 0 < declared_size <= MAX_REMOTE_EMOJI_BYTES:
        return None
    return declared_size


def _is_native_emoji_cdn_url(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    return (
        parsed.scheme in {"http", "https"}
        and parsed.username is None
        and parsed.password is None
        and (
            host == "qpic.cn"
            or host.endswith(".qpic.cn")
            or host == "qq.com"
            or host.endswith(".qq.com")
        )
    )


def _verify_pillow_visual(payload: bytes) -> tuple[str, str] | None:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            format_name = (image.format or "").upper()
    except Exception:
        return None
    return _PIL_VISUAL_FORMATS.get(format_name)
