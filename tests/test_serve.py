"""The review server's write endpoints, exercised over real HTTP on localhost."""

from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atlas_review import ReviewStore  # noqa: E402
from atlas_review.serve import _host_allowed, make_handler  # noqa: E402


class _Review:
    """The only part of AtlasReview the handler touches."""

    def __init__(self, store: ReviewStore) -> None:
        self.store = store


DECISION = {"screen_id": "s1", "build_id": "b1", "status": "approved",
            "author": "ethan", "decided_at": "2026-08-14T00:00:00+00:00", "note": ""}
COMMENT = {"id": "c1", "screen_id": "s1", "build_id": "b1", "body": "looks good",
           "author": "ethan", "created_at": "2026-08-14T00:00:00+00:00",
           "region": None, "resolved": False}


class ServeEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "index.html").write_text("<!doctype html><title>report</title>")
        self.store_path = root / "review.json"
        self.store = ReviewStore(self.store_path)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_Review(self.store), root))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = {"Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % self.port}
        sent.update(headers or {})
        sent = {k: v for k, v in sent.items() if v is not None}
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        conn.request(method, path, body=data, headers=sent)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, payload

    def _saved(self):
        return ReviewStore(self.store_path)

    def test_same_origin_json_decision_is_saved(self):
        status, _ = self._request("POST", "/api/decide", DECISION)
        self.assertEqual(status, 200)
        self.assertEqual(self._saved().status("s1", "b1"), "approved")

    def test_same_origin_json_comment_is_saved(self):
        status, _ = self._request("POST", "/api/comment", COMMENT)
        self.assertEqual(status, 200)
        self.assertEqual([c.body for c in self._saved().comments], ["looks good"])

    def test_text_plain_is_refused(self):
        # A cross-site <form> or no-cors fetch can only send "simple" content
        # types, which skip the CORS preflight. Those must not write.
        status, _ = self._request("POST", "/api/decide", DECISION, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertFalse(self.store_path.exists())

    def test_foreign_origin_is_refused(self):
        status, _ = self._request("POST", "/api/decide", DECISION, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse(self.store_path.exists())

    def test_rebound_host_name_is_refused(self):
        headers = {"Host": "evil.example:%d" % self.port, "Origin": "http://evil.example:%d" % self.port}
        status, _ = self._request("POST", "/api/decide", DECISION, headers)
        self.assertEqual(status, 403)
        status, _ = self._request("GET", "/index.html", headers=headers)
        self.assertEqual(status, 403)
        self.assertFalse(self.store_path.exists())

    def test_localhost_page_still_loads(self):
        status, body = self._request("GET", "/index.html", headers={"Host": "localhost:%d" % self.port})
        self.assertEqual(status, 200)
        self.assertIn(b"report", body)

    def test_clients_without_an_origin_header_can_still_write(self):
        status, _ = self._request("POST", "/api/decide", DECISION, {"Origin": None})
        self.assertEqual(status, 200)

    def test_unknown_status_is_refused(self):
        bad = dict(DECISION, status='"><img src=x onerror=alert(1)>')
        status, body = self._request("POST", "/api/decide", bad)
        self.assertEqual(status, 400)
        self.assertIn(b"status", body)
        self.assertFalse(self.store_path.exists())

    def test_non_string_fields_are_refused(self):
        status, _ = self._request("POST", "/api/comment", dict(COMMENT, body={"html": "<b>"}))
        self.assertEqual(status, 400)
        status, _ = self._request("POST", "/api/comment", dict(COMMENT, region="0,0,1,1"))
        self.assertEqual(status, 400)

    def test_missing_field_is_reported(self):
        bad = {k: v for k, v in DECISION.items() if k != "build_id"}
        status, body = self._request("POST", "/api/decide", bad)
        self.assertEqual(status, 400)
        self.assertIn(b"build_id", body)

    def test_non_object_body_is_refused(self):
        status, _ = self._request("POST", "/api/decide", b"[1, 2, 3]")
        self.assertEqual(status, 400)

    def test_unknown_endpoint_is_404(self):
        status, _ = self._request("POST", "/api/nope", DECISION)
        self.assertEqual(status, 404)


class HostAllowList(unittest.TestCase):
    def test_loopback_names_and_ip_literals(self):
        for host in ("127.0.0.1:7391", "localhost:7391", "[::1]:7391", "LOCALHOST", "192.168.1.5:7391"):
            self.assertTrue(_host_allowed(host, "127.0.0.1"), host)

    def test_other_names_are_rejected(self):
        for host in ("evil.example:7391", "127.0.0.1.evil.example", "", None):
            self.assertFalse(_host_allowed(host, "127.0.0.1"), host)

    def test_the_configured_host_name_is_allowed(self):
        self.assertTrue(_host_allowed("devbox.local:7391", "devbox.local"))

    def test_wildcard_bind_accepts_any_name(self):
        self.assertTrue(_host_allowed("devbox.local:7391", "0.0.0.0"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
