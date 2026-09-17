"""Exercise installed entrypoints with generated data, never real account content."""

from __future__ import annotations

import argparse
import io
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import wave


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        check=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        **kwargs,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--voice-python", default=os.environ.get("WECHAT_DIRECT_VOICE_PYTHON")
    )
    parser.add_argument("--require-voice", action="store_true")
    parser.add_argument("--require-wxgf", action="store_true")
    args = parser.parse_args()
    import wechat_cli as cli
    from wechat_wxgf import wxgf_to_image
    from PIL import Image

    checks = {}
    result = run(
        [sys.executable, "-B", "-m", "wechat_cli", "doctor", "--environment-only"]
    )
    doctor = json.loads(result.stdout)
    assert doctor["status"] == "success", doctor
    assert doctor["configuration"]["status"] == "not_checked"
    checks["installed_cli_environment"] = "pass"
    with tempfile.TemporaryDirectory(prefix="wechat-runtime-smoke-") as td:
        root = Path(td)
        if args.voice_python:
            pcm, silk, wav = root / "tone.pcm", root / "tone.silk", root / "tone.wav"
            pcm.write_bytes(
                b"".join(
                    struct.pack(
                        "<h", int(4000 * math.sin(2 * math.pi * 440 * n / 24000))
                    )
                    for n in range(4800)
                )
            )
            encode = "import pilk,sys;pilk.encode(sys.argv[1],sys.argv[2],pcm_rate=24000,tencent=True)"
            run([args.voice_python, "-B", "-c", encode, str(pcm), str(silk)])
            original = silk.read_bytes()
            assert original.startswith(b"\x02#!SILK_V3")
            previous = os.environ.get("WECHAT_DIRECT_VOICE_PYTHON")
            os.environ["WECHAT_DIRECT_VOICE_PYTHON"] = args.voice_python
            try:
                cli._decode_voice_file(silk, wav)
            finally:
                if previous is None:
                    os.environ.pop("WECHAT_DIRECT_VOICE_PYTHON", None)
                else:
                    os.environ["WECHAT_DIRECT_VOICE_PYTHON"] = previous
            with wave.open(str(wav), "rb") as decoded:
                assert decoded.getframerate() == 24000
                assert decoded.getnchannels() == 1
                assert decoded.getnframes() >= 2400
                assert any(decoded.readframes(decoded.getnframes()))
            assert silk.read_bytes() == original
            checks["python311_silk_to_wav_real_roundtrip"] = "pass"
        else:
            if args.require_voice:
                raise RuntimeError("voice_python_required")
            checks["python311_silk_to_wav_real_roundtrip"] = "not_run"
        if shutil.which("ffmpeg") and shutil.which("ffprobe"):
            hevc = run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=64x64:rate=1",
                    "-frames:v",
                    "1",
                    "-c:v",
                    "libx265",
                    "-threads",
                    "1",
                    "-x265-params",
                    "pools=none:frame-threads=1:log-level=error",
                    "-f",
                    "hevc",
                    "pipe:1",
                ]
            ).stdout
            payload = b"wxgf\x05" + len(hevc).to_bytes(4, "big") + hevc
            picture = wxgf_to_image(payload)
            assert picture and picture.startswith(b"\x89PNG")
            with Image.open(io.BytesIO(picture)) as image:
                assert image.size == (64, 64)
            checks["wxgf_ffprobe_ffmpeg_real_roundtrip"] = "pass"
        else:
            if args.require_wxgf:
                raise RuntimeError("ffmpeg_ffprobe_required")
            checks["wxgf_ffprobe_ffmpeg_real_roundtrip"] = "not_run"
    print(
        json.dumps(
            {
                "status": "success",
                "scope": "synthetic_runtime",
                "sourceAccountRead": "not_performed",
                "checks": checks,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
