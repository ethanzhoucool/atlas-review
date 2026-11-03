"""CLI and config mistakes should produce one readable line, not a traceback.

No network: the revyl CLI is either a nonexistent path or a tiny fake script.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from atlas_review import Policy  # noqa: E402
from atlas_review.cli import _parse_region, main  # noqa: E402
from atlas_review.client import AtlasClient, RevylError  # noqa: E402

DEMO_CONFIG = Path(__file__).resolve().parents[1] / "demo" / ".atlas-review.json"


def _fake_revyl(root: Path, body: str) -> Path:
    script = root / "revyl"
    script.write_text("#!%s\n%s\n" % (sys.executable, body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class ConfigFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _main(self, config_text, *argv):
        path = self.root / "config.json"
        path.write_text(config_text)
        stderr = io.StringIO()
        with redirect_stderr(stderr), mock.patch.dict(os.environ, {"REVYL_BIN": "/nonexistent/revyl"}):
            try:
                code = main(["--config", str(path)] + list(argv or ["builds"]))
            except SystemExit as exc:
                return exc.code, str(exc.code)
        return code, stderr.getvalue()

    def test_invalid_json_names_the_file_and_position(self):
        code, message = self._main('{"app": "x",\n "policy": {,}}')
        self.assertIn("config.json is not valid JSON (line 2, column", str(message))

    def test_non_object_config(self):
        _, message = self._main("[1, 2]")
        self.assertIn("must contain a JSON object", str(message))

    def test_policy_mistakes_name_the_key(self):
        cases = {
            '{"default": {"severity": "critical"}}':
                "policy.default: severity must be one of error, warning, info",
            '{"screens": {"home": {"watch": [{"box": [0, 0, 1, 1], "severity": "loud"}]}}}':
                "policy.screens.home.watch[0]: severity must be one of",
            '{"screens": {"home": {"watch": [{"box": [0.1, 0.2]}]}}}':
                "policy.screens.home.watch[0].box must be [x, y, w, h]",
            '{"screens": {"home": {"watch": [{"bbox": [0, 0, 1, 1]}]}}}':
                'policy.screens.home.watch[0] needs a "box"',
            '{"compare_against": "prev"}': "policy.compare_against must be one of approved, previous",
            '{"diff": {"ignore": ["statusbar"]}}': "policy.diff.ignore: unknown preset 'statusbar'",
            '{"diff": {"tolerance": "lots"}}': "policy.diff.tolerance must be a number",
            '{"diff": {"ignore_boxes": [[0, 0, 2, 1]]}}': "policy.diff.ignore_boxes[0] must be",
            '{"default": {"max_change_pct": "two"}}': "policy.default.max_change_pct must be a number",
        }
        for policy, expected in cases.items():
            with self.subTest(policy=policy):
                code, stderr = self._main('{"app": "x", "policy": %s}' % policy)
                self.assertEqual(code, 2)
                self.assertIn("error: " + expected, stderr)
                self.assertNotIn("Traceback", stderr)

    def test_shipped_configs_still_load(self):
        Policy.from_dict(json.loads(DEMO_CONFIG.read_text())["policy"])
        from atlas_review import cli

        with TemporaryDirectory() as tmp, mock.patch.object(cli, "DEFAULT_CONFIG",
                                                            str(Path(tmp) / "c.json")):
            with redirect_stderr(io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
                main(["init", "--app", "a"])
            Policy.from_dict(json.loads((Path(tmp) / "c.json").read_text())["policy"])


class Regions(unittest.TestCase):
    def test_good_region(self):
        self.assertEqual(_parse_region("0.04, 0.175, 0.9, 0.045"), (0.04, 0.175, 0.9, 0.045))

    def test_bad_regions_explain_the_format(self):
        cases = {
            "0.1,0.2,0.3": "four comma-separated fractions",
            "a,b,c,d": "must be numbers between 0 and 1",
            "10px,10px,5px,5px": "must be numbers between 0 and 1",
            "0.1,0.2,0,0.5": "non-zero width and height",
            "0.1,0.2,1.5,0.5": "fractions of the frame (0-1)",
        }
        for text, expected in cases.items():
            with self.subTest(region=text):
                with self.assertRaises(SystemExit) as caught:
                    _parse_region(text)
                self.assertIn(expected, str(caught.exception.code))
                self.assertIn("e.g. --region", str(caught.exception.code))


class RevylBinary(unittest.TestCase):
    def test_missing_binary_is_a_clean_error(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), mock.patch.dict(os.environ, {"REVYL_BIN": "/nonexistent/revyl"}):
            code = main(["--app", "x", "builds"])
        self.assertEqual(code, 2)
        self.assertIn("could not run the revyl CLI at /nonexistent/revyl", stderr.getvalue())

    def test_hung_binary_times_out_cleanly(self):
        with TemporaryDirectory() as tmp:
            script = _fake_revyl(Path(tmp), "import time\ntime.sleep(10)")
            client = AtlasClient("x", cache_dir=Path(tmp), revyl_bin=str(script), timeout=1)
            with self.assertRaises(RevylError) as caught:
                client.builds()
            self.assertIn("timed out after 1s", str(caught.exception))


class ImportFile(unittest.TestCase):
    def test_missing_and_malformed_files(self):
        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "review.json"
            bad.write_text("{nope")
            with mock.patch.dict(os.environ, {"REVYL_BIN": "/nonexistent/revyl"}):
                for path, expected in ((Path(tmp) / "missing.json", "cannot read review file"),
                                       (bad, "is not valid JSON (line 1, column 2)")):
                    with self.subTest(path=path.name):
                        with self.assertRaises(SystemExit) as caught:
                            main(["--app", "x", "--workdir", tmp, "import", str(path)])
                        self.assertIn(expected, str(caught.exception.code))


class BrokenPipe(unittest.TestCase):
    def test_closing_the_pipe_early_is_not_a_crash(self):
        with TemporaryDirectory() as tmp:
            versions = [{"id": "%032d" % i, "version": "v%d" % i,
                         "uploaded_at": "2026-08-13T12:00:%02dZ" % (i % 60),
                         "metadata": {"git": {"commit": "%040d" % i, "message": "m" * 80}}}
                        for i in range(3000)]
            payload = Path(tmp) / "builds.json"
            payload.write_text(json.dumps({"versions": versions}))
            script = _fake_revyl(Path(tmp), "import sys\nsys.stdout.write(open(%r).read())" % str(payload))
            env = dict(os.environ, REVYL_BIN=str(script), PYTHONPATH=str(SRC))
            proc = subprocess.Popen([sys.executable, "-m", "atlas_review.cli", "--app", "x", "builds"],
                                    cwd=tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc.stdout.readline()
            proc.stdout.close()
            stderr = proc.stderr.read().decode()
            proc.stderr.close()
            self.assertEqual(proc.wait(timeout=60), 141)
            self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
