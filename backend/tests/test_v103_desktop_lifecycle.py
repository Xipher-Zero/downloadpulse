"""The desktop shell's lifecycle (WS6).

* Close with no tray (Linux, a desktop without one): the window stays up and
  its own page -- never a browser tab or a Local Port client -- asks whether
  to quit, with a single-use nonce; Quit comes back with the supervisor's next
  ``show``; with no page to ask, the window minimizes; with a tray it hides.
* The tray's absence is logged once, never retried on every poll.
* The logs are size-rotated; both drains of a child share one writer.
* SIGTERM, SIGINT and SIGHUP quit in order, by phase; a shell that ends during
  start fails it at once; a failed start is logged with its cause and said in
  a notice.

The loop and the bus are driven with stand-ins (no GUI); the endpoint against
a real backend started as the supervisor starts it; the signals and the start
failure against the real supervisor process with a stand-in native shell.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import aiohttp
import pytest

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(ROOT))

from desktop import supervisor  # noqa: E402
from desktop.supervisor import free_port  # noqa: E402
from test_v114_desktop_control import Backend  # noqa: E402
from test_v114_desktop_local_port import _port_backend  # noqa: E402

LINUX = pytest.mark.only_on(sys.platform.startswith("linux"), reason="the no-tray close is Linux's")


def _jeepney() -> bool:
    try:
        import jeepney  # noqa: F401
    except ImportError:
        return False
    return True


# The Linux supervisor's session bus client (the desktop runtime's jeepney).
LINUX_JEEPNEY = pytest.mark.only_on(sys.platform.startswith("linux") and _jeepney(),
                                    reason="Linux with the desktop runtime's jeepney")


# ── stand-ins for the loop ─────────────────────────────────────────────────

class Channel:
    """The backend's control channel as the loop uses it: answers recorded."""

    def __init__(self, prompted: bool = True):
        self.asked, self.prompted, self.quit = [], prompted, False

    async def ask(self, op, **fields):
        self.asked.append(op)
        if op == "show":
            confirmed, self.quit = self.quit, False
            return {"show": False, "quit": confirmed}
        return {"events": {"cursor": 0, "events": []}, "status": {"status": {"active": 0, "queued": 0}},
                "locale": {"locale": "en"}, "quit_prompt": {"prompted": self.prompted},
                "frame": {"frame": None}, "saves": {"saves": []}}[op]


class Shell:
    def __init__(self):
        self.events, self.calls = asyncio.Queue(), []

    async def call(self, method, data=None):
        self.calls.append(method)


class Native:
    def __init__(self, tray_shown: bool):
        self.tray_shown = tray_shown

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


