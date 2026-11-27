"""WS27 update notices: one owner for "the latest release".

* ``application.updates.refresh`` is the only reader of ``releases/latest``.
  Its record names this platform's installer by exact name, with the hash
  GitHub states for it (no digest: no installer), on this repository's
  release downloads only, and only on a desktop installation (Linux: inside
  Flatpak). A failed check keeps the last good record and publishes nothing.
* The scheduler's one loop checks about a minute after start, then on the
  interval (a failed check: in an hour), and notifies once per version.
* ``/version/check`` reads the record, never GitHub, for an Admin alone, and
  never says the installer's address or hash.
* ``/desktop/update/download`` is the desktop window's, an Admin's: it submits
  the record's own one-file Metalink4, and a second click returns the same
  transfer while it serves.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

import core.config as config
import db.database as database
from account_support import account
from application import updates
from auth.accounts import DEFAULT_ADMIN_ID, Role
from auth.models import Principal
from auth.permissions import ACTIONS, Capability
from auth.sessions import HTTP_SESSION_COOKIE, session_store

SECRET = "internal /app/data/secret.db path\ntraceback detail"
DIGEST = "ab" * 32
BASE = "https://github.com/Xipher-Zero/downloadpulse/releases"


def _asset(name, *, digest=f"sha256:{DIGEST}", url=None, size=1234):
    asset = {"name": name, "size": size, "browser_download_url": url or f"{BASE}/download/v1.0.4/{name}"}
    if digest is not None:
        asset["digest"] = digest
    return asset


def _release(tag="v1.0.4", **overrides):
    release = {
        "tag_name": tag, "html_url": f"{BASE}/tag/{tag}", "published_at": "2026-12-01T10:00:00Z",
        "body": "Notes", "draft": False, "prerelease": False,
        "assets": [_asset(f"DownloadPulse-1.0.4-{suffix}") for suffix in
                   ("windows-x64.exe", "macos-arm64.dmg", "linux-x86_64.flatpak")]
        + [_asset("SHA256SUMS")],
    }
    release.update(overrides)
    return release


class GitHub:
    """A fake ``releases/latest``: each answer in turn, recording what was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[str] = []

    async def __call__(self, etag):
        self.asked.append(etag)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Releases:
    """A fake ``releases`` list (the release notes): its one answer, counting the asks."""

    def __init__(self, answer):
        self.answer, self.asked = answer, 0

    async def __call__(self):
        self.asked += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(updates, "_record", None)
    monkeypatch.setattr(updates, "_etag", "")
    monkeypatch.setattr(updates, "_download", None)
    monkeypatch.setattr(updates, "_notes", [])
    monkeypatch.setattr(updates, "_fetch_releases", Releases(None))
    monkeypatch.setattr(updates, "read_version", lambda: "1.0.3")
    monkeypatch.setattr(config, "_settings", config.AppSettings())
    monkeypatch.delenv("DOWNLOADPULSE_DESKTOP", raising=False)
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    published = []

    async def publish(event, payload):
        published.append((event, payload))

    import services.event_bus as event_bus
    monkeypatch.setattr(event_bus, "publish", publish)
    return published


MACHINES = {"win32": "AMD64", "darwin": "arm64", "linux": "x86_64"}


def _desktop(monkeypatch, platform, *, flatpak=True, machine=None):
    monkeypatch.setenv("DOWNLOADPULSE_DESKTOP", "1")
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(updates._platform, "machine", lambda: machine or MACHINES.get(platform, "x86_64"))
    if flatpak:
        monkeypatch.setenv("FLATPAK_ID", "io.github.xipher_zero.DownloadPulse")


# ── the record ──────────────────────────────────────────────────────────────

def test_the_record_is_the_latest_release_and_a_server_has_no_installer(monkeypatch, fresh):
    monkeypatch.setattr(updates, "_fetch", GitHub((200, '"e1"', _release())))
    record, notes = asyncio.run(updates.refresh())
    assert {key: value for key, value in record.items() if key != "checked_at"} == {
        "current": "1.0.3", "latest": "1.0.4", "update_available": True,
        "release_url": f"{BASE}/tag/v1.0.4", "published_at": "2026-12-01", "installer": None}
    assert notes == "Notes"
    assert fresh == [("update_status", {})]     # a bare signal: the page reads the record again


