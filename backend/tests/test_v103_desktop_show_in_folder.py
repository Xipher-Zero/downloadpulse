"""Show in folder (1.0.3 WS12): a completed download, in the desktop's own file manager.

* Desktop only: a server refuses it (``desktop.reveal_only``); so does a page
  that reached the backend through Local Port, not the window's own address.
* The page names a transfer, never a path (an extra field is refused): the
  backend resolves where it is from core's records alone
  (``ApplicationService.completed_location``) -- only a completed transfer's
  completed artifacts, inside the download root; a link or a file outside it
  is no location.
* It is an operate action, and the desktop (always Standalone) admits its
  one person, the default Admin, alone.
* The supervisor receives it once, on its next ``show``.
* The supervisor opens it with a fixed argument list per platform, never a
  shell; on Linux a file goes to the OpenURI portal first.
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
from application import desktop_control
from auth.accounts import DEFAULT_ADMIN_ID, Role
from auth.models import Principal
from auth.permissions import ACTIONS, Capability
from auth.sessions import HTTP_SESSION_COOKIE, session_store
from transfers.models import SubmissionDemand, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop import supervisor  # noqa: E402

WINDOW_PORT = 47811
LOCAL_PORT = 47812


@pytest_asyncio.fixture
async def desktop(tmp_path, monkeypatch):
    """The real application object and middleware, as the desktop supervisor
    starts it, with the default Admin, a User and a ReadOnly account signed in."""
    import main
    from application.service import ApplicationService
    from transfers.engine import TransferEngine

    monkeypatch.setenv("DOWNLOADPULSE_DESKTOP", "1")
    monkeypatch.setenv("DOWNLOADPULSE_DESKTOP_PORT", str(WINDOW_PORT))
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reveal.db")
    await database.init_db()
    monkeypatch.setattr(config, "_settings", config.AppSettings(auth_password_enabled=True))
    root = tmp_path / "downloads"
    root.mkdir()
    repository = TransferRepository()
    engine = TransferEngine(repository, IntegrationRegistry(), download_root=str(root),
                            policy=TransferPolicy(retry_delay=0), clock=lambda: 1000.0)
    await engine.initialize()
    application = ApplicationService(engine)
    monkeypatch.setitem(main.app.state._state, "application", application)
    monkeypatch.setattr(desktop_control, "_reveal", None)
    session_store.clear()
    sessions = {}
    for name, role, ident in (("admin", Role.ADMIN, DEFAULT_ADMIN_ID), ("alice", Role.USER, "alice-id"),
                              ("reader", Role.READONLY, "reader-id")):
        token, _ = session_store.create(
            Principal.password_session(account(role.value, account_id=ident, role=role)), lifetime_seconds=3600)
        sessions[name] = token
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)

    async def reveal(who, body, *, port=WINDOW_PORT):
        token = sessions[who]
        async with httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{port}") as client:
            response = await client.post("/api/desktop/reveal", json=body, headers={
                "Cookie": f"{HTTP_SESSION_COOKIE}={token}", "X-CSRF-Token": session_store.csrf_token(token)})
        return response.status_code, response.json()

    yield SimpleNamespace(root=root, repository=repository, application=application, reveal=reveal)
    session_store.clear()


async def _download(repository, account_id, files, *, status="completed", collection=None, payload="a"):
    """A transfer ``account_id`` requested, with one completed artifact at each of ``files``."""
    transfer, _ = await repository.admit(
        (TransferRequest("magnet", f"magnet:?xt=urn:btih:{payload * 40}", name="parcel", fingerprint=payload * 40),),
        name="parcel", demand=SubmissionDemand(account_id))
    async with database.get_db() as db:
        root = await db.fetchone("SELECT id, payload FROM transfer_requests WHERE transfer_id=?", (transfer.id,))
        await db.execute("UPDATE torrents SET status=?, collection_root=? WHERE id=?", (status, collection, transfer.id))
        for ordinal, path in enumerate(files):
            request = root["id"]
            if ordinal:                              # each further file is a member of the root request
                request = f"{root['id']}-{ordinal}"
                await db.execute("INSERT INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload,state) "
                                 "VALUES(?,?,?,?,?,'resolved')", (request, transfer.id, root["id"], ordinal, root["payload"]))
            await db.execute("INSERT INTO download_files(torrent_id,request_id,filename,local_path,status,size_bytes) "
                             "VALUES(?,?,?,?,'completed',1)", (transfer.id, request, Path(path).name, str(path)))
        await db.commit()
    return transfer.id


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


async def _handed() -> dict | None:
    return (await desktop_control.answer(None, {"id": 1, "op": "show"}))["reveal"]


def test_show_in_folder_is_an_operate_action():
    assert ACTIONS["api.routes.desktop_reveal"] is Capability.TRANSFERS_OPERATE


@pytest.mark.asyncio
async def test_a_completed_file_is_resolved_by_the_backend_and_handed_over_once(desktop):
    film = _file(desktop.root / "film.mkv")
    transfer = await _download(desktop.repository, "alice-id", [film])
    assert await _handed() is None
    status, body = await desktop.reveal("admin", {"transfer_id": transfer})
    assert (status, body) == (200, {"revealing": True})
    assert await _handed() == {"path": str(film.resolve()), "folder": False}
    assert await _handed() is None                                   # single use
    # The page names a transfer, never a path.
    status, _ = await desktop.reveal("admin", {"transfer_id": transfer, "path": "/etc"})
    assert status == 422 and await _handed() is None


@pytest.mark.asyncio
async def test_a_download_of_several_files_opens_its_folder(desktop):
    season = desktop.root / "Season 1"
    first, second = _file(season / "e1.mkv"), _file(season / "sub" / "e2.mkv")
    collected = await _download(desktop.repository, "alice-id", [first, second], collection="Season 1")
    assert (await desktop.reveal("admin", {"transfer_id": collected}))[0] == 200
    assert await _handed() == {"path": str(season.resolve()), "folder": True}
    # No collection folder: the folder its files share.
    loose = desktop.root / "loose"
    shared = await _download(desktop.repository, "alice-id", [_file(loose / "a.bin"), _file(loose / "b.bin")],
                             payload="b")
    assert (await desktop.reveal("admin", {"transfer_id": shared}))[0] == 200
    assert await _handed() == {"path": str(loose.resolve()), "folder": True}


@pytest.mark.asyncio
async def test_only_a_completed_download_inside_the_download_root_is_shown(desktop, tmp_path):
    unfinished = await _download(desktop.repository, "alice-id", [_file(desktop.root / "part.bin")],
                                 status="downloading")
    status, body = await desktop.reveal("admin", {"transfer_id": unfinished})
    assert status == 409 and body["code"] == "desktop.reveal_not_completed"
    outside = await _download(desktop.repository, "alice-id", [_file(tmp_path / "elsewhere" / "x.bin")], payload="c")
    status, body = await desktop.reveal("admin", {"transfer_id": outside})
    assert status == 409 and body["code"] == "desktop.reveal_missing"
    link = desktop.root / "link.bin"
    link.symlink_to(_file(tmp_path / "secret" / "y.bin"))
    linked = await _download(desktop.repository, "alice-id", [link], payload="d")
    status, body = await desktop.reveal("admin", {"transfer_id": linked})
    assert status == 409 and body["code"] == "desktop.reveal_missing"
    climbing = await _download(desktop.repository, "alice-id", [desktop.root / ".." / "elsewhere" / "x.bin"],
                               payload="e")
    status, body = await desktop.reveal("admin", {"transfer_id": climbing})
    assert status == 409 and body["code"] == "desktop.reveal_missing"
    gone = await _download(desktop.repository, "alice-id", [desktop.root / "deleted.bin"], payload="f")
    status, body = await desktop.reveal("admin", {"transfer_id": gone})
    assert status == 409 and body["code"] == "desktop.reveal_missing"
    status, body = await desktop.reveal("admin", {"transfer_id": 999})
    assert status == 404 and body["code"] == "transfer.not_found"
    assert await _handed() is None


@pytest.mark.asyncio
async def test_the_desktop_admits_only_its_one_person(desktop):
    transfer = await _download(desktop.repository, "alice-id", [_file(desktop.root / "mine.bin")])
    # The desktop application is Standalone: it admits the default Admin's sessions alone.
    assert (await desktop.reveal("alice", {"transfer_id": transfer}))[0] == 401
    assert (await desktop.reveal("reader", {"transfer_id": transfer}))[0] == 401
    assert await _handed() is None


@pytest.mark.asyncio
async def test_only_the_windows_own_page_may_ask_and_a_server_never(desktop, monkeypatch):
    transfer = await _download(desktop.repository, "alice-id", [_file(desktop.root / "film.mkv")])
    status, body = await desktop.reveal("admin", {"transfer_id": transfer}, port=LOCAL_PORT)    # through Local Port
    assert status == 403 and body["code"] == "desktop.reveal_window_only"
    monkeypatch.delenv("DOWNLOADPULSE_DESKTOP")
    status, body = await desktop.reveal("admin", {"transfer_id": transfer})
    assert status == 404 and body["code"] == "desktop.reveal_only"
    assert await _handed() is None


# ── the supervisor ──────────────────────────────────────────────────────────

def test_each_platform_opens_the_file_manager_with_a_fixed_command():
    environ = {"SystemRoot": r"C:\Windows"}
    # Windows: one literal command line -- /select, then the quoted path (Explorer ignores a quoted switch).
    assert supervisor.reveal_command(r"C:\D\My Film\f.mkv", False, "win32", environ) == \
        r'"C:\Windows\explorer.exe" /select,"C:\D\My Film\f.mkv"'
    assert supervisor.reveal_command(r"C:\D\Film, Part 1.mkv", False, "win32", environ) == \
        r'"C:\Windows\explorer.exe" /select,"C:\D\Film, Part 1.mkv"'
    assert supervisor.reveal_command(r"C:\D\Season 1, Disc 2", True, "win32", environ) == \
        r'"C:\Windows\explorer.exe" "C:\D\Season 1, Disc 2"'
    # macOS: revealed, never opened -- a folder may be an application bundle.
    assert supervisor.reveal_command("/D/film.mkv", False, "darwin") == ["/usr/bin/open", "-R", "/D/film.mkv"]
    assert supervisor.reveal_command("/D/Tool.app", True, "darwin") == ["/usr/bin/open", "-R", "/D/Tool.app"]
    assert supervisor.reveal_command("/D/film.mkv", False, "linux") == ["xdg-open", "/D"]
    assert supervisor.reveal_command("/D/Season 1", True, "linux") == ["xdg-open", "/D/Season 1"]


@pytest.mark.asyncio
async def test_windows_starts_the_literal_command_line_and_refuses_a_quote(monkeypatch):
    started = []
    monkeypatch.setattr(supervisor, "_start_windows", started.append)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    assert await supervisor.show_in_folder({"path": r"C:\D\My Film\f.mkv", "folder": False}, "win32") is True
    assert started == [r'"C:\Windows\explorer.exe" /select,"C:\D\My Film\f.mkv"']
    assert await supervisor.show_in_folder({"path": 'C:\\D\\a" b', "folder": False}, "win32") is False
    assert len(started) == 1


class _Process:
    async def wait(self):
        return 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("platform", "portal", "reveal", "argv"), [
    ("darwin", None, {"path": "/D/film.mkv", "folder": False}, ["/usr/bin/open", "-R", "/D/film.mkv"]),
    ("darwin", None, {"path": "/D/Tool.app", "folder": True}, ["/usr/bin/open", "-R", "/D/Tool.app"]),
    ("linux", True, {"path": "/D/film.mkv", "folder": False}, None),                  # the portal took it
    ("linux", False, {"path": "/D/film.mkv", "folder": False}, ["xdg-open", "/D"]),    # no portal: its folder
    ("linux", None, {"path": "/D/Season 1", "folder": True}, ["xdg-open", "/D/Season 1"]),
])
async def test_the_supervisor_runs_the_command_itself_without_a_shell(monkeypatch, platform, portal, reveal, argv):
    started, asked = [], []
    monkeypatch.setattr(supervisor.os.path, "isdir", lambda path: True)

    async def exec_(*args, **kwargs):
        started.append((list(args), kwargs))
        return _Process()

    def open_directory(path):
        asked.append(path)
        return portal

    monkeypatch.setattr(supervisor.asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(supervisor, "portal_open_directory", open_directory)
    assert await supervisor.show_in_folder(reveal, platform) is True
    assert asked == ([reveal["path"]] if portal is not None else [])
    assert [args for args, _ in started] == ([argv] if argv else [])
    assert all("shell" not in kwargs for _, kwargs in started)


@pytest.mark.asyncio
@pytest.mark.parametrize("reveal", [None, "/D/film.mkv", {"path": "D/film.mkv", "folder": False},
                                    {"path": "/D/film.mkv", "folder": "no"}, {"path": "/D/\0x", "folder": True}])
async def test_the_supervisor_refuses_anything_but_an_absolute_path(monkeypatch, reveal):
    async def exec_(*args, **kwargs):
        raise AssertionError("nothing may start")

    monkeypatch.setattr(supervisor.asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(supervisor, "portal_open_directory", lambda path: pytest.fail("no portal call"))
    assert await supervisor.show_in_folder(reveal, "linux") is False


@pytest.mark.asyncio
async def test_linux_opens_no_folder_that_is_not_there(monkeypatch, tmp_path):
    async def exec_(*args, **kwargs):
        raise AssertionError("nothing may start")

    monkeypatch.setattr(supervisor.asyncio, "create_subprocess_exec", exec_)
    assert await supervisor.show_in_folder({"path": str(tmp_path / "gone"), "folder": True}, "linux") is False


async def _serve_one_reveal(monkeypatch, show):
    """``_serve`` for three looks, its first carrying one Show in folder."""
    class Channel:
        def __init__(self):
            self.answered = False

        async def ask(self, op, **fields):
            if op == "show":
                reveal, self.answered = (None if self.answered else {"path": "/D/film.mkv", "folder": False}), True
                return {"show": False, "quit": False, "reveal": reveal}
            return {"events": {"cursor": 0, "events": []}, "status": {"status": {"active": 0, "queued": 0}},
                    "locale": {"locale": "en"}, "frame": {"frame": None}, "saves": {"saves": []}}[op]

    class Shell:
        def __init__(self):
            self.events = asyncio.Queue()

        async def call(self, method, data=None):
            pass

    class Native:
        tray_shown = True

        async def tray(self, menu):
            pass

        async def notice(self, title, body):
            pass

    class Words:
        def __call__(self, key, **params):
            return key

        def choose(self, tag):
            return False

    class Running:
        class process:
            returncode = None

    monkeypatch.setattr(supervisor, "show_in_folder", show)
    task = asyncio.ensure_future(supervisor._serve(Shell(), Channel(), Words(), Running(), Native()))
    try:
        await asyncio.sleep(supervisor.SHOW_POLL_SECONDS * 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_the_supervisor_shows_what_its_next_look_carries(monkeypatch):
    shown = []

    async def show(reveal, platform=sys.platform):
        shown.append(reveal)
        return True

    await _serve_one_reveal(monkeypatch, show)
    assert shown == [{"path": "/D/film.mkv", "folder": False}]


@pytest.mark.asyncio
async def test_a_show_in_folder_that_fails_is_logged_by_the_loop_that_started_it(monkeypatch, caplog):
    async def show(reveal, platform=sys.platform):
        raise RuntimeError("the file manager went away")

    with caplog.at_level(logging.ERROR, logger="downloadpulse.desktop.supervisor"):
        await _serve_one_reveal(monkeypatch, show)
    failed = [record for record in caplog.records if record.getMessage() == "Show in folder failed"]
    assert len(failed) == 1 and isinstance(failed[0].exc_info[1], RuntimeError)
    # The loop holds each task it starts until it ends (never an unreferenced future).
    source = (ROOT / "desktop" / "supervisor.py").read_text(encoding="utf-8")
    serve = source[source.index("async def _serve("):source.index("async def apply_frame(")]
    assert "asyncio.ensure_future(show_in_folder" not in serve and "asyncio.ensure_future(_save" not in serve
    assert "running.add(task)" in serve and "running.discard(done)" in serve


def test_nothing_reaches_the_file_manager_through_a_shell():
    source = (ROOT / "desktop" / "supervisor.py").read_text(encoding="utf-8")
    start = source.index("def reveal_command")
    reveal = source[start:source.index("async def _save(")]
    assert "shell=True" not in reveal and "create_subprocess_shell" not in reveal and "os.system" not in reveal
    assert "cmd.exe" not in reveal and "/bin/sh" not in reveal
