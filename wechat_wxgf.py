"""Small, conservative converter for decrypted WeChat WXGF payloads."""

from __future__ import annotations

import subprocess
from typing import Final


WXGF_MAGIC: Final = b"wxgf"
JPEG_MAGIC: Final = b"\xff\xd8\xff"
PNG_MAGIC: Final = b"\x89PNG"
HEVC_START_4: Final = b"\x00\x00\x00\x01"
HEVC_START_3: Final = b"\x00\x00\x01"
EMBEDDED_SCAN_LIMIT: Final = 4096
COMMAND_TIMEOUT_SECONDS: Final = 10.0
CREATE_NO_WINDOW: Final = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _embedded_image(payload: bytes) -> bytes | None:
    end = min(len(payload), EMBEDDED_SCAN_LIMIT)
    candidates = [
        (payload.find(JPEG_MAGIC, 4, end), JPEG_MAGIC),
        (payload.find(PNG_MAGIC, 4, end), PNG_MAGIC),
    ]
    candidates = [(offset, magic) for offset, magic in candidates if offset >= 0]
    if not candidates:
        return None
    offset, magic = min(candidates, key=lambda item: item[0])
    if magic == JPEG_MAGIC:
        eoi = payload.find(b"\xff\xd9", offset + len(magic))
        return payload[offset : eoi + 2] if eoi >= 0 else payload[offset:]
    iend = payload.find(b"\x00\x00\x00\x00IEND\xaeB`\x82", offset + len(magic))
    return payload[offset : iend + 12] if iend >= 0 else payload[offset:]


def _hevc_partitions(payload: bytes) -> list[bytes]:
    """Extract length-bounded HEVC partitions without concatenating them."""

    if len(payload) < 5:
        return []
    header_length = payload[4]
    if header_length >= len(payload):
        return []
    search_from = max(5, header_length)
    partitions: list[bytes] = []
    occupied_until = -1
    position = search_from
    while position < len(payload):
        if payload.startswith(HEVC_START_4, position):
            start = position
        elif payload.startswith(HEVC_START_3, position):
            start = position
        else:
            position += 1
            continue
        prefix_start = start - 4
        if prefix_start < search_from:
            position = start + 1
            continue
        length = int.from_bytes(payload[prefix_start:start], "big")
        end = start + length
        if length > 0 and end <= len(payload) and start >= occupied_until:
            partitions.append(payload[start:end])
            occupied_until = end
            position = end
        else:
            position = start + 1
    return partitions


def _run(command: list[str], payload: bytes) -> bytes | None:
    try:
        completed = subprocess.run(
            command,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    return bytes(completed.stdout)


def _frame_count(hevc: bytes) -> int | None:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=nw=1:nk=1",
            "-f",
            "hevc",
            "-i",
            "pipe:0",
        ],
        hevc,
    )
    if result is None:
        return None
    try:
        lines = result.decode("ascii").strip().splitlines()
        if len(lines) != 1:
            return None
        count = int(lines[0])
    except (UnicodeDecodeError, ValueError):
        return None
    return count if count > 0 else None


def _to_png(hevc: bytes) -> bytes | None:
    result = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "hevc",
            "-i",
            "pipe:0",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ],
        hevc,
    )
    return result if result and result.startswith(b"\x89PNG\r\n\x1a\n") else None


def _to_gif(hevc: bytes) -> bytes | None:
    result = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "hevc",
            "-i",
            "pipe:0",
            "-filter_complex",
            "[0:v]split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
            "-loop",
            "0",
            "-f",
            "gif",
            "pipe:1",
        ],
        hevc,
    )
    return result if result and result.startswith((b"GIF87a", b"GIF89a")) else None


def wxgf_to_image(payload: bytes) -> bytes | None:
    """Convert one decrypted WXGF payload to PNG/GIF or return ``None``.

    Standard images embedded in the first 4 KiB are returned directly.  HEVC
    is converted only when one length-bounded partition is discoverable and
    ``ffprobe`` proves its frame count.  Multiple partitions are left
    unresolved because they may represent animation frames or an alpha stream.
    """

    if not isinstance(payload, bytes) or not payload.startswith(WXGF_MAGIC):
        return None
    embedded = _embedded_image(payload)
    if embedded is not None:
        return embedded
    partitions = _hevc_partitions(payload)
    if len(partitions) != 1:
        return None
    hevc = partitions[0]
    frame_count = _frame_count(hevc)
    if frame_count is None:
        return None
    return _to_png(hevc) if frame_count == 1 else _to_gif(hevc)


__all__ = ["wxgf_to_image"]