@pytest.mark.parametrize("platform,name", [
    ("win32", "DownloadPulse-1.0.4-windows-x64.exe"),
    ("darwin", "DownloadPulse-1.0.4-macos-arm64.dmg"),
    ("linux", "DownloadPulse-1.0.4-linux-x86_64.flatpak"),
])
def test_the_desktop_installer_is_this_platforms_asset_by_exact_name(monkeypatch, platform, name):
    _desktop(monkeypatch, platform)
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release())))
    record, _notes = asyncio.run(updates.refresh())
    assert record["installer"] == {"name": name, "url": f"{BASE}/download/v1.0.4/{name}", "sha256": DIGEST,
                                   "size": 1234}


def test_linux_outside_flatpak_and_an_unknown_platform_get_the_notice_only(monkeypatch):
    for platform, flatpak in (("linux", False), ("freebsd14", True)):
        _desktop(monkeypatch, platform, flatpak=flatpak)
        monkeypatch.setattr(updates, "_record", None)
        monkeypatch.setattr(updates, "_inside_flatpak", lambda flatpak=flatpak: flatpak)
        monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release())))
        record, _notes = asyncio.run(updates.refresh())
        assert record["update_available"] is True and record["installer"] is None


@pytest.mark.parametrize("platform,machine", [
    ("darwin", "x86_64"), ("win32", "ARM64"), ("linux", "aarch64"),
], ids=["intel-mac", "windows-on-arm", "linux-on-arm"])
def test_another_architecture_gets_the_notice_only(monkeypatch, platform, machine):
    _desktop(monkeypatch, platform, machine=machine)
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release())))
    record, _notes = asyncio.run(updates.refresh())
    assert record["update_available"] is True and record["installer"] is None


ESCAPES = [
    f"{BASE}/download/../../../../evil/repo/releases/download/v1.0.4/DownloadPulse-1.0.4-windows-x64.exe",
    f"{BASE}/download/v1.0.4/./DownloadPulse-1.0.4-windows-x64.exe",
    f"{BASE}/download/v1.0.4//DownloadPulse-1.0.4-windows-x64.exe",
    f"{BASE}/download/%2e%2e/%2e%2e/v1.0.4/DownloadPulse-1.0.4-windows-x64.exe",
    f"{BASE}/download/v1.0.4%2FDownloadPulse-1.0.4-windows-x64.exe",
    f"{BASE}/download/..\\..\\v1.0.4/DownloadPulse-1.0.4-windows-x64.exe",
]


@pytest.mark.parametrize("url", ESCAPES, ids=["dot-dot", "dot", "empty-segment", "encoded-dots", "encoded-slash",
                                              "backslash"])
def test_an_installer_address_that_climbs_out_of_the_release_downloads_is_refused(monkeypatch, url):
    _desktop(monkeypatch, "win32")
    asset = _asset("DownloadPulse-1.0.4-windows-x64.exe", url=url)
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release(assets=[asset]))))
    assert asyncio.run(updates.refresh())[0]["installer"] is None


@pytest.mark.parametrize("page", [
    f"{BASE}/../../evil/releases/tag/v1.0.4", f"{BASE}/tag/./v1.0.4", f"{BASE}/tag/%2e%2e/v1.0.4",
    f"{BASE}//tag/v1.0.4",
])
def test_a_release_page_that_climbs_out_is_not_linked(monkeypatch, page):
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release(html_url=page))))
    assert asyncio.run(updates.refresh())[0]["release_url"] is None


@pytest.mark.parametrize("asset", [
    _asset("DownloadPulse-1.0.4-windows-x64.exe", digest=None),                        # no digest: no SHA256SUMS fallback
    _asset("DownloadPulse-1.0.4-windows-x64.exe", digest="sha1:" + "ab" * 20),
    _asset("DownloadPulse-1.0.4-windows-x64.exe", url="https://example.com/DownloadPulse-1.0.4-windows-x64.exe"),
    _asset("DownloadPulse-1.0.4-windows-x64.exe",
           url="https://github.com/someone/else/releases/download/v1.0.4/DownloadPulse-1.0.4-windows-x64.exe"),
    _asset("DownloadPulse-1.0.4-windows-x64.exe",
           url="https://github.com:8443/Xipher-Zero/downloadpulse/releases/download/v1.0.4/"
               "DownloadPulse-1.0.4-windows-x64.exe"),
    _asset("DownloadPulse-1.0.4-windows-x64.exe", url=f"{BASE}/download/v1.0.4/other.exe"),
    _asset("DownloadPulse-1.0.4-windows-x64.exe", size=0),
], ids=["no-digest", "not-sha256", "foreign-host", "foreign-repository", "port", "other-name", "no-size"])
def test_no_stated_hash_or_a_foreign_address_is_no_installer(monkeypatch, asset):
    _desktop(monkeypatch, "win32")
    release = _release(assets=[asset, _asset("SHA256SUMS")])
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", release)))
    record, _notes = asyncio.run(updates.refresh())
    assert record["update_available"] is True and record["installer"] is None


