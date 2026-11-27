"""1.0.8 (WS-A7): two sign-in stragglers from the audit (AUDIT-1.0.8.md, A7).

* F4 -- a live event stream opened with HTTP Basic outlived Username &
  Password being turned off (with OIDC kept on): sessions end through
  ``revoke_mechanism``, but a Basic subscriber has no session to end. The
  stream's re-check (``_sse_still_authorized``) now asks the mechanism too.
* F5 -- in open mode any web page could include ``/ui-theme-bootstrap.js``
  as a script and read the account and desktop flags it carries. Every
  response outside the API is now this origin's alone
  (``Cross-Origin-Resource-Policy: same-origin``); the API, answered to
  credentialed clients and CORS fetches, is unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api import routes  # noqa: E402
from auth.models import AuthMechanism, Principal  # noqa: E402
from main import request_id_middleware  # noqa: E402


def _basic(**settings):
    principal = Principal(authenticated=True, mechanism=AuthMechanism.HTTP_BASIC, subject="ada",
                          account_id="acct-1", role="user")
    return routes._SseSubscriber(principal), SimpleNamespace(**settings)


def test_a_basic_stream_ends_when_username_and_password_is_turned_off(monkeypatch):
    subscriber, on = _basic(auth_password_enabled=True, auth_oidc_enabled=True)
    monkeypatch.setattr(routes, "get_settings", lambda: on)
    assert routes._sse_still_authorized(subscriber) is True
    _, off = _basic(auth_password_enabled=False, auth_oidc_enabled=True)
    monkeypatch.setattr(routes, "get_settings", lambda: off)
    assert routes._sse_still_authorized(subscriber) is False


def test_other_credentialed_streams_are_judged_as_before(monkeypatch):
    off = SimpleNamespace(auth_password_enabled=False, auth_oidc_enabled=True)
    monkeypatch.setattr(routes, "get_settings", lambda: off)
    token = Principal(authenticated=True, mechanism=AuthMechanism.API_TOKEN, subject="arr", account_id="acct-2",
                      role="integration")
    assert routes._sse_still_authorized(routes._SseSubscriber(token)) is True


def _client() -> TestClient:
    app = FastAPI()
    app.middleware("http")(request_id_middleware)

    @app.get("/ui-theme-bootstrap.js")
    def bootstrap() -> Response:
        return Response("window.downloadPulseDesktop=true;", media_type="text/javascript")

    @app.get("/api/browser/v1/session")
    def api() -> dict:
        return {"ok": True}
    return TestClient(app)


def test_interface_files_are_this_origins_alone_and_the_api_is_unchanged():
    client = _client()
    assert client.get("/ui-theme-bootstrap.js").headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "Cross-Origin-Resource-Policy" not in client.get("/api/browser/v1/session").headers