async def _until(predicate, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return predicate()


def _loop(shell, channel, native):
    return asyncio.ensure_future(supervisor._serve(shell, channel, Words(), Running(), native))


async def _stop(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@LINUX
@pytest.mark.asyncio
async def test_closing_with_no_tray_keeps_the_window_up_asks_its_page_and_quits_on_the_answer():
    shell, channel, native = Shell(), Channel(prompted=True), Native(tray_shown=False)
    task = _loop(shell, channel, native)
    try:
        await shell.events.put(("windowClose", None))
        assert await _until(lambda: "quit_prompt" in channel.asked)
        await asyncio.sleep(supervisor.SHOW_POLL_SECONDS * 2)
        # The page asks in the window: it is neither hidden nor minimized, and nothing quits yet.
        assert "window.minimize" not in shell.calls and "window.hide" not in shell.calls and not task.done()
        channel.quit = True                                 # the person confirmed in the window
        assert await asyncio.wait_for(task, supervisor.SHOW_POLL_SECONDS * 4) == 0
    finally:
        await _stop(task)


@LINUX
@pytest.mark.asyncio
async def test_closing_with_no_tray_and_no_page_to_ask_minimizes_and_keeps_running():
    shell, channel, native = Shell(), Channel(prompted=False), Native(tray_shown=False)
    task = _loop(shell, channel, native)
    try:
        await shell.events.put(("windowClose", None))
        assert await _until(lambda: "window.minimize" in shell.calls)
        assert channel.asked.count("quit_prompt") == 1 and "window.hide" not in shell.calls and not task.done()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_closing_with_a_tray_hides_and_asks_nothing():
    shell, channel, native = Shell(), Channel(), Native(tray_shown=True)
    task = _loop(shell, channel, native)
    try:
        await shell.events.put(("windowClose", None))
        assert await _until(lambda: "window.hide" in shell.calls)
        await asyncio.sleep(0.2)
        assert "quit_prompt" not in channel.asked and "window.minimize" not in shell.calls and not task.done()
    finally:
        await _stop(task)


@LINUX
@pytest.mark.asyncio
async def test_the_trays_availability_decides_each_close():
    shell, channel, native = Shell(), Channel(prompted=True), Native(tray_shown=True)
    window = lambda: [call for call in shell.calls if call.startswith("window.")]  # noqa: E731
    task = _loop(shell, channel, native)
    try:
        await shell.events.put(("windowClose", None))                         # tray: hidden
        assert await _until(lambda: window() == ["window.hide"])
        native.tray_shown = False                                              # the tray went away
        await shell.events.put(("trayAvailability", {"shown": False}))
        assert await _until(lambda: window() == ["window.hide", "window.show", "window.minimize"])
        await shell.events.put(("windowClose", None))                         # no tray: the page asks
        assert await _until(lambda: "quit_prompt" in channel.asked)
        native.tray_shown = True                                               # it came back
        await shell.events.put(("trayAvailability", {"shown": True}))
        await shell.events.put(("windowClose", None))
        assert await _until(lambda: window()[-1:] == ["window.hide"])
        assert channel.asked.count("quit_prompt") == 1 and not task.done()
    finally:
        await _stop(task)


# ── signals, by phase ──────────────────────────────────────────────────────

class _Task:
    def __init__(self):
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1


def _records(level=logging.INFO):
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record)
    supervisor.logger.addHandler(handler)
    supervisor.logger.setLevel(level)
    return records, handler


def test_a_signal_during_start_cancels_once_and_while_serving_asks_the_loop_and_never_interrupts_quitting():
    records, handler = _records()
    try:
        task = _Task()
        starting = supervisor.Signals(task)
        starting.signalled("SIGTERM")
        starting.signalled("SIGINT")                     # quitting: only logged
        assert task.cancelled == 1 and starting.received == "SIGTERM" and starting.served is False
        events = asyncio.Queue()
        serving = supervisor.Signals(task)
        serving.serving(events)
        serving.signalled("SIGHUP")
        serving.signalled("SIGTERM")
        assert task.cancelled == 1 and events.qsize() == 1
        assert events.get_nowait() == ("quitRequested", {"signal": "SIGHUP"})
        assert sum("while quitting" in record.getMessage() for record in records) == 2
    finally:
        supervisor.logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_quit_requested_by_a_signal_ends_the_loop_with_zero():
    shell, channel = Shell(), Channel()
    task = _loop(shell, channel, Native(tray_shown=True))
    await shell.events.put(("quitRequested", {"signal": "SIGTERM"}))
    assert await asyncio.wait_for(task, 5) == 0


# ── a shell that ends during start ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_shell_that_ended_fails_the_start_at_once_not_after_the_whole_wait(tmp_path):
    class Ended:
        class process:
            returncode = 7

    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"ended during start \(7\)"):
        await supervisor.Shell(session=None).connect(tmp_path / "auth_info.json",
                                                     time.monotonic() + supervisor.READY_SECONDS, Ended())
    assert time.monotonic() - started < 2


# ── the logs ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_both_drains_of_a_child_share_one_rotating_writer(tmp_path):
    log = supervisor.ChildLog(tmp_path / "logs" / "backend.log", max_bytes=100, backups=3)
    out, err = asyncio.StreamReader(), asyncio.StreamReader()
    drains = [asyncio.ensure_future(supervisor._drain(stream, log)) for stream in (out, err)]
    for index in range(12):
        (out if index % 2 else err).feed_data(f"{index:02d}".encode() * 20)   # 40 bytes each
        await asyncio.sleep(0.01)
    out.feed_eof()
    err.feed_eof()
    await asyncio.gather(*drains)
    log.close()
    names = sorted(path.name for path in (tmp_path / "logs").iterdir())
    assert names == ["backend.log", "backend.log.1", "backend.log.2", "backend.log.3"]     # the oldest dropped
    # Every file within its bound, and the newest output in the live file -- from either drain.
    assert all((tmp_path / "logs" / name).stat().st_size <= 100 for name in names)
    assert (tmp_path / "logs" / "backend.log").read_bytes() == b"10" * 20 + b"11" * 20
    assert (tmp_path / "logs" / "backend.log.1").read_bytes() == b"08" * 20 + b"09" * 20


