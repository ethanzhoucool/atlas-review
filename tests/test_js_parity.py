"""Run the report's JavaScript detector against the Python one on the same pixels.

The report re-implements the detector in report.js, and a CI gate reading
1.00% while the page shows 0.98% (or "moved" in one and "changed" in the
other) destroys trust in both. String-matching shared constants can't catch an
algorithmic drift, so this executes `computeDiff` under node and compares its
pixel counts and regions with `compare_images`. Skipped when node is missing.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from atlas_review import DiffOptions, compare_images  # noqa: E402
from atlas_review.diff import _dilate  # noqa: E402
from atlas_review.imaging import fit_to  # noqa: E402
from atlas_review.report import detector_defaults  # noqa: E402

JS_PATH = Path(__file__).resolve().parents[1] / "src" / "atlas_review" / "assets" / "report.js"

HARNESS = r"""
const fs = require("fs");
const [jsPath, specPath] = process.argv.slice(1);
const spec = JSON.parse(fs.readFileSync(specPath, "utf8"));
let src = fs.readFileSync(jsPath, "utf8");
const end = src.lastIndexOf("})();");
src = src.slice(0, end) + "globalThis.__engine = { computeDiff: computeDiff };\n" + src.slice(end);
globalThis.window = { __ATLAS_DATA__: { app: "parity", screens: [], builds: [], images: {},
                                        alerts: [], defaults: {} } };
