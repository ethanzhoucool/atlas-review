"""Watch-region measurements must be relative to the part of the box on the frame."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from atlas_review.diff import DiffOptions, DiffResult, compare_images  # noqa: E402

W, H = 400, 800


def _result(*boxes):
    """A diff in which each pixel box (x0, y0, x1, y1) went from white to black."""
    before = Image.new("RGB", (W, H), "white")
    after = before.copy()
    for box in boxes:
        ImageDraw.Draw(after).rectangle(box, fill="black")
    mask, regions, changed, comparable, _ = compare_images(before, after, DiffOptions(ignore=[]))
    return DiffResult(before=None, after=None, score=changed / comparable, changed_pixels=changed,
                      comparable_pixels=comparable, regions=regions, _mask=mask)


class WatchBoxArea(unittest.TestCase):
    def setUp(self):
        # The bottom-right 80x80 corner changed completely.
        self.result = _result([320, 720, W - 1, H - 1])

    def test_box_inside_the_frame(self):
        self.assertAlmostEqual(self.result.changed_fraction_in((0.8, 0.9, 0.2, 0.1)), 1.0)

    def test_box_overhanging_the_frame_edge_measures_its_visible_part(self):
        # Before: 6400 changed px over a nominal 160x160 box read as 25%.
        for box in ((0.8, 0.9, 0.4, 0.2), (0.8, 0.9, 0.25, 0.1), (0.8, 0.9, 5.0, 5.0)):
            with self.subTest(box=box):
                self.assertAlmostEqual(self.result.changed_fraction_in(box), 1.0)

    def test_fraction_uses_the_same_pixels_as_the_count(self):
        # Columns int(0.01 * 400) = 4 to int(0.07 * 400) = int(27.999...) = 27
        # are 23 px wide; int(0.06 * 400) says 24. Count and area must agree.
        result = _result([0, 0, 40, H - 1])
        box = (0.01, 0.0, 0.06, 1.0)
        self.assertEqual(result.changed_in(box), 23 * H)
        self.assertAlmostEqual(result.changed_fraction_in(box), 1.0)

    def test_box_entirely_off_the_frame(self):
        self.assertEqual(self.result.changed_in((1.2, 0.5, 0.1, 0.1)), 0)
        self.assertEqual(self.result.changed_fraction_in((1.2, 0.5, 0.1, 0.1)), 0.0)

    def test_partially_changed_box(self):
        fraction = self.result.changed_fraction_in((0.6, 0.9, 0.4, 0.1))
        self.assertAlmostEqual(fraction, 0.5)

    def test_no_mask_means_no_change(self):
        empty = DiffResult(before=None, after=None, score=0.0, changed_pixels=0,
                           comparable_pixels=0, regions=[])
        self.assertEqual(empty.changed_in((0, 0, 1, 1)), 0)
        self.assertEqual(empty.changed_fraction_in((0, 0, 1, 1)), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
