from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from wechat_wxgf import wxgf_to_image


WXGF = b"wxgf"
JPEG = b"\xff\xd8\xffsynthetic-jpeg\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\nsynthetic-png"
PNG_WITH_END = PNG + b"\x00\x00\x00\x00IEND\xaeB`\x82"
HEVC = b"\x00\x00\x00\x01\x40\x01synthetic-hevc"


def _hevc_package(stream: bytes = HEVC) -> bytes:
    # Byte 4 is the WXGF header length; the test header is five bytes long.
    return WXGF + b"\x05" + b"\x00\x00\x00" + len(stream).to_bytes(4, "big") + stream


class WxgfToImageTests(unittest.TestCase):
    def test_rejects_non_wxgf_and_unrecognised_payloads(self):
        self.assertIsNone(wxgf_to_image(b"garbage"))
        self.assertIsNone(wxgf_to_image(WXGF + b"no-image-or-hevc"))
        self.assertIsNone(wxgf_to_image(WXGF + b"\x00"))

    def test_extracts_embedded_jpeg_and_png_exactly(self):
        jpeg_payload = WXGF + b"header" + JPEG + b"trailing-package-data"
        self.assertEqual(wxgf_to_image(jpeg_payload), JPEG)

        png_payload = WXGF + b"header" + PNG_WITH_END + b"trailing-package-data"
        self.assertEqual(wxgf_to_image(png_payload), PNG_WITH_END)

    def test_single_frame_hevc_uses_ffprobe_then_png_conversion(self):
        package = _hevc_package()
        calls: list[tuple[list[str], float]] = []
        inputs: list[bytes] = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs["timeout"]))
            inputs.append(kwargs["input"])
            if command[0] == "ffprobe":
                return subprocess.CompletedProcess(command, 0, stdout=b"1\n", stderr=b"")
            return subprocess.CompletedProcess(
                command, 0, stdout=b"\x89PNG\r\n\x1a\nconverted", stderr=b""
            )

        with patch("wechat_wxgf.subprocess.run", side_effect=fake_run):
            converted = wxgf_to_image(package)

        self.assertTrue(converted.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual([call[0][0] for call in calls], ["ffprobe", "ffmpeg"])
        self.assertEqual(inputs, [HEVC, HEVC])
        self.assertTrue(all(timeout > 0 for _, timeout in calls))

    def test_multi_frame_hevc_uses_gif_conversion(self):
        package = _hevc_package()

        def fake_run(command, **kwargs):
            if command[0] == "ffprobe":
                return subprocess.CompletedProcess(command, 0, stdout=b"2\n", stderr=b"")
            self.assertIn("-f", command)
            self.assertIn("gif", command)
            return subprocess.CompletedProcess(command, 0, stdout=b"GIF89aconverted", stderr=b"")

        with patch("wechat_wxgf.subprocess.run", side_effect=fake_run):
            converted = wxgf_to_image(package)

        self.assertTrue(converted.startswith(b"GIF89a"))

    def test_ffprobe_failure_and_timeout_are_unresolved(self):
        package = _hevc_package()
        with patch("wechat_wxgf.subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(wxgf_to_image(package))
        with patch(
            "wechat_wxgf.subprocess.run",
            side_effect=subprocess.TimeoutExpired("ffprobe", 10),
        ):
            self.assertIsNone(wxgf_to_image(package))

    def test_ffmpeg_failure_after_frame_probe_is_unresolved(self):
        package = _hevc_package()

        def fake_run(command, **_kwargs):
            if command[0] == "ffprobe":
                return subprocess.CompletedProcess(command, 0, stdout=b"1\n", stderr=b"")
            raise subprocess.TimeoutExpired(command, 10)

        with patch("wechat_wxgf.subprocess.run", side_effect=fake_run):
            self.assertIsNone(wxgf_to_image(package))

    def test_multiple_partitions_are_not_concatenated(self):
        first = _hevc_package()
        second = _hevc_package(HEVC + b"second")
        package = first + second[4:]
        with patch("wechat_wxgf.subprocess.run") as run:
            self.assertIsNone(wxgf_to_image(package))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