@pytest.mark.asyncio
async def test_a_refused_rotation_never_stops_either_drain(tmp_path, monkeypatch, caplog):
    """On Windows another process holding the file refuses the rename: the
    drains go on writing (an unread output pipe would stall the child), the
    failure is said once, and the rename is not retried on every write."""
    log = supervisor.ChildLog(tmp_path / "logs" / "backend.log", max_bytes=100, backups=3)
    attempts = []

    def refused(*_args):
        attempts.append(1)
        raise PermissionError(13, "in use")

    monkeypatch.setattr(supervisor.os, "replace", refused)
    out, err = asyncio.StreamReader(), asyncio.StreamReader()
    drains = [asyncio.ensure_future(supervisor._drain(stream, log)) for stream in (out, err)]
    with caplog.at_level(logging.WARNING, logger="downloadpulse.desktop.supervisor"):
        for index in range(12):
            (out if index % 2 else err).feed_data(f"{index:02d}".encode() * 20)   # 40 bytes each
            await asyncio.sleep(0.01)
        out.feed_eof()
        err.feed_eof()
        await asyncio.gather(*drains)
    log.close()
    assert all(drain.exception() is None for drain in drains)
    written = (tmp_path / "logs" / "backend.log").read_bytes()
    assert written == b"".join(f"{index:02d}".encode() * 20 for index in range(12))   # nothing lost, in order
    assert len([r for r in caplog.records if "rotating backend.log failed" in r.getMessage()]) == 1
    # Once per further full bound (about every 100 bytes: ~7 of 480 bytes), not on each of the 11 writes past it.
    assert len(attempts) <= 8


def test_the_supervisors_own_log_rotates_too(tmp_path):
    handler = supervisor.diagnostics_handler(tmp_path)
    try:
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.maxBytes == supervisor.LOG_BYTES == 5 * 1024 * 1024
        assert handler.backupCount == supervisor.LOG_BACKUPS == 3
        assert Path(handler.baseFilename) == tmp_path / "logs" / "desktop.log"
    finally:
        handler.close()
    source = (ROOT / "desktop" / "supervisor.py").read_text(encoding="utf-8")
    assert "logging.FileHandler(" not in source
    # The Flatpak CI's readiness check still finds the backend's log where it was.
    assert 'ChildLog(root / "logs" / "backend.log")' in source
    assert 'grep -qs "Scheduler started" "$logs/backend.log"' in (
        ROOT / "packaging" / "desktop" / "flatpak" / "ci-build.sh").read_text(encoding="utf-8")


# ── the tray's absence: said once, never retried on every poll ─────────────

class _Reply:
    def __init__(self, kind: str, body=()):
        self.body = body
        self.header = type("Header", (), {"message_type": type("Kind", (), {"name": kind})(), "fields": {}})()


class _Router:
    """The session bus as SessionBus uses it: who owns the watcher's name,
    and the watcher's own answers once there is one."""

    def __init__(self):
        self.watcher, self.members = None, []

    async def send(self, message):
        pass

    async def send_and_get_reply(self, message):
        from jeepney.low_level import HeaderFields
        member = message.header.fields.get(HeaderFields.member)
        self.members.append(member)
        if member == "GetNameOwner":
            return _Reply("method_return", (self.watcher,)) if self.watcher else _Reply("error")
        if member == "RegisterStatusNotifierItem":
            return _Reply("method_return")
        if member == "Get":
            return _Reply("method_return", (("b", True),))
        raise AssertionError(member)


