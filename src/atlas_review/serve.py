"""Local review server: the same report UI, but approvals write to the store.

Static reports are shareable; served reports are actionable. Both render from
the same payload, so a reviewer sees the same thing either way.

The write endpoints change `review.json`, which is what `atlas-review check`
trusts, so they only accept requests the report itself could have made: a JSON
body (which a cross-site page cannot send without a CORS preflight this server
never answers), from the server's own origin, addressed to a host name that is
not a DNS-rebinding alias.
"""

from __future__ import annotations

import ipaddress
import json
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from .report import build_report
from .review import VALID_STATUSES, Comment, Decision

if TYPE_CHECKING:  # pragma: no cover
    from .api import AtlasReview

# Plenty for a comment; stops a stray client from making the server buffer gigabytes.
MAX_BODY_BYTES = 1 << 20


class _BadRequest(ValueError):
    pass


def _host_allowed(host_header: Optional[str], bound_host: str) -> bool:
    """Is the Host header one this server can legitimately be reached by?

    A DNS-rebinding page reaches 127.0.0.1 under its own domain name, so it
    shows up here as a host name that is neither `localhost`, an IP literal,
    nor the name the server was started with. A server deliberately bound to
    every interface (`--host 0.0.0.0`) is reachable by whatever name the LAN
    uses for this machine, so the check is skipped there.
    """
    if bound_host in ("", "0.0.0.0", "::"):
        return True
    if not host_header:
        return False
    name = host_header.strip().lower()
    if name.startswith("["):  # [::1]:7391
        name = name[1:].split("]", 1)[0]
    elif name.count(":") == 1:
        name = name.split(":", 1)[0]
    if name in ("localhost", bound_host.lower()):
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def _string(payload: Dict[str, Any], key: str, required: bool = True, default: str = "") -> str:
    value = payload.get(key)
    if value is None or value == "":
        if required:
            raise _BadRequest("missing field '%s'" % key)
        return default
    if not isinstance(value, str):
        raise _BadRequest("field '%s' must be a string" % key)
    return value


def _region(value: Any) -> Optional[list]:
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
        raise _BadRequest("field 'region' must be null or [x, y, w, h]")
    return [float(v) for v in value]


def _decision(payload: Dict[str, Any]) -> Decision:
    status = _string(payload, "status")
    if status not in VALID_STATUSES:
        raise _BadRequest("status must be one of %s" % ", ".join(VALID_STATUSES))
    return Decision(
        screen_id=_string(payload, "screen_id"),
        build_id=_string(payload, "build_id"),
        status=status,
        author=_string(payload, "author", required=False, default="reviewer"),
        decided_at=_string(payload, "decided_at", required=False),
        note=_string(payload, "note", required=False),
    )


def _comment(payload: Dict[str, Any]) -> Comment:
    return Comment(
        id=_string(payload, "id"),
        screen_id=_string(payload, "screen_id"),
        build_id=_string(payload, "build_id"),
        body=_string(payload, "body"),
        author=_string(payload, "author", required=False, default="reviewer"),
        created_at=_string(payload, "created_at", required=False),
        region=_region(payload.get("region")),
        resolved=bool(payload.get("resolved")),
    )


class _Handler(SimpleHTTPRequestHandler):
    review: "AtlasReview"
    bound_host: str = "127.0.0.1"
    # One store, many request threads: serialise mutate + save.
    lock: threading.Lock

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        return

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        if _host_allowed(self.headers.get("Host"), self.bound_host):
            return True
        self.send_error(403, "unexpected Host header")
        return False

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self._host_ok():
            super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802 (stdlib naming)
        if self._host_ok():
            super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        if not _host_allowed(self.headers.get("Host"), self.bound_host):
            return self._json(403, {"error": "unexpected Host header"})
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://%s" % self.headers.get("Host"):
            return self._json(403, {"error": "cross-origin writes are not allowed"})
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return self._json(415, {"error": "expected Content-Type: application/json"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad Content-Length"})
        if length < 0 or length > MAX_BODY_BYTES:
            return self._json(413, {"error": "request body too large"})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "bad request body"})
        if not isinstance(payload, dict):
            return self._json(400, {"error": "request body must be a JSON object"})

        routes: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "/api/decide": _decision,
            "/api/comment": _comment,
        }
        parse = routes.get(self.path)
        if parse is None:
            return self._json(404, {"error": "no such endpoint"})
        try:
            record = parse(payload)
        except _BadRequest as exc:
            return self._json(400, {"error": str(exc)})

        store = self.review.store
        with self.lock:
            if isinstance(record, Decision):
                store.decisions[store._key(record.screen_id, record.build_id)] = record
            else:
                store.comments.append(record)
            store.save()
        self._json(200, {"ok": True, "saved_to": str(store.path)})


def make_handler(review: "AtlasReview", directory: Path, host: str = "127.0.0.1") -> Callable[..., Any]:
    """A request handler bound to one review store and one served directory."""
    handler_cls = type("BoundHandler", (_Handler,), {
        "review": review,
        "bound_host": host,
        "lock": threading.Lock(),
    })
    return partial(handler_cls, directory=str(directory))


def serve(
    review: "AtlasReview",
    out: Path,
    host: str = "127.0.0.1",
    port: int = 7391,
    open_browser: bool = True,
) -> None:
    """Render the report into `out` and serve it until interrupted."""
    build_report(review, out=out, served=True)
    server = ThreadingHTTPServer((host, port), make_handler(review, out, host))
    url = "http://%s:%d/index.html" % (host, port)
    print("atlas-review serving %s" % url)
    print("approvals and comments write to %s" % review.store.path)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
