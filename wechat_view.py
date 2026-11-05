"""Turn one image or emoji into something an AI can read in a single look.

A still picture needs no frames. A real animation becomes one sheet of its distinct
frames in time order, so the reader never has to cut frames by hand.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

READABLE_STILL_FORMATS = {"PNG", "JPEG", "WEBP"}
MAX_SHEET_FRAMES = 25
SHEET_COLUMNS = 5
CELL_PIXELS = 240
LABEL_PIXELS = 16
GAP_PIXELS = 4


def _flatten(frame: Image.Image) -> Image.Image:
    rgba = frame.convert("RGBA")
    flat = Image.new("RGB", rgba.size, "white")
    flat.paste(rgba, mask=rgba.getchannel("A"))
    return flat


def _png(picture: Image.Image) -> bytes:
    output = io.BytesIO()
    picture.save(output, "PNG")
    return output.getvalue()


def _distinct_frames(picture: Image.Image) -> list[tuple[int, float, Image.Image]]:
    frames, previous, elapsed = [], None, 0.0
    for index in range(picture.n_frames):
        picture.seek(index)
        flat = _flatten(picture)
        pixels = flat.tobytes()
        if pixels != previous:
            frames.append((index + 1, elapsed, flat))
            previous = pixels
        elapsed += picture.info.get("duration", 0) / 1000
    return frames


def _sample(frames: list) -> list:
    if len(frames) <= MAX_SHEET_FRAMES:
        return frames
    last = len(frames) - 1
    return [frames[round(i * last / (MAX_SHEET_FRAMES - 1))] for i in range(MAX_SHEET_FRAMES)]


def _sheet(frames: list) -> Image.Image:
    rows = (len(frames) + SHEET_COLUMNS - 1) // SHEET_COLUMNS
    columns = min(len(frames), SHEET_COLUMNS)
    cell_height = CELL_PIXELS + LABEL_PIXELS + GAP_PIXELS
    sheet = Image.new("RGB", (columns * (CELL_PIXELS + GAP_PIXELS), rows * cell_height), "white")
    draw = ImageDraw.Draw(sheet)
    for position, (number, elapsed, frame) in enumerate(frames):
        left = position % SHEET_COLUMNS * (CELL_PIXELS + GAP_PIXELS)
        top = position // SHEET_COLUMNS * cell_height
        small = frame.copy()
        small.thumbnail((CELL_PIXELS, CELL_PIXELS))
        sheet.paste(small, (left, top))
        draw.text((left + 2, top + CELL_PIXELS + 2), f"#{number}  {elapsed:.1f}s", fill="black")
    return sheet


def build_view(payload: bytes) -> tuple[dict, bytes | None, str | None]:
    """Return facts, the bytes to write as a view, and that view's kind.

    No bytes means the original file is itself the view.
    """

    with Image.open(io.BytesIO(payload)) as picture:
        total = getattr(picture, "n_frames", 1)
        facts = {"width": picture.width, "height": picture.height, "frameCount": total}
        if total == 1:
            facts.update(animated=False, distinctFrames=1, framesShown=1, complete=True)
            if picture.format in READABLE_STILL_FORMATS:
                return facts, None, None
            return facts, _png(_flatten(picture)), "still"
        frames = _distinct_frames(picture)
    if len(frames) == 1:
        facts.update(animated=False, distinctFrames=1, framesShown=1, complete=True)
        return facts, _png(frames[0][2]), "still"
    shown = _sample(frames)
    facts.update(animated=True, distinctFrames=len(frames), framesShown=len(shown), complete=len(shown) == len(frames))
    return facts, _png(_sheet(shown)), "frames"


__all__ = ["build_view"]