def test_a_release_page_outside_this_repositorys_releases_is_not_linked(monkeypatch):
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release(html_url="https://evil.example/releases/"))))
    assert asyncio.run(updates.refresh())[0]["release_url"] is None


@pytest.mark.parametrize("field", ["draft", "prerelease"])
def test_a_draft_or_pre_release_is_never_the_latest(monkeypatch, fresh, field):
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release(**{field: True}))))
    assert asyncio.run(updates.refresh()) is None
    assert updates.record() is None and fresh == []


def test_a_release_candidate_is_older_than_its_release(monkeypatch):
    monkeypatch.setattr(updates, "read_version", lambda: "1.0.3-rc1")
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release(tag="v1.0.3"))))
    assert asyncio.run(updates.refresh())[0]["update_available"] is True
    monkeypatch.setattr(updates, "read_version", lambda: "1.0.3")
    assert asyncio.run(updates.refresh())[0]["update_available"] is False


@pytest.mark.parametrize("failure", [
    OSError(SECRET), RuntimeError(SECRET), (403, "", None), (429, "", None), (500, "", None), (200, "", ["x"]),
], ids=["network", "exception", "403", "429", "500", "not-an-object"])
def test_a_failed_check_keeps_the_last_good_record_and_publishes_nothing(monkeypatch, fresh, caplog, failure):
    github = GitHub((200, '"e1"', _release()), failure)
    monkeypatch.setattr(updates, "_fetch", github)
    good, _notes = asyncio.run(updates.refresh())
    fresh.clear()
    with caplog.at_level(logging.WARNING, logger="downloadpulse.updates"):
        assert asyncio.run(updates.refresh()) is None
    assert updates.record() == good and fresh == []
    assert "Update check failed" in caplog.text                      # the reason is logged ...
    assert "secret.db" not in repr(updates.record())                  # ... and never kept in the record
    assert github.asked == ["", '"e1"']                                # the second check is conditional


def test_not_modified_keeps_the_record_and_publishes_nothing(monkeypatch, fresh):
    github = GitHub((200, '"e1"', _release()), (304, '"e1"', None))
    monkeypatch.setattr(updates, "_fetch", github)
    good, _notes = asyncio.run(updates.refresh())
    fresh.clear()
    again, _notes = asyncio.run(updates.refresh())
    assert {k: v for k, v in again.items() if k != "checked_at"} == {k: v for k, v in good.items() if k != "checked_at"}
    assert fresh == [] and github.asked == ["", '"e1"']


def test_an_unchanged_record_is_not_announced_again(monkeypatch, fresh):
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release())))
    asyncio.run(updates.refresh())
    asyncio.run(updates.refresh())
    assert fresh == [("update_status", {})]


def test_the_metalink_carries_the_records_name_size_hash_and_one_address():
    from providers.multimeta.metalink import parse
    installer = {"name": "DownloadPulse-1.0.4-windows-x64.exe", "size": 1234, "sha256": DIGEST,
                 "url": f"{BASE}/download/v1.0.4/DownloadPulse-1.0.4-windows-x64.exe"}
    (described,) = parse(updates.metalink(installer), base=None)
    assert (described.path, described.size, tuple(described.hashes), tuple(described.sources)) == (
        installer["name"], 1234, (("sha256", DIGEST),), (installer["url"],))


# ── the one loop ────────────────────────────────────────────────────────────

class _Stop(BaseException):
    pass


def _run_loop(monkeypatch, github, *, ticks, interval=12):
    from core import scheduler
    sleeps, sent = [], []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > ticks:
            raise _Stop()

    class Client:
        async def send_update(self, **fields):
            sent.append(fields)

    monkeypatch.setattr(scheduler.asyncio, "sleep", sleep)
    monkeypatch.setattr(scheduler, "NotificationService", lambda: SimpleNamespace(client=lambda: Client()))
    monkeypatch.setattr(config, "_settings", config.AppSettings(update_check_interval_hours=interval))
    monkeypatch.setattr(updates, "_fetch", github)
    with pytest.raises(_Stop):
        asyncio.run(scheduler.update_check_loop())
    return sleeps, sent


