"""The generated HTML must survive hostile build metadata.

Commit messages, author names, screen names and git remotes come from whoever
pushed the build, so the report treats them as untrusted. No network and no
revyl CLI: the client is faked and the frames are synthetic.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from atlas_review import AtlasReview  # noqa: E402
from atlas_review.client import AtlasClient  # noqa: E402
from atlas_review.models import Build  # noqa: E402
from atlas_review.report import script_json  # noqa: E402

JS_PATH = Path(__file__).resolve().parents[1] / "src" / "atlas_review" / "assets" / "report.js"

HOSTILE_MESSAGE = (
    'fix: <img src=x onerror="alert(1)"> </script><script>alert(2)</script> '
    "<!--<script> /*__JS__*/ __DATA__ /*__CSS__*/ & done"
)
HOSTILE_NAME = "<svg onload=alert(3)>"


class FakeClient(AtlasClient):
    """An AtlasClient that serves canned builds and graphs instead of shelling out."""

    def __init__(self, app, builds, graphs):  # noqa: D107 -- skips find_revyl()
        self.app = app
        self.cache_dir = Path(".")
        self.revyl_bin = "revyl"
        self.timeout = 1
        self.repo = None
        self.refresh = False
        self._builds = builds
        self._graphs = {}
        self._canned = graphs

    def graph(self, build_id, with_screenshots=True):
        return self._canned[build_id]

    def _run_json(self, args):  # pragma: no cover - would mean a real CLI call
        raise AssertionError("tests must never call the revyl CLI: %r" % (args,))


def _frame(color):
    img = Image.new("RGB", (120, 240), (255, 255, 255))
    ImageDraw.Draw(img).rectangle([10, 60, 90, 120], fill=color)
    return img


def _review(root: Path) -> AtlasReview:
    builds = []
    graphs = {}
    for n, color in enumerate([(20, 20, 20), (200, 30, 30)], start=1):
        build = Build.from_api({
            "id": "%08d-0000-0000-0000-000000000000" % n,
            "version": "main-%d" % n,
            "uploaded_at": "2026-08-13T12:0%d:00Z" % n,
            "metadata": {"git": {
                "commit": "%040d" % n,
                "branch": "main",
                "message": HOSTILE_MESSAGE,
                "author": "<b>mallory</b>",
                # A quote in the remote used to break out of the single-quoted PR href.
                "remote": "https://github.com/acme/voy'onmouseover='alert(4)'age.git",
                "pr": 40 + n,
            }},
        }, ordinal=n)
        builds.append(build)
        path = root / ("frame-%d.png" % n)
        _frame(color).save(path)
        graphs[build.id] = {"nodes": [{
            "id": "screen-1",
            "semantic_name": HOSTILE_NAME,
            "product_area": "<i>area</i>",
            "local_screenshot_path": str(path),
            "observation_count": 1,
        }]}

    def fake_client(app, **_kwargs):
        return FakeClient(app, builds, graphs)

    with mock.patch("atlas_review.api.AtlasClient", fake_client):
        return AtlasReview("app-1", workdir=root / ".atlas-review")


def _data_script(html: str) -> str:
    match = re.search(r"<script>window\.__ATLAS_DATA__ = (.*?);</script>", html, re.S)
    assert match, "data script not found"
    return match.group(1)


class ScriptJson(unittest.TestCase):
    def test_markup_never_appears_inside_the_data(self):
        text = script_json({"m": HOSTILE_MESSAGE})
        for needle in ("<", ">", "&"):
            self.assertNotIn(needle, text)

    def test_round_trips_to_the_same_value(self):
        payload = {"m": HOSTILE_MESSAGE, "u": "café    ", "n": [1, 2.5, None]}
        self.assertEqual(json.loads(script_json(payload)), payload)


class HostileReport(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.review = _review(self.root)
        self.html = self.review.report(self.root / "out.html").read_text()

    def tearDown(self):
        self.tmp.cleanup()

    def test_data_script_cannot_close_or_reopen_script_elements(self):
        data = _data_script(self.html)
        self.assertNotIn("</", data)
        self.assertNotIn("<!--", data)
        self.assertNotIn("<script", data.lower())

    def test_untrusted_strings_survive_intact(self):
        payload = json.loads(_data_script(self.html))
        self.assertEqual(payload["builds"][0]["message"], HOSTILE_MESSAGE)
        self.assertEqual(payload["screens"][0]["name"], HOSTILE_NAME)

    def test_placeholders_inside_data_are_not_substituted(self):
        js = JS_PATH.read_text()
        self.assertEqual(self.html.count(js), 1, "the script must be inserted exactly once")
        self.assertNotIn("/*__JS__*/", self.html.replace(_data_script(self.html), ""))

    def test_exactly_two_script_elements(self):
        # One data script and one code script: any extra boundary means the
        # data broke out of its element.
        self.assertEqual(self.html.count("<script>"), 2)
        self.assertEqual(self.html.count("</script>"), 2)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class JsEscaping(unittest.TestCase):
    """Run the report's own esc() under node rather than pattern-matching it."""

    def _esc(self, value: str) -> str:
        source = JS_PATH.read_text()
        match = re.search(r"function esc\(s\) \{.*?\n\}", source, re.S)
        self.assertIsNotNone(match, "esc() not found in report.js")
        script = match.group(0) + "\nprocess.stdout.write(esc(%s));" % json.dumps(value)
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_escapes_both_quote_styles(self):
        self.assertEqual(self._esc("a'b\"c"), "a&#39;b&quot;c")

    def test_escapes_markup(self):
        self.assertEqual(self._esc("<a href=x>&</a>"), "&lt;a href=x&gt;&amp;&lt;/a&gt;")


class JsClassAttributes(unittest.TestCase):
    """Status and severity come from review.json and the policy file."""

    def test_status_and_severity_are_escaped_where_they_become_markup(self):
        source = JS_PATH.read_text()
        for raw in ("' + st + '", "' + sev + '", "' + a.severity + '"):
            self.assertNotIn(raw, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
