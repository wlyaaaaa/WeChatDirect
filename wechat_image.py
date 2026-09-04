"""Small, format-validated decoder for local WeChat ``.dat`` image blobs.

The module has no source-database access and never discovers keys.  Callers
must supply a current V2 AES key and, where necessary, a proven XOR byte.
"""

from __future__ import annotations

import struct
from typing import Final

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


DAT_V1_MAGIC: Final = b"\x07\x08V1\x08\x07"
DAT_V2_MAGIC: Final = b"\x07\x08V2\x08\x07"
DAT_HEADER_SIZE: Final = 15
V1_PUBLIC_AES_KEY: Final = b"cfcd208495d565ef"
WXGF_MAGIC: Final = b"wxgf"
PNG_TRAILER: Final = b"\x00\x00\x00\x00IEND\xaeB`\x82"


def decode_wechat_dat(
    payload: bytes,
    *,
    aes_key: bytes | None = None,
    xor_key: int | None = None,
) -> bytes | None:
    """Decode one local WeChat image payload when its format is provable.

    V1 uses its documented public, format-specific AES-128 key if no key is
    supplied.  V2 deliberately requires the caller to supply its exact
    16-byte AES key.  For an old headerless XOR payload, a caller can supply
    the XOR byte or the decoder can derive it only from a complete leading
    visual signature.  An undecodable, malformed, or non-visual payload
    returns ``None``.

    A decoded ``wxgf`` blob is intentionally returned as ``wxgf`` bytes.  It
    still needs a caller-owned transcode step before a browser or vision model
    can treat it as an image.
    """

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        return None
    source = bytes(payload)
    if _is_visual_or_wxgf(source):
        return source

    if source.startswith((DAT_V1_MAGIC, DAT_V2_MAGIC)):
        return _decode_modern_dat(source, aes_key=aes_key, xor_key=xor_key)
    return _decode_legacy_xor(source, xor_key=xor_key)


def _decode_modern_dat(
    source: bytes, *, aes_key: bytes | None, xor_key: int | None
) -> bytes | None:
    if len(source) < DAT_HEADER_SIZE:
        return None
    magic = source[:6]
    aes_size = struct.unpack_from("<I", source, 6)[0]
    xor_size = struct.unpack_from("<I", source, 10)[0]
    encrypted_size = (aes_size // 16) * 16 + 16
    body = source[DAT_HEADER_SIZE:]
    if encrypted_size > len(body) or xor_size > len(body) - encrypted_size:
        return None

    key = _effective_aes_key(magic, aes_key)
    if key is None:
        return None
    aes_plaintext = _decrypt_aes_ecb_pkcs7(body[:encrypted_size], key, aes_size)
    if aes_plaintext is None:
        return None

    middle_end = len(body) - xor_size
    middle = body[encrypted_size:middle_end]
    encrypted_tail = body[middle_end:]
    if xor_size:
        key_byte = _coerce_xor_key(xor_key)
        if key_byte is None:
            key_byte = _infer_xor_key(encrypted_tail)
        if key_byte is None:
            return None
        tail = _xor(encrypted_tail, key_byte)
    else:
        if xor_key is not None and _coerce_xor_key(xor_key) is None:
            return None
        tail = b""

    result = aes_plaintext + middle + tail
    return result if _is_visual_or_wxgf(result) else None


def _effective_aes_key(magic: bytes, supplied: bytes | None) -> bytes | None:
    if supplied is None:
        return V1_PUBLIC_AES_KEY if magic == DAT_V1_MAGIC else None
    if not isinstance(supplied, (bytes, bytearray, memoryview)):
        return None
    key = bytes(supplied)
    return key if len(key) == 16 else None


def _decrypt_aes_ecb_pkcs7(
    encrypted: bytes, key: bytes, expected_plaintext_size: int
) -> bytes | None:
    if not encrypted or len(encrypted) % 16:
        return None
    try:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
    except Exception:
        return None
    padding_size = padded[-1] if padded else 0
    if not 1 <= padding_size <= 16:
        return None
    if padded[-padding_size:] != bytes([padding_size]) * padding_size:
        return None
    plaintext = padded[:-padding_size]
    return plaintext if len(plaintext) == expected_plaintext_size else None


def _infer_xor_key(encrypted_tail: bytes) -> int | None:
    """Infer only from complete, independently checkable file trailers."""

    candidates: set[int] = set()
    if len(encrypted_tail) >= 2:
        jpeg_key = encrypted_tail[-2] ^ 0xFF
        if encrypted_tail[-1] ^ jpeg_key == 0xD9:
            candidates.add(jpeg_key)
    if len(encrypted_tail) >= len(PNG_TRAILER):
        png_ciphertext = encrypted_tail[-len(PNG_TRAILER):]
        png_key = png_ciphertext[0] ^ PNG_TRAILER[0]
        if all(
            value ^ png_key == expected
            for value, expected in zip(png_ciphertext, PNG_TRAILER, strict=True)
        ):
            candidates.add(png_key)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _decode_legacy_xor(source: bytes, *, xor_key: int | None) -> bytes | None:
    explicit_key = _coerce_xor_key(xor_key)
    if xor_key is not None and explicit_key is None:
        return None
    if explicit_key is not None:
        decoded = _xor(source, explicit_key)
        return decoded if _is_visual_or_wxgf(decoded) else None

    candidates: list[bytes] = []
    seen_keys: set[int] = set()
    for positions, expected in _LEGACY_SIGNATURES:
        if not positions or max(positions) >= len(source):
            continue
        key = source[positions[0]] ^ expected[0]
        if key in seen_keys:
            continue
        if any(source[position] ^ key != value for position, value in zip(positions, expected)):
            continue
        seen_keys.add(key)
        decoded = _xor(source, key)
        if _is_visual_or_wxgf(decoded):
            candidates.append(decoded)
    return candidates[0] if len(candidates) == 1 else None


_LEGACY_SIGNATURES: Final = (
    (tuple(range(3)), b"\xff\xd8\xff"),
    (tuple(range(8)), b"\x89PNG\r\n\x1a\n"),
    (tuple(range(4)), b"GIF8"),
    (tuple(range(4)), b"II*\x00"),
    (tuple(range(4)), b"MM\x00*"),
    ((0, 1, 2, 3, 8, 9, 10, 11), b"RIFFWEBP"),
    (tuple(range(4)), WXGF_MAGIC),
)


def _coerce_xor_key(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 0xFF else None


def _xor(payload: bytes, key: int) -> bytes:
    return bytes(value ^ key for value in payload)


def _is_visual_or_wxgf(payload: bytes) -> bool:
    return _is_visual(payload) or payload.startswith(WXGF_MAGIC)


def _is_visual(payload: bytes) -> bool:
    return (
        payload.startswith(b"\xff\xd8\xff")
        or payload.startswith(b"\x89PNG\r\n\x1a\n")
        or payload.startswith((b"GIF87a", b"GIF89a"))
        or payload.startswith(b"BM")
        or payload.startswith((b"II*\x00", b"MM\x00*"))
        or (payload.startswith(b"RIFF") and payload[8:12] == b"WEBP")
    )