@LINUX_JEEPNEY
@pytest.mark.asyncio
async def test_no_tray_watcher_is_logged_once_and_no_poll_registers_or_logs_an_error():
    records, handler = _records()
    try:
        router, events = _Router(), asyncio.Queue()
        bus = supervisor.SessionBus(router, events, [])
        await bus._register()                                     # as at start: stock GNOME, no watcher
        menu = supervisor.tray_menu({"active": 0, "queued": 0}, Words(), "linux")
        for _ in range(20):                                       # the loop's polls, the tray not shown
            await bus.tray(menu)
        assert not bus.tray_shown and events.empty()
        assert "RegisterStatusNotifierItem" not in router.members and router.members.count("GetNameOwner") == 1
        assert not [record for record in records if record.levelno >= logging.WARNING]
        said = [record.getMessage() for record in records if record.getMessage().startswith("tray icon")]
        assert said == ["tray icon not shown: no tray host to open the window from"]
        router.watcher = ":1.42"                                  # a watcher appeared (its NameOwnerChanged)
        await bus._register()
        assert bus.tray_shown and events.get_nowait() == ("trayAvailability", {"shown": True})
        for _ in range(5):
            await bus.tray(menu)
        assert router.members.count("RegisterStatusNotifierItem") == 1
        assert [record.getMessage() for record in records if record.getMessage().startswith("tray icon")] == [
            "tray icon not shown: no tray host to open the window from", "tray icon shown"]
    finally:
        supervisor.logger.removeHandler(handler)


# ── the start failure's notice ─────────────────────────────────────────────

def test_a_failed_start_is_said_with_the_catalogs_words_and_macos_only_logs(tmp_path):
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    words = supervisor.Words(config)
    assert words("desktop.error.start_failed_title") == "DownloadPulse could not start"
    assert "logs/desktop.log" in words("desktop.error.start_failed")
    asyncio.run(supervisor.start_failure_notice(words, "darwin"))     # macOS: nothing to show it through
    asyncio.run(supervisor.start_failure_notice(None, "linux"))       # without its words: the log alone


@pytest.mark.only_on(sys.platform.startswith("linux") and _jeepney() and __import__("shutil").which("dbus-daemon")
                     is not None, reason="Linux with the desktop runtime's jeepney and a dbus-daemon")
def test_a_failed_start_on_linux_is_a_portal_notice_on_a_connection_of_its_own(tmp_path):
    from test_v114_desktop_supervisor import BareBus, Portal
    bus = BareBus(tmp_path)
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")

    async def scenario():
        portal = await Portal().start()
        try:
            await supervisor.start_failure_notice(supervisor.Words(config), "linux")
            await asyncio.sleep(0.1)
            assert [title for _, title in portal.added] == ["DownloadPulse could not start"]
        finally:
            await portal.stop()

    try:
        asyncio.run(scenario())
    finally:
        bus.close()


# ── the quit endpoint, against a real backend ──────────────────────────────

class _Stream:
    """One event stream of the page, opened at ``port`` (the window's own, or
    Local Port's: a browser tab there)."""

    async def open(self, port: int):
        self.session = aiohttp.ClientSession()
        self.reply = await self.session.get(f"http://127.0.0.1:{port}/api/events/stream",
                                            timeout=aiohttp.ClientTimeout(total=None))
        assert self.reply.status == 200
        assert (await self.event(10))[0] == "connected"
        return self

    async def event(self, timeout: float):
        """The next event other than a heartbeat, or None in ``timeout``."""
        name = None
        try:
            while True:
                line = (await asyncio.wait_for(self.reply.content.readline(), timeout)).decode().rstrip("\n")
                if line.startswith("event: "):
                    name = line[len("event: "):]
                elif line.startswith("data: ") and name not in (None, "ping"):
                    return name, json.loads(line[len("data: "):])
        except TimeoutError:
            return None

    async def close(self):
        self.reply.close()
        await self.session.close()


