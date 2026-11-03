"""Resampling is the most expensive step of a diff and of a report; do it once.

No network and no revyl CLI: frames are synthetic files in a temp dir.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from atlas_review import Build, Screen, ScreenVersion, diff_versions  # noqa: E402
from atlas_review.imaging import load_rgb  # noqa: E402
from atlas_review.report import _frame_payload  # noqa: E402


def _frame(width, mark=False):
    img = Image.new("RGB", (width, width * 2), (250, 250, 252))
    draw = ImageDraw.Draw(img)
    scale = width / 200.0
    draw.rectangle([10 * scale, 60 * scale, 120 * scale, 90 * scale], fill=(30, 30, 40))
    if mark:
        draw.rectangle([20 * scale, 200 * scale, 150 * scale, 260 * scale], fill=(200, 30, 30))
    return img


class CountingResize:
    """Wraps Image.resize so a test can count how often frames get resampled."""

    def __init__(self):
        self.calls = 0
        self._real = Image.Image.resize

    def __enter__(self):
        counter = self

        def resize(img, *args, **kwargs):
            counter.calls += 1
            return counter._real(img, *args, **kwargs)

        self._patch = mock.patch.object(Image.Image, "resize", resize)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


class FramePayloadMemo(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        # The same frame under two builds' cache directories, as Atlas leaves it
        # for a screen that did not change.
        self.paths = [root / "build-1" / "s.png", root / "build-2" / "s.png"]
        for path in self.paths:
            path.parent.mkdir()
            _frame(600).save(path)
        self.other = root / "other.png"
        _frame(600, mark=True).save(self.other)

    def tearDown(self):
        self.tmp.cleanup()

    def test_identical_sources_are_resampled_once(self):
        seen = {}
        with CountingResize() as counter:
            first = _frame_payload(self.paths[0], 200, seen)
            second = _frame_payload(self.paths[1], 200, seen)
        self.assertEqual(counter.calls, 1)
        self.assertEqual(first, second)

    def test_memo_does_not_change_the_output(self):
        memo = _frame_payload(self.paths[1], 200, {})
        plain = _frame_payload(self.paths[1], 200)
        self.assertEqual(memo, plain)
        self.assertEqual((plain["w"], plain["h"]), (200, 400))

    def test_different_bytes_or_widths_are_not_conflated(self):
        seen = {}
        a = _frame_payload(self.paths[0], 200, seen)
        b = _frame_payload(self.other, 200, seen)
        c = _frame_payload(self.paths[0], 300, seen)
        self.assertNotEqual(a["digest"], b["digest"])
        self.assertEqual(c["w"], 300)

    def test_callers_cannot_corrupt_the_memo(self):
        seen = {}
        first = _frame_payload(self.paths[0], 200, seen)
        first.pop("src")
        self.assertIn("src", _frame_payload(self.paths[1], 200, seen))


class DiffResamplesOnce(unittest.TestCase):
    def test_mixed_scale_pair_resamples_only_the_big_frame_once(self):
        with TemporaryDirectory() as tmp:
            small, big = Path(tmp) / "a.png", Path(tmp) / "b.png"
            _frame(200).save(small)
            _frame(600, mark=True).save(big)
            screen = Screen(id="s", name="home")
            before = ScreenVersion(screen, Build(id="b1", version="1", ordinal=1), image_path=small)
            after = ScreenVersion(screen, Build(id="b2", version="2", ordinal=2), image_path=big)
            with CountingResize() as counter:
                result = diff_versions(before, after, target_width=200)
            self.assertEqual(counter.calls, 1)
            self.assertTrue(result.regions)
            self.assertEqual(result.render("highlight").width, 200)


class LoadRgb(unittest.TestCase):
    def test_rgb_and_alpha_frames(self):
        with TemporaryDirectory() as tmp:
            rgb, rgba = Path(tmp) / "rgb.png", Path(tmp) / "rgba.png"
            _frame(50).save(rgb)
            Image.new("RGBA", (4, 4), (0, 0, 0, 0)).save(rgba)
            self.assertEqual(load_rgb(rgb).mode, "RGB")
            self.assertEqual(load_rgb(rgb).getpixel((0, 0)), (250, 250, 252))
            # Fully transparent pixels flatten onto white, as the browser shows them.
            self.assertEqual(load_rgb(rgba).getpixel((0, 0)), (255, 255, 255))


if __name__ == "__main__":
    unittest.main(verbosity=2)
