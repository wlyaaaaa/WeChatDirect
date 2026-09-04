from __future__ import annotations

import struct
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechat_image import (
    DAT_V1_MAGIC,
    DAT_V2_MAGIC,
    V1_PUBLIC_AES_KEY,
    WXGF_MAGIC,
    decode_wechat_dat,
)


JPEG_PREFIX = b"\xff\xd8\xffsynthetic"
JPEG_TAIL = b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\nsynthetic"
GIF = b"GIF89asynthetic"
WEBP = b"RIFF\x00\x00\x00\x00WEBPsynthetic"


def _pkcs7(value: bytes) -> bytes:
    padding = 16 - len(value) % 16
    return value + bytes([padding]) * padding


def _encrypt_ecb(value: bytes, key: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(value) + encryptor.finalize()


def _modern_payload(
    *,
    magic: bytes,
    key: bytes,
    aes_plaintext: bytes,
    middle: bytes,
    tail: bytes,
    xor_key: int,
) -> bytes:
    header = magic + struct.pack("<I", len(aes_plaintext)) + struct.pack(
        "<I", len(tail)
    ) + b"\x00"
    return header + _encrypt_ecb(_pkcs7(aes_plaintext), key) + middle + bytes(
        item ^ xor_key for item in tail
    )


class DecodeWechatDatTests(unittest.TestCase):
    def test_returns_standard_visual_payloads_without_rewriting(self):
        for payload in (PNG, GIF, WEBP, b"BMsynthetic", b"II*\x00synthetic"):
            with self.subTest(payload=payload[:4]):
                self.assertEqual(decode_wechat_dat(payload), payload)

    def test_v1_modern_layout_decrypts_and_infers_jpeg_tail_xor(self):
        payload = _modern_payload(
            magic=DAT_V1_MAGIC,
            key=V1_PUBLIC_AES_KEY,
            aes_plaintext=JPEG_PREFIX,
            middle=b"middle",
            tail=JPEG_TAIL,
            xor_key=0x37,
        )

        self.assertEqual(
            decode_wechat_dat(payload), JPEG_PREFIX + b"middle" + JPEG_TAIL
        )

    def test_v2_requires_exact_caller_key(self):
        key = b"0123456789abcdef"
        payload = _modern_payload(
            magic=DAT_V2_MAGIC,
            key=key,
            aes_plaintext=JPEG_PREFIX,
            middle=b"",
            tail=JPEG_TAIL,
            xor_key=0x6A,
        )

        self.assertIsNone(decode_wechat_dat(payload))
        self.assertIsNone(decode_wechat_dat(payload, aes_key=b"x" * 16))
        self.assertEqual(decode_wechat_dat(payload, aes_key=key), JPEG_PREFIX + JPEG_TAIL)

    def test_explicit_xor_key_handles_non_jpeg_tail(self):
        key = b"0123456789abcdef"
        payload = _modern_payload(
            magic=DAT_V2_MAGIC,
            key=key,
            aes_plaintext=GIF,
            middle=b"tail-without-known-marker",
            tail=b"?",
            xor_key=0x51,
        )

        self.assertIsNone(decode_wechat_dat(payload, aes_key=key))
        self.assertEqual(
            decode_wechat_dat(payload, aes_key=key, xor_key=0x51),
            GIF + b"tail-without-known-marker?",
        )

    def test_rejects_bad_padding_and_invalid_header_bounds(self):
        key = b"0123456789abcdef"
        malformed_padding = b"A" * 14 + b"\x02\x03"
        header = DAT_V2_MAGIC + struct.pack("<I", 14) + struct.pack("<I", 0) + b"\x00"
        bad_padding = header + _encrypt_ecb(malformed_padding, key)
        bad_bounds = DAT_V2_MAGIC + struct.pack("<I", 99) + struct.pack("<I", 0) + b"\x00"

        self.assertIsNone(decode_wechat_dat(bad_padding, aes_key=key))
        self.assertIsNone(decode_wechat_dat(bad_bounds, aes_key=key))

    def test_legacy_xor_requires_full_leading_magic_validation(self):
        key = 0x4D
        encrypted = bytes(item ^ key for item in PNG)

        self.assertEqual(decode_wechat_dat(encrypted), PNG)
        self.assertEqual(decode_wechat_dat(encrypted, xor_key=key), PNG)
        self.assertIsNone(decode_wechat_dat(b"\x00\x01\x02"))

    def test_wxgf_is_returned_for_caller_owned_transcoding(self):
        payload = WXGF_MAGIC + b"opaque-frame-data"
        self.assertEqual(decode_wechat_dat(payload), payload)
        self.assertEqual(
            decode_wechat_dat(bytes(item ^ 0x22 for item in payload)), payload
        )