@pytest.mark.asyncio
async def test_quit_is_asked_of_the_windows_own_page_alone_and_needs_its_single_use_nonce(tmp_path):
    local = free_port()
    desktop = await _port_backend(tmp_path / "desktop", local).start()
    server = await Backend(tmp_path / "server", desktop=False, channel=False, locked=False).start()
    streams = []
    try:
        assert await desktop.ready() and await server.ready()
        ask = desktop.channel.ask
        quit_ = lambda nonce, backend=desktop: backend.exchange("POST", "/api/desktop/quit",  # noqa: E731
                                                                 json={"nonce": nonce})
        # Nothing pending: no quit, whatever is sent.
        status, body = await quit_("guess")
        assert status == 409 and body["code"] == "desktop.quit_refused"
        assert (await ask("quit_prompt"))["prompted"] is False             # no page at all: minimize
        tab = await _Stream().open(local)                                    # a browser tab, through Local Port
        streams.append(tab)
        assert (await ask("quit_prompt"))["prompted"] is False             # a tab is never asked
        window = await _Stream().open(desktop.port)                          # the window's own page
        streams.append(window)
        assert (await ask("quit_prompt"))["prompted"] is True
        name, first = await window.event(10)
        assert name == "desktop_quit_prompt" and set(first) == {"nonce"} and len(first["nonce"]) >= 32
        assert (await ask("quit_prompt"))["prompted"] is True              # closed again: a new prompt
        name, second = await window.event(10)
        assert name == "desktop_quit_prompt" and second["nonce"] != first["nonce"]
        assert await tab.event(1) is None                                    # the tab heard neither
        # Only the prompt pending is answered, once.
        assert (await quit_(first["nonce"]))[0] == 409
        assert (await ask("show"))["quit"] is False
        status, body = await quit_(second["nonce"])
        assert status == 200 and body == {"quitting": True}
        status, body = await quit_(second["nonce"])
        assert status == 409 and body["code"] == "desktop.quit_refused"
        # The supervisor's next look carries it, once.
        answer = await ask("show")
        assert answer["quit"] is True and answer["show"] is False
        assert (await ask("show"))["quit"] is False
        # A server has no window to quit.
        status, body = await quit_("anything", server)
        assert status == 404 and body["code"] == "desktop.quit_only"
    finally:
        for stream in streams:
            await stream.close()
        await desktop.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_quit_is_an_admin_action_and_a_signed_out_window_is_not_asked(tmp_path):
    from auth.permissions import ACTIONS, Capability
    assert ACTIONS["api.routes.desktop_quit"] is Capability.ADMINISTER
    backend = Backend(tmp_path, locked=True, config_extra={"desktop_local_port": free_port()})
    backend.env = {"DOWNLOADPULSE_DESKTOP_PORT": str(backend.port)}     # the window's own address, as served
    await backend.start()
    try:
        assert await backend.ready()
        assert await backend.http("GET", "/api/events/stream") == 401        # signed out: no page to ask
        assert (await backend.channel.ask("quit_prompt"))["prompted"] is False
        assert await backend.http("POST", "/api/desktop/quit", json={"nonce": "x"}) in {401, 403}
        assert (await backend.channel.ask("show"))["quit"] is False
    finally:
        await backend.stop()


# ── the real supervisor: SIGTERM, and a start that fails ────────────────────

FAKE_SHELL = """#!{python}
# A stand-in native shell: it exports its credential where Neutralino does and
# answers every native call, recording each; SIGTERM ends it (as its handler).
import asyncio, json, os, sys
from pathlib import Path
from aiohttp import web

record = os.environ["FAKE_SHELL_RECORD"]
Path(record + ".pid").write_text(str(os.getpid()))
mode = os.environ.get("FAKE_SHELL_MODE", "")
if mode == "fail":
    sys.exit(7)


async def main():
    if mode == "hang":
        await asyncio.Event().wait()

    async def native(request):
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        async for message in sock:
            call = json.loads(message.data)
            with open(record, "a") as out:
                out.write(call["method"] + "\\n")
            await sock.send_str(json.dumps({{"id": call["id"], "data": {{"success": True}}}}))
        return sock

    app = web.Application()
    app.router.add_get("/", native)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    folder = Path(os.environ["XDG_DATA_HOME"]) / "net.downloadpulse.desktop" / ".tmp"
    (folder / "auth_info.part").write_text(json.dumps({{"nlToken": "t", "nlPort": port, "nlConnectToken": "c"}}))
    os.replace(folder / "auth_info.part", folder / "auth_info.json")
    await asyncio.Event().wait()

asyncio.run(main())
"""


