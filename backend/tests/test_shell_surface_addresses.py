"""Each interface page has its own address (WS11, Server 10).

The server serves the shell at exactly ``main.SHELL_SURFACE_PATHS`` as it does
at "/": the same bytes, headers, caching and authentication. Nothing else falls
back to the shell. The addresses have one owner (``api.auth_routes.PAGE_ADDRESSES``):
the server serves the shell at them and the browser's navigation owner
(``app.js`` ``VIEW_PATHS``) reads them from the bootstrap the server prepends
to its bundle. The Details window closes on Escape through the shared modal
owner (``DPModal``), not a second key listener.
"""
from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

import core.config as config
import db.database as database
from account_support import account
from auth.accounts import Role
from auth.models import Principal
from auth.sessions import HTTP_SESSION_COOKIE, session_store

pytestmark = pytest.mark.asyncio

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
SURFACES = ("/downloads", "/activity", "/statistics", "/settings", "/help")
COMPARED_HEADERS = ("content-type", "cache-control", "x-content-type-options", "referrer-policy",
                    "x-frame-options", "content-security-policy", "etag", "content-length")


@pytest_asyncio.fixture
async def served(tmp_path, monkeypatch):
    """The real application and middleware, password mode, one User session."""
    import main

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "shell.db")
    await database.init_db()
    monkeypatch.setattr(config, "_settings", config.AppSettings(auth_password_enabled=True))

    @asynccontextmanager
    async def operation():
        yield

    monkeypatch.setitem(main.app.state._state, "application", SimpleNamespace(application_operation=operation))
    session_store.clear()
    token, _ = session_store.create(
        Principal.password_session(account("user", account_id="user-id", role=Role.USER)), lifetime_seconds=3600)
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        yield client, {"Cookie": f"{HTTP_SESSION_COOKIE}={token}"}
    session_store.clear()


async def test_server_and_navigation_owner_name_the_same_addresses(served):
    import main
    from api.auth_routes import PAGE_ADDRESSES

    assert main.SHELL_SURFACE_PATHS == SURFACES
    assert PAGE_ADDRESSES == {"dashboard": "/", "torrents": "/downloads", "events": "/activity",
                              "stats": "/statistics", "settings": "/settings", "help": "/help"}
    # The page reads the server's list from its bundle's bootstrap; it keeps none of its own.
    client, user = served
    bundle = (await client.get("/app.js", headers=user)).text
    head = bundle.split("window.downloadPulsePageAddresses=Object.freeze(", 1)[1].split(");", 1)[0]
    paths = json.loads(head)
    assert paths == PAGE_ADDRESSES
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert app_js.count("const VIEW_PATHS = window.downloadPulsePageAddresses;") == 1
    assert bundle.index("window.downloadPulsePageAddresses=") < bundle.index("const VIEW_PATHS")
    assert not re.search(r"'/(downloads|activity|statistics)'", app_js)
    # Every page the shell owns has an address, and no address names a page it lacks.
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert all(f'id="view-{view}"' in index for view in paths)
    # Back/Forward and reload read the address; navigation writes it through History.
    assert "window.addEventListener('popstate'" in app_js
    # 1.0.5 (WS8b1, changed assertion): start-up opens the addressed page from one
    # DOMContentLoaded handler -- at once anywhere but the desktop window, and there
    # once DPWindowState has said whether a released window comes back elsewhere.
    start = app_js.split("document.addEventListener('DOMContentLoaded', () => {", 1)[1].split("\n});", 1)[0]
    assert "if (!DPWindowState.active) { showAddressedPage(); resolve(); return; }" in start
    assert start.count("showAddressedPage();") == 2
    assert "pushState" in app_js and "replaceState" in app_js
    assert "hashchange" not in app_js


async def test_each_page_address_serves_the_shell_exactly_as_root(served):
    client, user = served
    root = await client.get("/", headers=user)
    assert root.status_code == 200 and root.headers["content-type"].startswith("text/html")
    for path in SURFACES:
        page = await client.get(path, headers=user)
        assert page.status_code == 200, path
        assert page.content == root.content, path
        for header in COMPARED_HEADERS:
            assert page.headers.get(header) == root.headers.get(header), (path, header)
        assert page.headers["cache-control"] == "no-cache, must-revalidate"
        head = await client.head(path, headers=user)
        assert head.status_code == 200 and head.content == b"", path
        # Conditional revalidation is answered as for "/".
        cached = await client.get(path, headers={**user, "If-None-Match": root.headers["etag"]})
        assert cached.status_code == (await client.get("/", headers={**user, "If-None-Match": root.headers["etag"]})).status_code


async def test_an_unauthenticated_page_address_takes_the_login_flow_of_root(served):
    client, _user = served
    root = await client.get("/", headers={"Accept": "text/html"})
    assert root.status_code == 303 and root.headers["location"] == "/login?next=%2F"
    for path in SURFACES:
        page = await client.get(path, headers={"Accept": "text/html"})
        assert page.status_code == root.status_code, path
        assert page.headers["location"] == "/login?next=" + path.replace("/", "%2F"), path
        assert (await client.get(path)).status_code == (await client.get("/")).status_code == 401


async def test_nothing_else_falls_back_to_the_shell(served):
    client, user = served
    for path in ("/api/unknown", "/downloads/", "/downloads/x", "/dashboard", "/unknown", "/login/x"):
        response = await client.get(path, headers=user)
        assert response.status_code == 404, path
        assert not response.headers.get("content-type", "").startswith("text/html"), path
    asset = await client.get("/style.css", headers=user)
    assert asset.status_code == 200 and asset.headers["content-type"].startswith("text/css")
    login = await client.get("/login", headers={"Accept": "text/html"})
    assert login.status_code == 200 and login.headers["content-type"].startswith("text/html")


def test_escape_closes_details_through_the_shared_modal_owner():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    modal = app_js[app_js.index("const DPModal = (function () {"):app_js.index("window.DPModal = DPModal;")]
    # One Escape listener, the shell's own, attached only while an opening asks for it --
    # on the window, so every owner on the document (a menu, a DPSettingsModal dialog)
    # hears Escape before it, whatever order they were added in.
    assert modal.count("addEventListener('keydown', escapeClose)") == 1
    assert modal.count("window.addEventListener('keydown', escapeClose)") == 1
    assert "document.addEventListener('keydown', escapeClose)" not in modal
    assert "requestModalClose('escape')" in modal
    assert "event.defaultPrevented" in modal and "window.DPSettingsModal?.showing()" in modal
    assert "if (window.DPTooltip?.dismissedBy(event)) return;" in modal
    # Details asks for it; no other owner adds an Escape listener for Details.
    assert re.search(r"DPModal\.open\(\{mode: 'details'[^}]*escape: true\}\)", app_js)
    # Focus goes back to the invoking row, or the row redrawn in its place, through
    # the dialog shell's one focus contract (no search of its own).
    assert "window.DPSettingsModal.focusTarget(origin)" in modal
    assert "window.DPSettingsModal.focusOrigin()" in modal
    assert "returnTargetNow" not in app_js and "data-torrent-id=" not in modal
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert index.count("/app.js?v=51") == 1
