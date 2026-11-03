from __future__ import annotations

import isolation  # noqa: F401

import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from PIL import Image
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechat_media import (
    EmojiStoreRecord,
    LocalMedia,
    emoji_cache_paths,
    fetch_emoji_media,
    open_cached_emoji_media,
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
    def test_monthly_cache_decrypts_only_verified_message_bytes(self):
        key = b"synthetic-key-16"
        md5 = hashlib.md5(GIF, usedforsecurity=False).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for month in ("2026-10", "2026-11"):
                source = root / "cache" / month / "Emoticon" / md5[:2] / md5
                source.parent.mkdir(parents=True)
                padder = padding.PKCS7(128).padder()
                padded = padder.update(GIF) + padder.finalize()
                encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
                source.write_bytes(encryptor.update(padded) + encryptor.finalize())
            self.assertIn("2026-11", str(emoji_cache_paths(root, md5)[0]))
            wrong_md5_path = root / "cache" / "2026-11" / "Emoticon" / "00" / ("0" * 32)
            wrong_md5_path.parent.mkdir(parents=True)
            wrong_md5_path.write_bytes(source.read_bytes())
            media = open_cached_emoji_media(account_root=root, emoji_md5=md5,
                                            cache_key=key, declared_size=len(GIF))
            self.assertEqual(media, LocalMedia(GIF, "emoji.gif", "image/gif", "original"))
            for wrong_key, wrong_md5, size in ((b"wrong-key-16byte", md5, len(GIF)),
                                                (key, "0" * 32, len(GIF)),
                                                (key, md5, len(GIF) + 1)):
                with self.subTest(key=wrong_key == key, md5=wrong_md5 == md5, size=size):
                    self.assertIsNone(open_cached_emoji_media(
                        account_root=root, emoji_md5=wrong_md5, cache_key=wrong_key,
                        declared_size=size))
            # A correct MD5 and padding cannot make non-visual bytes an image.
            invalid = b"not-a-gif"
            bad_md5 = hashlib.md5(invalid, usedforsecurity=False).hexdigest()
            bad = root / "cache" / "2026-11" / "Emoticon" / bad_md5[:2] / bad_md5
            bad.parent.mkdir(parents=True, exist_ok=True)
            padder = padding.PKCS7(128).padder()
            encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
            bad.write_bytes(encryptor.update(padder.update(invalid) + padder.finalize()) + encryptor.finalize())
            self.assertIsNone(open_cached_emoji_media(account_root=root, emoji_md5=bad_md5, cache_key=key))

    def test_monthly_cache_never_reads_another_account_or_invalid_md5(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "primary"
            primary.mkdir()
            source = root / "secondary" / "cache" / "2026-11" / "Emoticon" / "aa" / EMOJI_MD5
            source.parent.mkdir(parents=True)
            source.write_bytes(b"private-other-account")
            with patch.object(Path, "open", side_effect=AssertionError("must not read bytes")):
                self.assertEqual(emoji_cache_paths(primary, EMOJI_MD5), ())
                self.assertEqual(emoji_cache_paths(primary, "../" + EMOJI_MD5), ())
                self.assertIsNone(open_cached_emoji_media(
                    account_root=primary, emoji_md5=EMOJI_MD5, cache_key=b"synthetic-key-16"))
            (primary / "cache").mkdir()
            junction = primary / "cache" / "2026-11"
            if os.name == "nt":
                linked = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction),
                                         str(source.parents[2])], capture_output=True)
                self.assertEqual(linked.returncode, 0, linked.stderr)
            else:
                junction.symlink_to(source.parents[2], target_is_directory=True)
            try:
                with patch.object(Path, "open", side_effect=AssertionError("must not follow another account")):
                    self.assertEqual(emoji_cache_paths(primary, EMOJI_MD5), ())
            finally:
                junction.rmdir() if os.name == "nt" else junction.unlink()

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