def test_the_loop_checks_after_a_minute_then_on_the_interval_and_notifies_once_per_version(monkeypatch):
    github = GitHub((200, "", _release()), (200, "", _release()), (200, "", _release(tag="v1.0.5")))
    sleeps, sent = _run_loop(monkeypatch, github, ticks=3)
    assert updates.FIRST_CHECK_SECONDS == 60
    assert sleeps == [60, 12 * 3600, 12 * 3600, 12 * 3600]
    assert len(github.asked) == 3                                      # one fetch per tick
    assert [item["latest_version"] for item in sent] == ["1.0.4", "1.0.5"]
    assert sent[0] == {"current_version": "1.0.3", "latest_version": "1.0.4",
                       "release_url": f"{BASE}/tag/v1.0.4", "release_notes": "Notes"}


def test_a_failed_check_is_tried_again_in_an_hour(monkeypatch):
    sleeps, sent = _run_loop(monkeypatch, GitHub(OSError("down"), (200, "", _release())), ticks=2, interval=24)
    assert sleeps == [60, 3600, 24 * 3600] and len(sent) == 1


def test_checks_off_fetch_nothing(monkeypatch):
    github = GitHub((200, "", _release()))
    sleeps, sent = _run_loop(monkeypatch, github, ticks=2, interval=0)
    assert sleeps == [60, 3600, 3600] and github.asked == [] and sent == []


# ── the routes ──────────────────────────────────────────────────────────────

def test_the_read_is_an_admin_action_and_so_is_the_download():
    assert ACTIONS["api.routes.version_check"] is Capability.ADMINISTER
    assert ACTIONS["api.routes.desktop_update_download"] is Capability.ADMINISTER


def test_the_read_never_contacts_github_and_is_unchecked_before_the_first_check(monkeypatch):
    import aiohttp
    from api import routes

    class Refused:
        def __init__(self, *args, **kwargs):
            raise AssertionError("the read made a network request")

    monkeypatch.setattr(aiohttp, "ClientSession", Refused)
    monkeypatch.setattr(updates, "_fetch", GitHub(AssertionError("the read fetched")))
    assert asyncio.run(routes.version_check(SimpleNamespace(state=SimpleNamespace(principal=None)))) == {
        "checked": False}


WINDOW_PORT = 47813


@pytest_asyncio.fixture
async def desktop(tmp_path, monkeypatch):
    """The real application and middleware, as the desktop supervisor starts
    it (Linux, inside Flatpak), with the default Admin and a User signed in."""
    import main
    from application.service import ApplicationService
    from transfers.engine import TransferEngine
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository
    from transfers.registry import IntegrationRegistry
    from transfers.staged_input import StagedInputStore

    _desktop(monkeypatch, "linux")
    monkeypatch.setenv("DOWNLOADPULSE_DESKTOP_PORT", str(WINDOW_PORT))
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "updates.db")
    await database.init_db()
    monkeypatch.setattr(config, "_settings", config.AppSettings(auth_password_enabled=True))
    root = tmp_path / "downloads"
    root.mkdir()
    repository = TransferRepository()
    engine = TransferEngine(repository, IntegrationRegistry(), download_root=str(root),
                            policy=TransferPolicy(retry_delay=0), clock=lambda: 1000.0)
    await engine.initialize()
    application = ApplicationService(engine)
    application.staged_input = StagedInputStore(str(tmp_path / "staged"))
    monkeypatch.setitem(main.app.state._state, "application", application)
    session_store.clear()
    sessions = {}
    for name, role, ident in (("admin", Role.ADMIN, DEFAULT_ADMIN_ID), ("alice", Role.USER, "alice-id")):
        token, _ = session_store.create(
            Principal.password_session(account(role.value, account_id=ident, role=role)), lifetime_seconds=3600)
        sessions[name] = token
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)

    async def call(who, method, path, headers=None):
        token = sessions[who]
        async with httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{WINDOW_PORT}") as client:
            response = await client.request(method, path, headers={
                "Cookie": f"{HTTP_SESSION_COOKIE}={token}", "X-CSRF-Token": session_store.csrf_token(token),
                **(headers or {})})
        return response.status_code, response.json()

    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release())))
    await updates.refresh()
    yield SimpleNamespace(root=root, repository=repository, application=application, call=call)
    session_store.clear()


@pytest.mark.asyncio
async def test_an_admins_page_reads_the_record_without_the_installers_address_or_hash(desktop):
    status, body = await desktop.call("admin", "GET", "/api/version/check")
    assert status == 200
    assert body["latest"] == "1.0.4" and body["update_available"] is True
    assert body["installer"] == {"name": "DownloadPulse-1.0.4-linux-x86_64.flatpak", "size": 1234}
    assert body["download_offered"] is True
    assert DIGEST not in str(body) and "/download/" not in str(body)
    # Not through Local Port (any forwarding header is never the window's).
    status, body = await desktop.call("admin", "GET", "/api/version/check", {"X-Forwarded-For": "10.0.0.2"})
    assert status == 200 and body["download_offered"] is False


