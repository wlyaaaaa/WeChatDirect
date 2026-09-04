from __future__ import annotations

import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from PIL import Image

from wechat_media import (
    EmojiStoreRecord,
    LocalMedia,
    fetch_emoji_media,
    open_emoji_media,
)


def _valid_image(format_name: str) -> bytes:
    stream = io.BytesIO()
    Image.new("RGBA", (1, 1), (12, 34, 56, 255)).save(stream, format=format_name)
    return stream.getvalue()


EMOJI_MD5 = "a" * 32
PNG = _valid_image("PNG")
GIF = _valid_image("GIF")


class _FakeResponse:
    def __init__(self, payload: bytes, *, content_length: int | None = None):
        self.payload = payload
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.read_limit: int | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit: int = -1) -> bytes:
        self.read_limit = limit
        return self.payload if limit < 0 else self.payload[:limit]


class _FakeOpener:
    def __init__(self, response: _FakeResponse | Exception):
        self.response = response
        self.requests = []

    def open(self, request, *, timeout: float):
        self.requests.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class EmojiMediaTests(unittest.TestCase):
    def _nonstore_path(self, root: Path, *, thumb: bool = False) -> Path:
        base = root / "business" / "emoticon"
        if thumb:
            return base / "Thumb" / EMOJI_MD5[:2] / f"{EMOJI_MD5}.thumb"
        return base / "Persist" / EMOJI_MD5[:2] / EMOJI_MD5

    def test_reads_verified_nonstore_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._nonstore_path(root)
            source.parent.mkdir(parents=True)
            source.write_bytes(PNG)

            media = open_emoji_media(account_root=root, emoji_md5=EMOJI_MD5)

            self.assertEqual(
                media,
                LocalMedia(PNG, "emoji.png", "image/png", "original"),
            )

    def test_uses_thumbnail_only_when_original_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._nonstore_path(root, thumb=True)
            source.parent.mkdir(parents=True)
            source.write_bytes(GIF)

            media = open_emoji_media(account_root=root, emoji_md5=EMOJI_MD5)

            self.assertEqual(media, LocalMedia(GIF, "emoji.gif", "image/gif", "thumbnail"))

    def test_reads_store_original_by_native_offset_and_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_id = "package-test"
            package_hash = hashlib.md5(
                package_id.encode("utf-8"), usedforsecurity=False
            ).hexdigest()
            package = (
                root
                / "business"
                / "emoticon"
                / "PersistStore"
                / package_hash[:2]
                / package_hash
            )
            package.parent.mkdir(parents=True)
            package.write_bytes(b"prefix" + PNG + b"thumbnail")
            store = EmojiStoreRecord(
                package_id=package_id,
                md5=EMOJI_MD5,
                emoticon_offset=6,
                emoticon_size=len(PNG),
                thumb_offset=6 + len(PNG),
                thumb_size=len(b"thumbnail"),
            )

            media = open_emoji_media(
                account_root=root,
                emoji_md5=EMOJI_MD5,
                store=store,
            )

            self.assertEqual(
                media,
                LocalMedia(PNG, "emoji.png", "image/png", "original"),
            )

    def test_uses_store_thumbnail_when_original_slice_is_not_visual(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_id = "package-thumb"
            package_hash = hashlib.md5(
                package_id.encode("utf-8"), usedforsecurity=False
            ).hexdigest()
            package = (
                root
                / "business"
                / "emoticon"
                / "PersistStore"
                / package_hash[:2]
                / package_hash
            )
            package.parent.mkdir(parents=True)
            package.write_bytes(b"not-an-image" + GIF)
            store = EmojiStoreRecord(
                package_id=package_id,
                md5=EMOJI_MD5,
                emoticon_offset=0,
                emoticon_size=len(b"not-an-image"),
                thumb_offset=len(b"not-an-image"),
                thumb_size=len(GIF),
            )

            media = open_emoji_media(
                account_root=root,
                emoji_md5=EMOJI_MD5,
                store=store,
            )

            self.assertEqual(media, LocalMedia(GIF, "emoji.gif", "image/gif", "thumbnail"))

    def test_decoder_must_return_a_known_visual_signature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._nonstore_path(root)
            source.parent.mkdir(parents=True)
            source.write_bytes(b"wrapped")

            media = open_emoji_media(
                account_root=root,
                emoji_md5=EMOJI_MD5,
                aes_key="not-recorded-in-result",
                decode_blob=lambda payload, key: PNG if payload == b"wrapped" and key else None,
            )

            self.assertEqual(media, LocalMedia(PNG, "emoji.png", "image/png", "original"))

    def test_rejects_invalid_md5_and_out_of_bounds_store_slice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(open_emoji_media(account_root=root, emoji_md5="../bad"))
            self.assertIsNone(
                open_emoji_media(
                    account_root=root,
                    emoji_md5=EMOJI_MD5,
                    store=EmojiStoreRecord("package", EMOJI_MD5, 0, 1, 0, 1),
                )
            )

    def test_truncated_or_invalid_local_magic_stays_a_gap(self):
        for payload in (PNG[:12], b"GIF89a-not-a-complete-gif"):
            with self.subTest(payload=payload[:6]), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self._nonstore_path(root)
                source.parent.mkdir(parents=True)
                source.write_bytes(payload)

                self.assertIsNone(open_emoji_media(account_root=root, emoji_md5=EMOJI_MD5))


class FetchEmojiMediaTests(unittest.TestCase):
    URL = "https://mmbiz.qpic.cn/native-emoji?opaque-query=kept"

    def setUp(self):
        self.payload = PNG
        self.md5 = hashlib.md5(self.payload, usedforsecurity=False).hexdigest()

    def test_fetches_bound_png_without_rewriting_url(self):
        response = _FakeResponse(self.payload, content_length=len(self.payload))
        opener = _FakeOpener(response)
        with patch("wechat_media.build_opener", return_value=opener) as build:
            media = fetch_emoji_media(
                emoji_md5=self.md5,
                native_url=self.URL,
                declared_size=len(self.payload),
                timeout=7,
            )

        self.assertEqual(
            media,
            LocalMedia(self.payload, "emoji.png", "image/png", "original", "remote"),
        )
        build.assert_called_once()
        self.assertEqual(opener.requests[0][0].full_url, self.URL)
        self.assertEqual(opener.requests[0][1], 7.0)
        self.assertEqual(response.read_limit, len(self.payload) + 1)

    def test_rejects_wrong_md5_and_size(self):
        for md5, size in (("0" * 32, len(self.payload)), (self.md5, len(self.payload) + 1)):
            with self.subTest(md5=md5, size=size):
                opener = _FakeOpener(_FakeResponse(self.payload))
                with patch("wechat_media.build_opener", return_value=opener):
                    self.assertIsNone(
                        fetch_emoji_media(
                            emoji_md5=md5,
                            native_url=self.URL,
                            declared_size=size,
                        )
                    )

    def test_rejects_untrusted_domain_without_opening(self):
        with patch("wechat_media.build_opener") as build:
            self.assertIsNone(
                fetch_emoji_media(
                    emoji_md5=self.md5,
                    native_url="https://example.com/not-native",
                    declared_size=len(self.payload),
                )
            )
        build.assert_not_called()

    def test_rejects_redirect_response(self):
        redirect = HTTPError(self.URL, 302, "redirect", {}, None)
        opener = _FakeOpener(redirect)
        with patch("wechat_media.build_opener", return_value=opener):
            self.assertIsNone(
                fetch_emoji_media(
                    emoji_md5=self.md5,
                    native_url=self.URL,
                    declared_size=len(self.payload),
                )
            )
        self.assertEqual(opener.requests[0][0].full_url, self.URL)
