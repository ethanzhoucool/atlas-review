"""Build ordering and build metadata that does not look like the happy path.

No network and no revyl CLI: the client is faked and the frames are synthetic.
"""

from __future__ import annotations

import datetime as _dt
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from atlas_review import AtlasReview, Build, Policy  # noqa: E402
from atlas_review.alerts import builds_through  # noqa: E402
from atlas_review.client import AtlasClient  # noqa: E402

UTC = _dt.timezone.utc


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


def _build(n, uploaded_at):
    return Build(id="build-%d" % n, version="v%d" % n, ordinal=n, uploaded_at=uploaded_at,
                 commit="%040d" % n, commit_short="%07d" % n)


def _review(root: Path, uploaded):
    """One screen across len(uploaded) builds; the last build adds a red box."""
    builds, graphs = [], {}
    for n, when in enumerate(uploaded, start=1):
        build = _build(n, when)
        builds.append(build)
        img = Image.new("RGB", (120, 240), (255, 255, 255))
        if n == len(uploaded):
            ImageDraw.Draw(img).rectangle([10, 80, 100, 160], fill=(200, 30, 30))
        path = root / ("frame-%d.png" % n)
        img.save(path)
        graphs[build.id] = {"nodes": [{"id": "screen-1", "semantic_name": "home",
                                       "local_screenshot_path": str(path)}]}

    def fake_client(app, **_kwargs):
        return FakeClient(app, builds, graphs)

    policy = Policy.from_dict({"compare_against": "previous", "default": {"max_change_pct": 1.0}})
    with mock.patch("atlas_review.api.AtlasClient", fake_client):
        return AtlasReview("app-1", workdir=root / ".atlas-review", policy=policy)


class MissingOrMixedTimestamps(unittest.TestCase):
    CASES = {
        "a build with no uploaded_at": [
            _dt.datetime(2026, 8, 13, 12, 0, tzinfo=UTC), None,
            _dt.datetime(2026, 8, 13, 12, 2, tzinfo=UTC)],
        "naive and aware timestamps": [
            _dt.datetime(2026, 8, 13, 12, 0), _dt.datetime(2026, 8, 13, 12, 1, tzinfo=UTC),
            _dt.datetime(2026, 8, 13, 12, 2, tzinfo=UTC)],
        "no timestamps at all": [None, None, None],
    }

    def setUp(self):
        self.tmp = TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_check_compares_against_the_previous_build(self):
        for label, uploaded in self.CASES.items():
            with self.subTest(label):
                root = Path(self.tmp.name) / label.replace(" ", "-")
                root.mkdir()
                report = _review(root, uploaded).check()
                budget = [a for a in report.alerts if a.code == "change_budget_exceeded"]
                self.assertEqual(len(budget), 1, [a.message for a in report.alerts])
                self.assertEqual(budget[0].baseline.id, "build-2")

    def test_changes_finds_the_new_box(self):
        for label, uploaded in self.CASES.items():
            with self.subTest(label):
                root = Path(self.tmp.name) / ("changes-" + label.replace(" ", "-"))
                root.mkdir()
                results = _review(root, uploaded).changes()
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].before.build.id, "build-2")

    def test_older_head_only_sees_earlier_builds(self):
        root = Path(self.tmp.name) / "older"
        root.mkdir()
        review = _review(root, [None, None, None, None])
        report = review.check("#2")
        # Build 2 is identical to build 1, so nothing alerts -- and in
        # particular build 3 or 4 must not have been used as its baseline.
        self.assertEqual([a for a in report.alerts if a.baseline is not None and
                          a.baseline.ordinal > 2], [])


class BuildsThrough(unittest.TestCase):
    def test_slices_by_position(self):
        builds = [_build(n, None) for n in range(1, 5)]
        self.assertEqual([b.id for b in builds_through(builds, builds[1])], ["build-1", "build-2"])

    def test_unknown_head_keeps_every_build_before_it(self):
        builds = [_build(n, None) for n in range(1, 3)]
        stray = _build(9, None)
        self.assertEqual([b.id for b in builds_through(builds, stray)],
                         ["build-1", "build-2", "build-9"])


class PrNumbers(unittest.TestCase):
    def _from_git(self, **git):
        git.setdefault("commit", "a" * 40)
        git.setdefault("remote", "git@github.com:acme/app.git")
        return Build.from_api({"id": "b", "version": "v", "metadata": {"git": git}}, ordinal=1)

    def test_string_pr_numbers_are_accepted(self):
        for raw in (42, "42", "#42", " 42 "):
            with self.subTest(raw=raw):
                build = self._from_git(pr=raw)
                self.assertEqual(build.pr_number, 42)
                self.assertEqual(build.pr_url, "https://github.com/acme/app/pull/42")
                self.assertEqual(build.to_dict()["pr_number"], 42)

    def test_pr_number_key_is_read_too(self):
        self.assertEqual(self._from_git(pr_number="7").pr_number, 7)

    def test_junk_pr_values_are_dropped_instead_of_crashing(self):
        for raw in ("", "abc", "12a", True, -3, 0, "²", None):
            with self.subTest(raw=raw):
                build = self._from_git(pr=raw)
                self.assertIsNone(build.pr_number)
                self.assertIsNone(build.to_dict()["pr_url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
