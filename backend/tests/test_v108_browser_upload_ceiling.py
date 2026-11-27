"""1.0.8 (WS-A11): the browser extension can send a .torrent, NZB or Metalink up
to DownloadPulse's own limit (AUDIT-1.0.8.md, A11).

``/api/browser/v1/jobs`` carries the file base64 inside JSON, beside a link's
bounded sign-in and referrer. It had no ceiling of its own, so the 20 MiB
default body limit refused every file above ~15.7 MB before the intake's own
16 MB limit could judge it -- with a non-JSON 413 every released extension
showed as "send failed". Its ceiling is now the largest inline kind's, encoded,
plus room; it only widens, so every extension 1.0.2-1.0.7 sends what it did.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest
from starlette.responses import Response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from external.arr import BROWSER_JOBS_PATH, UPLOAD_CEILINGS  # noqa: E402
from external.intake import MAX_METALINK_BYTES, MAX_NZB_BYTES, MAX_TORRENT_BYTES  # noqa: E402


def _admitted(path: str, length: int) -> bool:
    sent, reached = [], []

    async def downstream(scope, receive, send):
        reached.append(True)
        await Response(status_code=204)(scope, receive, send)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": [(b"content-length", str(length).encode())], "client": ("127.0.0.1", 1)}
    asyncio.run(main.RequestBodyLimitMiddleware(downstream, max_bytes=20 * 1024 * 1024)(scope, receive, send))
    assert sent[0]["status"] == (204 if reached else 413)
    return bool(reached)


def _largest_submission() -> int:
    """A browser job carrying the largest file the intake accepts, with a full sign-in and referrer."""
    largest = max(MAX_TORRENT_BYTES, MAX_METALINK_BYTES, MAX_NZB_BYTES)
    body = {"idempotency_key": "k" * 64, "kind": "torrent", "name": "n" * 255,
            "file": base64.b64encode(b"\0" * largest).decode(), "referrer": "https://r.example/" + "p" * 2030,
            "cookies": [{"name": "c" * 64, "value": "v" * 64, "domain": "d.example", "path": "/"}] * 300}
    return len(json.dumps(body).encode())


def test_the_largest_file_the_intake_accepts_fits_the_browser_path():
    assert _admitted(BROWSER_JOBS_PATH, _largest_submission())


@pytest.mark.parametrize("over, admitted", [(0, True), (1, False)])
def test_the_browser_path_has_its_own_ceiling_above_the_default(over, admitted):
    assert UPLOAD_CEILINGS[BROWSER_JOBS_PATH] > 20 * 1024 * 1024
    assert _admitted(BROWSER_JOBS_PATH, UPLOAD_CEILINGS[BROWSER_JOBS_PATH] + over) is admitted
