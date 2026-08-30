"""Decode one Tencent/WeChat SILK payload to a mono WAV file."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile
import wave

import pilk


def decode(source: Path, output: Path, sample_rate: int = 24_000) -> None:
    if output.exists():
        raise FileExistsError(output)
    data = source.read_bytes()
    if not data.startswith(b"\x02#!SILK_V3"):
        raise ValueError("not_tencent_silk")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wechat-silk-") as scratch:
        pcm = Path(scratch) / "voice.pcm"
        wav = Path(scratch) / "voice.wav"
        pilk.decode(os.fspath(source), os.fspath(pcm), pcm_rate=sample_rate)
        with pcm.open("rb") as source_pcm, wave.open(os.fspath(wav), "wb") as target:
            target.setparams((1, 2, sample_rate, 0, "NONE", "NONE"))
            target.writeframes(source_pcm.read())
        temporary = output.with_name(output.name + ".incomplete")
        temporary.write_bytes(wav.read_bytes())
        temporary.replace(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sample-rate", type=int, default=24_000)
    args = parser.parse_args()
    decode(Path(args.input), Path(args.output), args.sample_rate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
