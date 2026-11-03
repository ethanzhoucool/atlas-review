"""The report's decoded-frame cache must stay bounded on large apps.

Runs the cache code from report.js under node; skipped when node is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

JS_PATH = Path(__file__).resolve().parents[1] / "src" / "atlas_review" / "assets" / "report.js"

HARNESS = r"""
const fs = require("fs");
let src = fs.readFileSync(process.argv[1], "utf8");
const end = src.lastIndexOf("})();");
src = src.slice(0, end) +
  "globalThis.__cache = { put: cachePixels, map: function () { return pixelCache; }," +
  " bytes: function () { return pixelCacheBytes; }, budget: PIXEL_CACHE_BYTES, pixels: pixels };\n" + src.slice(end);
globalThis.window = { __ATLAS_DATA__: { app: "t", screens: [], builds: [], images: {}, alerts: [], defaults: {} } };
globalThis.document = { readyState: "loading", addEventListener: function () {} };
globalThis.localStorage = { getItem: function () { return null; }, setItem: function () {} };
(0, eval)(src);
const C = globalThis.__cache, MB = 1024 * 1024, out = { budget: C.budget };
// Fake ImageData: the cache only looks at data.length.
function frame(mb) { return { data: { length: mb * MB } }; }
// A cache hit makes that frame the most recently used, so it outlives later inserts.
const perFrame = C.budget / 4 + 1;
["a", "b", "c"].forEach(function (k) { C.put(k + "|s|1x1", { data: { length: perFrame } }); });
C.pixels({ build_id: "a", screen_id: "s" }, 1, 1).then(function () {
  C.put("d|s|1x1", { data: { length: perFrame } });
  out.afterHit = Array.from(C.map().keys());
  for (let i = 0; i < 400; i++) C.put("f" + i, frame(1.7));   // a 400-frame browsing session
  out.bytesAfterMany = C.bytes();
  out.sizeAfterMany = C.map().size;
  out.newestKept = C.map().has("f399");
  out.oldestKept = C.map().has("f0");
  for (let i = 0; i < 3; i++) C.put("huge" + i, frame(C.budget / MB));  // frames bigger than the budget
  out.sizeAfterHuge = C.map().size;
  process.stdout.write(JSON.stringify(out));
});
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PixelCacheBudget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(["node", "-e", HARNESS, str(JS_PATH)],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.result = json.loads(proc.stdout)

    def test_bytes_never_exceed_the_budget(self):
        self.assertLessEqual(self.result["bytesAfterMany"], self.result["budget"])
        self.assertLess(self.result["sizeAfterMany"], 400)

    def test_it_still_holds_enough_for_an_overview(self):
        # One pair across ~50 screens is ~100 frames; the window must cover that.
        self.assertGreaterEqual(self.result["sizeAfterMany"], 100)

    def test_least_recently_added_goes_first(self):
        self.assertTrue(self.result["newestKept"])
        self.assertFalse(self.result["oldestKept"])

    def test_a_cache_hit_counts_as_recent_use(self):
        self.assertEqual(self.result["afterHit"], ["c|s|1x1", "a|s|1x1", "d|s|1x1"])

    def test_the_current_pair_survives_even_when_oversized(self):
        self.assertEqual(self.result["sizeAfterHuge"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