@pytest.mark.asyncio
async def test_a_user_gets_no_record(desktop, monkeypatch):
    # A server (Server deployment): an ordinary User is refused the record, an Admin reads it.
    monkeypatch.delenv("DOWNLOADPULSE_DESKTOP")
    monkeypatch.setattr(config, "_settings", config.AppSettings(auth_password_enabled=True))
    assert config.get_settings().deployment_type == "server"
    status, _body = await desktop.call("alice", "GET", "/api/version/check")
    assert status == 403
    status, body = await desktop.call("admin", "GET", "/api/version/check")
    assert status == 200 and body["installer"] is None and body["download_offered"] is False


@pytest.mark.asyncio
async def test_the_download_is_the_desktop_windows_alone(desktop, monkeypatch):
    status, body = await desktop.call("admin", "POST", "/api/desktop/update/download",
                                      {"X-Forwarded-For": "10.0.0.2"})
    assert status == 403 and body["code"] == "desktop.update_window_only"
    monkeypatch.delenv("DOWNLOADPULSE_DESKTOP")
    status, body = await desktop.call("admin", "POST", "/api/desktop/update/download")
    assert status == 404 and body["code"] == "desktop.update_only"
    async with database.get_db() as db:
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM torrents"))["n"] == 0


@pytest.mark.asyncio
async def test_no_update_no_download(desktop, monkeypatch):
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release(tag="v1.0.3"))))
    await updates.refresh()
    status, body = await desktop.call("admin", "POST", "/api/desktop/update/download")
    assert status == 409 and body["code"] == "desktop.update_unavailable"


async def _submitted_document(desktop, transfer_id) -> bytes:
    from transfers.staged_input import StagedPayload
    (record,) = [item for item in await desktop.repository.requests(transfer_id) if item.parent_id is None]
    assert record.request.kind == "meta4"
    payload = record.request.payload
    assert isinstance(payload, StagedPayload)
    return desktop.application.staged_input.read(payload)


@pytest.mark.asyncio
async def test_the_download_submits_the_records_metalink_and_a_second_click_is_the_same_transfer(desktop):
    from providers.multimeta.metalink import parse
    status, body = await desktop.call("admin", "POST", "/api/desktop/update/download")
    assert status == 200, body
    transfer_id = body["id"]
    (described,) = parse(await _submitted_document(desktop, transfer_id), base=None)
    installer = updates.installer()
    assert tuple(described.hashes) == (("sha256", installer["sha256"]),) == (("sha256", DIGEST),)
    assert tuple(described.sources) == (installer["url"],) and described.size == installer["size"]
    # The submission is the local Admin's.
    async with database.get_db() as db:
        demands = await db.fetchall("SELECT account_id, origin FROM transfer_demands WHERE transfer_id=?",
                                    (transfer_id,))
    assert [(row["account_id"], row["origin"]) for row in demands] == [(DEFAULT_ADMIN_ID, "submission")]
    # A second click while the first still serves: the same transfer, nothing new admitted.
    # (Completed, removed, deleted and failed downloads, reached through public paths over
    # the real engine: test_v103_update_download_integrity.py.)
    status, again = await desktop.call("admin", "POST", "/api/desktop/update/download")
    assert status == 200 and again["id"] == transfer_id


# ── boundaries ──────────────────────────────────────────────────────────────

def test_the_release_owner_imports_neither_core_nor_the_database():
    """Core never learns what an update is, and the release owner reads no
    core table: by what ``application/updates.py`` imports, not its words."""
    import ast
    tree = ast.parse((Path(__file__).resolve().parents[1] / "application" / "updates.py").read_text())
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not {name for name in imported
                if name.split(".")[0] in {"transfers", "db", "aiosqlite", "sqlite3", "application"}
                and name != "application"}, imported


def test_the_old_fetcher_and_its_cache_are_gone():
    routes = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text()
    assert "_update_check_cache" not in routes and "_version_gt" not in routes
    assert "releases/latest" not in routes
    scheduler = (Path(__file__).resolve().parents[1] / "core" / "scheduler.py").read_text()
    assert "releases/latest" not in scheduler and "aiohttp" not in scheduler.split(
        "async def update_check_loop", 1)[1].split("async def event_index_loop", 1)[0]