globalThis.document = { readyState: "loading", addEventListener: function () {} };
globalThis.localStorage = { getItem: function () { return null; }, setItem: function () {} };
(0, eval)(src);
const out = spec.pairs.map(function (p) {
  const A = { data: new Uint8ClampedArray(fs.readFileSync(p.a)) };
  const B = { data: new Uint8ClampedArray(fs.readFileSync(p.b)) };
  const d = globalThis.__engine.computeDiff(A, B, p.w, p.h, p.opts);
  return { changed: d.changed, comparable: d.comparable,
           regions: d.regions.map(function (r) { return [r.x, r.y, r.w, r.h, r.kind, r.shiftDy]; }) };
});
process.stdout.write(JSON.stringify(out));
"""


def _frame(seed: int, w: int = 200, h: int = 400, dy: int = 0, recolor=(), clock="9:41"):
    """A busy synthetic phone frame: clock, header, rows of glyphs and swatches."""
    rng = random.Random(seed)
    img = Image.new("RGB", (w, h), (250, 250, 252))
    draw = ImageDraw.Draw(img)
    draw.text((8, 6), clock, fill=(0, 0, 0))
    draw.rectangle([0, 34, w, 60], fill=(40, 80, 190))
    for i in range(12):
        y = 72 + i * 26 + (dy if i >= 4 else 0)
        color = (200, 30, 30) if i in recolor else (rng.randint(0, 200),) * 3
        draw.rectangle([8, y, 26, y + 18], fill=color)
        draw.text((34, y + 2), "row %d %s" % (i, "x" * rng.randint(2, 14)), fill=(20, 20, 30))
        draw.text((w - 44, y + 2), "$%d" % rng.randint(10, 999), fill=(0, 110, 60))
    return img


def _marks(w, h, boxes, bg=(255, 255, 255)):
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    for x, y, bw, bh in boxes:
        draw.rectangle([x, y, x + bw - 1, y + bh - 1], fill=(0, 0, 0))
    return img


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class JsDetectorParity(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_js(self, cases):
        pairs = []
        for i, (before, after, options) in enumerate(cases):
            size = (max(before.width, after.width), max(before.height, after.height))
            paths = []
            for tag, img in (("a", before), ("b", after)):
                path = self.root / ("%d-%s.rgba" % (i, tag))
                path.write_bytes(np.asarray(fit_to(img, size).convert("RGBA")).tobytes())
                paths.append(str(path))
            pairs.append({"a": paths[0], "b": paths[1], "w": size[0], "h": size[1],
                          "opts": detector_defaults(options)})
        spec = self.root / "spec.json"
        spec.write_text(json.dumps({"pairs": pairs}))
        proc = subprocess.run(["node", "-e", HARNESS, str(JS_PATH), str(spec)],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def assertAgree(self, cases):
        js = self._run_js(cases)
        for (before, after, options), got in zip(cases, js):
            _, regions, changed, comparable, _ = compare_images(before, after, options)
            py = {"changed": changed, "comparable": comparable,
                  "regions": [[r.x, r.y, r.w, r.h, r.kind, r.shift_dy] for r in regions]}
            self.assertEqual(got, py)
        return js

    def test_edits_shifts_and_recolours(self):
        cases = []
        for seed in range(4):
            base = _frame(seed)
            for variant in (_frame(seed, recolor=(2, 7)), _frame(seed, dy=3),
                            _frame(seed, dy=11, recolor=(1,)), _frame(seed, dy=-6),
                            _frame(seed, clock="12:07")):
                cases.append((base, variant, DiffOptions()))
        results = self.assertAgree(cases)
        kinds = {r[4] for res in results for r in res["regions"]}
        self.assertEqual(kinds, {"changed", "moved"}, "cases should exercise both region kinds")

    def test_regions_touching_both_side_edges(self):
        # Two marks on the right edge with a gap between them, and one on the
        # left edge level with that gap: a dilation that wraps around glues
        # the right-hand pair into one region.
        before = _marks(160, 320, [])
        after = _marks(160, 320, [(144, 128, 16, 16), (144, 208, 16, 16), (0, 168, 16, 16)])
        (result,) = self.assertAgree([(before, after, DiffOptions(detect_shift=False))])
        self.assertEqual(len(result["regions"]), 3)

    def test_policy_ignore_boxes_and_presets(self):
        options = DiffOptions(ignore=["status_bar", "ios_home_indicator"],
                              ignore_boxes=[(0.5, 0.3, 0.45, 0.1)])
        before = _marks(200, 400, [])
        after = _marks(200, 400, [(120, 125, 60, 30), (0, 396, 200, 4), (20, 250, 40, 40)])
        (result,) = self.assertAgree([(before, after, options)])
        self.assertEqual(len(result["regions"]), 1, "only the unmasked mark should remain")

    def test_masking_disabled_and_mismatched_heights(self):
        self.assertAgree([
            (_frame(1, clock="9:41"), _frame(1, clock="12:07"), DiffOptions(ignore=[])),
            (_frame(2), _frame(2, h=460), DiffOptions()),
            (_frame(3, recolor=(5,)), _frame(3), DiffOptions(merge_radius=0, min_region_px=0)),
        ])


class EdgeDilation(unittest.TestCase):
    def test_dilation_does_not_wrap_around(self):
        mask = np.zeros((6, 10), dtype=bool)
        mask[3, 0] = True
        out = _dilate(mask, 2)
        self.assertTrue(out[1:6, 0:3].all())
        self.assertFalse(out[:, 8:].any(), "left-edge change leaked onto the right edge")

    def test_dilation_is_a_square_neighbourhood(self):
        mask = np.zeros((9, 9), dtype=bool)
        mask[4, 4] = True
        out = _dilate(mask, 2)
        expected = np.zeros_like(mask)
        expected[2:7, 2:7] = True
        np.testing.assert_array_equal(out, expected)

    def test_side_edge_regions_stay_separate(self):
        before = _marks(160, 320, [])
        after = _marks(160, 320, [(144, 128, 16, 16), (144, 208, 16, 16), (0, 168, 16, 16)])
        _, regions, _, _, _ = compare_images(before, after, DiffOptions(detect_shift=False))
        self.assertEqual(len(regions), 3)


class ReportDefaults(unittest.TestCase):
    def test_every_ignore_box_reaches_the_page(self):
        options = DiffOptions(ignore=["status_bar", "android_nav_bar", "not_a_preset"],
                              ignore_boxes=[(0.1, 0.2, 0.3, 0.4)])
        defaults = detector_defaults(options)
        self.assertTrue(defaults["ignoreStatusBar"])
        self.assertEqual(defaults["ignoreBoxes"], [[0.0, 0.95, 1.0, 0.05], [0.1, 0.2, 0.3, 0.4]])

    def test_status_bar_can_be_switched_off(self):
        self.assertFalse(detector_defaults(DiffOptions(ignore=[]))["ignoreStatusBar"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