class Supervisor:
    """The real supervisor process, on a disposable data root and layout whose
    native shell is the stand-in above."""

    def __init__(self, tmp_path: Path, mode: str = ""):
        self.tmp, self.mode = tmp_path, mode
        layout = tmp_path / "layout"
        (layout / "desktop" / "shell" / "resources" / "icons").mkdir(parents=True)
        (layout / "backend").symlink_to(BACKEND)
        (layout / "frontend").symlink_to(ROOT / "frontend")
        shell = layout / "desktop" / "shell" / "downloadpulse-shell"
        shell.write_text(FAKE_SHELL.format(python=sys.executable), encoding="utf-8")
        shell.chmod(0o755)
        self.root = tmp_path / "xdg" / "downloadpulse"
        (self.root / "config").mkdir(parents=True)
        (tmp_path / "home").mkdir()
        (tmp_path / "downloads").mkdir()
        (self.root / "config" / "config.json").write_text(json.dumps(
            {"desktop_local_port": free_port(), "download_folder": str(tmp_path / "downloads")}), encoding="utf-8")
        self.record = tmp_path / "shell-calls"
        self.env = {key: value for key, value in os.environ.items()
                    if key not in {"DBUS_SESSION_BUS_ADDRESS", "DOWNLOADPULSE_DESKTOP", "DOWNLOADPULSE_DESKTOP_CONTROL"}}
        self.env.update(XDG_DATA_HOME=str(tmp_path / "xdg"), HOME=str(tmp_path / "home"),
                        DOWNLOADPULSE_DESKTOP_LAYOUT=str(layout), FAKE_SHELL_RECORD=str(self.record),
                        FAKE_SHELL_MODE=mode)

    async def start(self):
        self.started = time.monotonic()
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / "desktop" / "supervisor.py"), env=self.env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return self

    def log(self) -> str:
        try:
            return (self.root / "logs" / "desktop.log").read_text(encoding="utf-8")
        except OSError:
            return ""

    def calls(self) -> list[str]:
        try:
            return self.record.read_text(encoding="utf-8").split()
        except OSError:
            return []

    def shell_pid(self) -> int | None:
        try:
            return int(Path(str(self.record) + ".pid").read_text())
        except (OSError, ValueError):
            return None

    async def ended(self, timeout: float = 90) -> int:
        try:
            await asyncio.wait_for(self.process.wait(), timeout)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
            raise AssertionError(f"the supervisor did not end:\n{self.log()}") from None
        return self.process.returncode


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _ordered(log: str, *lines: str) -> bool:
    places = [log.find(line) for line in lines]
    return -1 not in places and places == sorted(places)


@LINUX_JEEPNEY
@pytest.mark.asyncio
async def test_sigterm_while_serving_quits_in_order_with_no_orphan_shell(tmp_path):
    run = await Supervisor(tmp_path).start()
    assert await _until(lambda: "os.setTray" in run.calls() or run.process.returncode is not None, 150), run.log()
    run.process.send_signal(signal.SIGTERM)
    assert await run.ended() == 0, run.log()
    log = run.log()
    assert _ordered(log, "SIGTERM: quitting", "Quit (SIGTERM)", "the native shell was ended by the supervisor",
                    "the DownloadPulse backend ended through its own shutdown (0)"), log
    assert "could not start" not in log and "Traceback" not in log
    assert run.shell_pid() is not None and _gone(run.shell_pid())
    assert "Scheduler started" in (run.root / "logs" / "backend.log").read_text(encoding="utf-8")


@LINUX_JEEPNEY
@pytest.mark.asyncio
async def test_sigterm_during_start_stops_what_started_in_order_and_says_no_failure(tmp_path):
    run = await Supervisor(tmp_path, mode="hang").start()
    assert await _until(lambda: run.shell_pid() is not None or run.process.returncode is not None, 150), run.log()
    run.process.send_signal(signal.SIGTERM)
    assert await run.ended() == 0, run.log()
    log = run.log()
    assert _ordered(log, "SIGTERM during start", "the native shell was ended by the supervisor",
                    "the DownloadPulse backend ended through its own shutdown (0)", "stopped by SIGTERM during start"), log
    assert "could not start" not in log and "notice was not shown" not in log
    assert _gone(run.shell_pid())


@LINUX_JEEPNEY
@pytest.mark.asyncio
async def test_a_shell_that_ends_during_start_fails_it_at_once_logged_and_noticed(tmp_path):
    run = await Supervisor(tmp_path, mode="fail").start()
    assert await run.ended(150) == 1, run.log()
    log = run.log()
    assert "DownloadPulse could not start" in log and "Traceback" in log
    assert "RuntimeError: the native shell ended during start (7)" in log
    # The notice was tried (this test's process has no session bus: its failure is logged).
    assert "the notice was not shown (DownloadPulse could not start)" in log
    assert _ordered(log, "could not start", "the notice was not shown")
    assert time.monotonic() - run.started < supervisor.READY_SECONDS
