"""Synthetic pictures only: a still is never cut into frames, an animation becomes one sheet."""


import isolation  # noqa: F401

import io
from pathlib import Path
import unittest
from unittest import mock

from PIL import Image

import wechat_cli as cli
import wechat_view as view
import wechat_wxgf as wxgf


def encoded(fmt, *colors, **options):
    frames = [Image.new("RGB", (32, 32), color) for color in colors]
    output = io.BytesIO()
    frames[0].save(output, fmt, save_all=len(frames) > 1, append_images=frames[1:], duration=100, **options)
    return output.getvalue()


def animation(count):
    return encoded("GIF", *[(index * 2, 255 - index * 2, index) for index in range(count)])


class ViewTests(unittest.TestCase):
    def test_readable_still_is_its_own_view(self):
        for fmt in ("PNG", "JPEG"):
            facts, data, kind = view.build_view(encoded(fmt, "red"))
            self.assertEqual((data, kind, facts["animated"], facts["frameCount"]), (None, None, False, 1))

    def test_single_frame_gif_becomes_one_png_without_frames(self):
        facts, data, kind = view.build_view(encoded("GIF", "red"))
        self.assertEqual((kind, facts["animated"], facts["framesShown"]), ("still", False, 1))
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_animation_with_identical_frames_is_a_still(self):
        picture = Image.new("P", (16, 16))
        with mock.patch.object(view, "_distinct_frames", return_value=[(1, 0.0, picture.convert("RGB"))]):
            facts, _data, kind = view.build_view(animation(3))
        self.assertEqual((kind, facts["animated"], facts["frameCount"]), ("still", False, 3))

    def test_short_animation_shows_every_distinct_frame_on_one_sheet(self):
        facts, data, kind = view.build_view(animation(7))
        self.assertEqual((kind, facts["animated"], facts["distinctFrames"], facts["framesShown"], facts["complete"]),
                         ("frames", True, 7, 7, True))
        with Image.open(io.BytesIO(data)) as sheet:
            self.assertEqual(sheet.width, view.SHEET_COLUMNS * (view.CELL_PIXELS + view.GAP_PIXELS))

    def test_long_animation_keeps_first_and_last_and_says_it_is_not_complete(self):
        frames = [(number, 0.0, None) for number in range(1, 61)]
        chosen = view._sample(frames)
        self.assertEqual((len(chosen), chosen[0][0], chosen[-1][0]), (view.MAX_SHEET_FRAMES, 1, 60))
        facts, _data, _kind = view.build_view(animation(40))
        self.assertEqual((facts["distinctFrames"], facts["framesShown"], facts["complete"]), (40, view.MAX_SHEET_FRAMES, False))

    def test_transparent_frames_sit_on_white(self):
        clear = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
        self.assertEqual(view._flatten(clear).getpixel((0, 0)), (255, 255, 255))


class OutputNameTests(unittest.TestCase):
    def test_suffix_follows_the_bytes(self):
        jpeg, gif = encoded("JPEG", "red"), encoded("GIF", "red")
        for given, data, expected in (("a/emo", gif, "a/emo.gif"), ("a/emo.jpg", gif, "a/emo.gif"),
                                      ("a/emo.gif", gif, "a/emo.gif"), ("a/emo.jpeg", jpeg, "a/emo.jpeg"),
                                      ("a/emo.jpg", jpeg, "a/emo.jpg"), ("a/emo.dat", b"unknown", "a/emo.dat")):
            self.assertEqual(cli._output_matching_content(Path(given), data, "emoji"), Path(given).with_name(Path(expected).name))

    def test_unreadable_picture_keeps_the_original_and_reports_the_gap(self):
        with mock.patch("wechat_view.build_view", side_effect=ValueError):
            self.assertEqual(cli._media_view(Path("unused.png"), b"x"), {"view": None, "viewError": "ValueError"})


class WxgfRouteTests(unittest.TestCase):
    PAYLOAD = b"wxgf" + b"\x00" * 8 + (16).to_bytes(4, "big") + b"\x00\x00\x00\x01" + b"\x40" * 12

    def route(self, decoded):
        with (mock.patch.object(wxgf, "_hevc_partitions", return_value=[b"hevc"]),
              mock.patch.object(wxgf, "_decode_in_process", return_value=decoded),
              mock.patch.object(wxgf, "_frame_count", return_value=1) as probe,
              mock.patch.object(wxgf, "_to_png", return_value=b"external-png"),
              mock.patch.object(wxgf, "_to_gif", return_value=b"external-gif")):
            return wxgf.wxgf_to_image(self.PAYLOAD), probe.call_count

    def test_still_never_starts_an_external_program(self):
        self.assertEqual(self.route((1, b"inside-png")), (b"inside-png", 0))

    def test_animation_keeps_its_motion(self):
        self.assertEqual(self.route((5, b"inside-png")), (b"external-gif", 0))

    def test_missing_decoder_falls_back_to_ffmpeg(self):
        self.assertEqual(self.route(None), (b"external-png", 1))

    def test_decoder_failure_is_not_fatal(self):
        with mock.patch.dict("sys.modules", {"av": None}):
            self.assertIsNone(wxgf._decode_in_process(b"hevc"))


if __name__ == "__main__":
    unittest.main()
