"""The desktop supervisor releases a window hidden in its tray, and Open brings
it back (1.0.5 WS8d).

The supervisor's own loop (``_serve``) with its one window owner
(``supervisor.Window``): a window hidden for RELEASE_AFTER_SECONDS is
released only once its own page answers that nothing would be lost (the
backend's release handshake: release_prompt -> show's ``release`` ->
released), its shell ended and its geometry kept; a retired shell's close is
never Quit, the current one's is; Open launches it again, hidden at that
geometry, then shown; a failed relaunch -- at its spawn, its connection or
its first calls, or past its bound -- keeps the run, says so and is tried
again by the next Open; the tray going away while it is released brings it
back minimized (or says it could not); an unanswered prompt is asked again,
and a late answer never releases a window hidden for less than
RELEASE_AFTER_SECONDS; Quit while released ends no shell, and Quit during a
relaunch is answered within its bound; an Open that comes during a release
is answered after it; a run that may not release (``release_allowed`` off)
never does. Most shells, processes and the backend are recorded fakes; the
last proof drives the real process owner with a stand-in shell binary.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop import supervisor  # noqa: E402
from test_v103_desktop_lifecycle import Native, Running, Words, _stop, _until  # noqa: E402

pytestmark = pytest.mark.asyncio


class Channel:
    """The backend's control channel: a page that answers its release prompt
    ``releasable`` (or not), with a handshake the loop must follow."""

    def __init__(self, releasable=True, *, answers=True):
        self.asked, self.releasable, self.prompts, self._answer = [], releasable, 0, None
        self.answers, self.held = answers, None          # answers=False: the page never answers (held instead)

    async def ask(self, op, **fields):
        self.asked.append((op, fields))
        if op == "release_prompt":
            self.prompts += 1
            answer = {"nonce": f"n{self.prompts}", "releasable": self.releasable}
            if self.answers:
                self._answer = answer
            else:
                self.held = answer
            return {"prompted": True}
        if op == "show":
            answer, self._answer = self._answer, None
            return {"show": False, "quit": False, "release": answer}
        if op == "released":
            return {"ok": fields.get("nonce") == f"n{self.prompts}"}
        return {"events": {"cursor": 0, "events": []}, "status": {"status": {"active": 0, "queued": 0}},
                "locale": {"locale": "en"}, "quit_prompt": {"prompted": True},
                "frame": {"frame": None}, "saves": {"saves": []}}[op]


class Shell:
    """One launched shell: its calls, and the size it reports. ``broken``: a
    shell that connected and then died or went silent (its calls raise)."""

    def __init__(self, events, generation, broken=None):
        self.events, self.generation, self.calls, self.ws, self.broken = events, generation, [], None, broken

    async def call(self, method, data=None):
        self.calls.append((method, data))
        if self.broken is not None:
            raise self.broken
        if method == "window.getSize":
            return {"width": 1500, "height": 900}
        if method == "window.isMaximized":
            return {"returnValue": False}
        return None


class Processes:
    def alive(self, _identity):
        return False

    def forget(self, _identity):
        pass


class TestWindow(supervisor.Window):
    """The window owner with its spawn recorded (no process): ``fail`` makes
    the next spawns fail, as a shell that cannot start; ``broken`` gives the
    next a shell that dies once connected; ``hang`` a spawn that never ends;
    ``relaunches`` records what each relaunch's shell would be started with."""

    def __init__(self, **kwargs):
        super().__init__(processes=Processes(), paths={"shell": Path("/nowhere")}, root=Path("/nowhere"), **kwargs)
        self.spawns, self.fail, self.broken, self.hang, self.relaunches = 0, 0, None, False, []

    async def spawn(self, deadline, *, relaunch=False):
        self.spawns += 1
        if relaunch:
            self.relaunches.append(supervisor.relaunch_arguments(self.geometry))
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            self.fail -= 1
            raise RuntimeError("the native shell exported no credential")
        broken, self.broken = self.broken, None
        self.shell = Shell(self.events, self.generation + 1, broken)
        self.owned = object()
        self.generation = self.shell.generation


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(supervisor, "RELEASE_AFTER_SECONDS", 0.2)
    monkeypatch.setattr(supervisor, "RELEASE_RETRY_SECONDS", 0.2)
    ended = []

    async def end_shell(shell, owned, processes):
        ended.append(shell.generation)
    monkeypatch.setattr(supervisor, "_end_shell", end_shell)
    monkeypatch.setattr(supervisor, "clear_cookie_store", lambda runtime: None)
    return ended


class Tray(Native):
    def __init__(self):
        super().__init__(tray_shown=True)
        self.noticed = []

    async def notice(self, title, body):
        self.noticed.append(title)


async def _serving(channel, *, native=None, window=None):
    window = window or TestWindow()
    await window.spawn(0)
    native = native or Tray()
    task = asyncio.ensure_future(supervisor._serve(channel, Words(), Running(), native, window, release_allowed=True))
    return window, native, task


@pytest.mark.only_on(sys.platform.startswith("linux"), reason="the release is Linux's (its tray is the supervisor's)")
async def test_a_hidden_window_is_released_on_its_pages_answer_and_open_brings_it_back(quick):
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    try:
        first = window.shell
        await window.events.put(("windowClose", None))
        assert await _until(lambda: window.released, 5)
        assert quick == [1]                                               # its shell ended
        assert ("released", {"nonce": "n1"}) in channel.asked
        assert window.geometry == {"width": 1500, "height": 900, "maximized": False}
        await window.events.put(("shellClosed", {"generation": first.generation}))   # the retired shell's end
        await asyncio.sleep(0.2)
        assert not task.done()                                            # no Quit
        await window.events.put(("trayMenuItemClicked", {"id": "open"}))
        assert await _until(lambda: not window.released, 5)
        assert window.spawns == 2 and window.shell is not first
        # Started hidden at the size it had (Neutralino's saved state set aside), then shown.
        assert window.relaunches == [["--window-hidden=true", "--window-use-saved-state=false",
                                      "--window-width=1500", "--window-height=900"]]
        assert window.shell.calls == [("window.show", None)] and not window.hidden
    finally:
        await _stop(task)


@pytest.mark.only_on(sys.platform.startswith("linux"), reason="the release is Linux's")
async def test_after_a_relaunch_only_the_current_shells_close_is_quit(quick):
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    first = window.shell.generation
    await window.events.put(("windowClose", None))
    assert await _until(lambda: window.released, 5)
    await window.events.put(("trayMenuItemClicked", {"id": "open"}))
    assert await _until(lambda: not window.released, 5) and window.generation == first + 1
    await window.events.put(("shellClosed", {"generation": first}))           # the retired shell, ending late
    await asyncio.sleep(0.3)
    assert not task.done()                                                    # no Quit: not the current shell
    await window.events.put(("shellClosed", {"generation": window.generation}))
    assert await asyncio.wait_for(task, 5) == 0                               # the current shell's end is Quit


async def test_a_relaunch_is_started_hidden_and_maximized_if_it_was():
    assert supervisor.relaunch_arguments(None) == ["--window-hidden=true", "--window-use-saved-state=false"]
    assert supervisor.relaunch_arguments({"width": 800, "height": 600, "maximized": True})[-1] == \
        "--window-maximize=true"


@pytest.mark.only_on(sys.platform.startswith("linux"), reason="the release is Linux's")
async def test_a_page_that_says_something_would_be_lost_keeps_its_window_and_is_asked_again(quick):
    channel = Channel(releasable=False)
    window, native, task = await _serving(channel)
    try:
        await window.events.put(("windowClose", None))
        assert await _until(lambda: channel.prompts >= 2, 5)              # asked again after the retry
        assert not window.released and quick == [] and not any(op == "released" for op, _ in channel.asked)
    finally:
        await _stop(task)


@pytest.mark.only_on(sys.platform.startswith("linux"), reason="the release is Linux's")
async def test_a_failed_relaunch_keeps_the_run_says_so_and_the_next_open_tries_again(quick):
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    try:
        await window.events.put(("windowClose", None))
        assert await _until(lambda: window.released, 5)
        window.fail = 1
        await window.events.put(("trayMenuItemClicked", {"id": "open"}))
        assert await _until(lambda: native.noticed == ["desktop.error.reopen_failed_title"], 5)
        assert window.released and not task.done()
        await window.events.put(("trayMenuItemClicked", {"id": "open"}))
        assert await _until(lambda: not window.released, 5) and window.spawns == 3
    finally:
        await _stop(task)


@pytest.mark.only_on(sys.platform.startswith("linux"), reason="the release is Linux's")
@pytest.mark.parametrize("broken", [ConnectionResetError("the shell went away"), TimeoutError()])
async def test_a_shell_that_dies_or_goes_silent_after_connecting_is_a_failed_relaunch_not_the_runs_end(quick, broken):
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    try:
        await window.events.put(("windowClose", None))
        assert await _until(lambda: window.released, 5)
        window.broken = broken
        await window.events.put(("trayMenuItemClicked", {"id": "open"}))
        assert await _until(lambda: native.noticed == ["desktop.error.reopen_failed_title"], 5)
        assert window.released and window.shell is None and not task.done()
        assert quick[-1] == 2                                                 # the shell that started was ended
        await window.events.put(("trayMenuItemClicked", {"id": "open"}))
        assert await _until(lambda: not window.released, 5)
    finally:
        await _stop(task)


@pytest.mark.only_on(sys.platform.startswith("linux"), reason="the release is Linux's")
async def test_a_relaunch_is_bounded_and_quit_during_one_is_answered_within_its_bound(quick, monkeypatch):
    monkeypatch.setattr(supervisor, "RELAUNCH_SECONDS", 0.5)
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    await window.events.put(("windowClose", None))
    assert await _until(lambda: window.released, 5)
    window.hang = True
    await window.events.put(("trayMenuItemClicked", {"id": "open"}))
    await asyncio.sleep(0.1)                                                  # the relaunch is under way
    asked = asyncio.get_running_loop().time()
    await window.events.put(("trayMenuItemClicked", {"id": "quit"}))
    assert await asyncio.wait_for(task, 5) == 0
    assert asyncio.get_running_loop().time() - asked < 1.5                    # the bound, not READY_SECONDS
    assert native.noticed == ["desktop.error.reopen_failed_title"] and window.released


@pytest.mark.only_on(sys.platform.startswith("linux"), reason="the release is Linux's")
async def test_an_open_that_comes_during_a_release_is_answered_after_it(quick, monkeypatch):
    releasing = asyncio.Event()

    async def slow_end(shell, owned, processes):
        releasing.set()
        await asyncio.sleep(0.3)
        quick.append(shell.generation)
    monkeypatch.setattr(supervisor, "_end_shell", slow_end)
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    try:
        await window.events.put(("windowClose", None))
        await asyncio.wait_for(releasing.wait(), 5)
        await window.events.put(("trayMenuItemClicked", {"id": "open"}))       # queued while the shell ends
        assert await _until(lambda: window.spawns == 2 and not window.released, 5)
        assert quick == [1] and ("window.show", None) in window.shell.calls and not task.done()
    finally:
        await _stop(task)


@pytest.mark.only_on(sys.platform.startswith("linux"), reason="the release is Linux's")
async def test_an_unanswered_prompt_is_asked_again_and_a_late_answer_never_releases_a_window_newly_hidden(quick,
                                                                                                       monkeypatch):
    monkeypatch.setattr(supervisor, "RELEASE_AFTER_SECONDS", 1.0)
    monkeypatch.setattr(supervisor, "RELEASE_RETRY_SECONDS", 0.3)
    channel = Channel(releasable=True, answers=False)
    window, native, task = await _serving(channel)
    try:
        await window.events.put(("windowClose", None))
        assert await _until(lambda: channel.prompts >= 1, 5)
        assert await _until(lambda: channel.prompts >= 2, 5)                  # never answered: asked again
        # The tray goes away (shown minimized) and comes back; it is hidden again, newly.
        await window.events.put(("trayAvailability", {"shown": False}))
        await window.events.put(("windowClose", None))
        assert await _until(lambda: window.hidden, 5)
        prompts = channel.prompts
        channel._answer = channel.held                                        # the earlier prompt's answer, late
        await asyncio.sleep(0.3)
        assert not window.released and not any(op == "released" for op, _ in channel.asked)
        # Hidden long enough again: asked anew, and its answer releases it.
        channel.answers = True
        assert await _until(lambda: window.released, 5) and channel.prompts > prompts
    finally:
        await _stop(task)


@pytest.mark.only_on(sys.platform.startswith("linux"), reason="the release is Linux's")
async def test_the_tray_going_away_while_released_brings_it_back_minimized_and_quit_while_released_ends_no_shell(quick):
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    try:
        await window.events.put(("windowClose", None))
        assert await _until(lambda: window.released, 5)
        await window.events.put(("trayAvailability", {"shown": False}))
        assert await _until(lambda: not window.released, 5)
        assert window.shell.calls == [("window.show", None), ("window.minimize", None)] and window.minimized
    finally:
        await _stop(task)
    # The tray went away and the relaunch failed: no tray, no window -- the notice says so.
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    try:
        await window.events.put(("windowClose", None))
        assert await _until(lambda: window.released, 5)
        window.fail = 1
        await window.events.put(("trayAvailability", {"shown": False}))
        assert await _until(lambda: native.noticed == ["desktop.error.reopen_failed_title"], 5)
        assert window.released and not task.done()
    finally:
        await _stop(task)
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    await window.events.put(("windowClose", None))
    assert await _until(lambda: window.released, 5)
    ended_before_quit = list(quick)
    await window.events.put(("trayMenuItemClicked", {"id": "quit"}))
    assert await asyncio.wait_for(task, 5) == 0
    await window.end()
    assert quick == ended_before_quit                                     # a released window has no shell to end


async def test_a_run_that_may_not_release_never_does(quick):
    """``release_allowed`` is the run's fact (Linux, whose tray is the
    supervisor's): off -- Windows and macOS, whose tray is the shell's -- a
    hidden window is never asked, never released."""
    channel = Channel(releasable=True)
    window = TestWindow()
    await window.spawn(0)
    task = asyncio.ensure_future(supervisor._serve(channel, Words(), Running(), Tray(), window))
    try:
        await window.events.put(("windowClose", None))
        await asyncio.sleep(0.6)
        assert window.hidden and not window.released and channel.prompts == 0
    finally:
        await _stop(task)


@pytest.mark.only_on(sys.platform.startswith("linux"), reason="the run-scoped cookie store is the Linux shell's")
async def test_a_linux_shell_is_given_the_run_scoped_cookie_store_and_a_run_clears_it(tmp_path, monkeypatch):
    """The shell keeps its sign-in in the store the supervisor names (its
    patch: DOWNLOADPULSE_COOKIE_STORE), inside the private runtime folder; a
    start and Quit clear it and its SQLite sidecars, so a new run asks for
    sign-in as before."""
    from types import SimpleNamespace
    spawned = {}

    class Owner:
        def alive(self, _identity):
            return False

        def forget(self, _identity):
            pass

        async def spawn(self, identity, argv, env=None, **_):
            spawned.update(identity=identity, env=env)
            return SimpleNamespace(process=SimpleNamespace(stdout=None, stderr=None, returncode=None))

    async def connected(self, auth_file, deadline, owned):
        return None
    monkeypatch.setattr(supervisor.Shell, "connect", connected)
    monkeypatch.setattr(supervisor, "shell_data_home", lambda: tmp_path / "data-home")
    (tmp_path / "data-home" / supervisor.APP_ID).mkdir(parents=True)      # the shell's data home exists, as installed
    window = supervisor.Window(processes=Owner(), paths={"shell": tmp_path / "shell"}, root=tmp_path, port=1)
    await window.spawn(0)
    runtime = tmp_path / "desktop-runtime"
    assert spawned["env"]["DOWNLOADPULSE_COOKIE_STORE"] == str(runtime / supervisor.COOKIE_STORE)
    assert (runtime.stat().st_mode & 0o777) == 0o700                     # private, as the credential's
    for suffix in ("", "-wal", "-shm", "-journal"):
        (runtime / f"{supervisor.COOKIE_STORE}{suffix}").write_text("x")
    supervisor.clear_cookie_store(runtime)
    assert not list(runtime.glob(f"{supervisor.COOKIE_STORE}*"))


@pytest.mark.only_on(sys.platform.startswith("linux"), reason="the release is Linux's (its process groups)")
async def test_a_symlinked_credential_folder_on_relaunch_refuses_the_shell_and_keeps_the_run(tmp_path, monkeypatch):
    """B5: every relaunch makes the credential's private folder first; one that
    became a symlink is refused (no shell spawned), and the run goes on."""
    from types import SimpleNamespace
    spawned = []

    class Owner:
        def alive(self, _identity):
            return False

        def forget(self, _identity):
            pass

        async def spawn(self, identity, argv, env=None, **_):
            spawned.append(argv)
            return SimpleNamespace(process=SimpleNamespace(stdout=None, stderr=None, returncode=None))

    async def connected(self, auth_file, deadline, owned):
        return None
    monkeypatch.setattr(supervisor.Shell, "connect", connected)
    monkeypatch.setattr(supervisor, "shell_data_home", lambda: tmp_path / "data-home")
    monkeypatch.setattr(supervisor, "clear_cookie_store", lambda runtime: None)
    (tmp_path / "data-home" / supervisor.APP_ID).mkdir(parents=True)
    window = supervisor.Window(processes=Owner(), paths={"shell": tmp_path / "shell"}, root=tmp_path, port=1)
    await window.spawn(0)
    assert len(spawned) == 1
    # As after a release: no shell; then the credential's folder is swapped for a link.
    window.shell, window.owned, window.released, window.hidden = None, None, True, True
    credential = tmp_path / "data-home" / supervisor.APP_ID / ".tmp"
    for leftover in credential.iterdir():
        leftover.unlink()
    credential.rmdir()
    (tmp_path / "elsewhere").mkdir()
    credential.symlink_to(tmp_path / "elsewhere")
    native = Tray()
    task = asyncio.ensure_future(supervisor._serve(Channel(), Words(), Running(), native, window, release_allowed=True))
    try:
        await window.events.put(("trayMenuItemClicked", {"id": "open"}))
        assert await _until(lambda: native.noticed == ["desktop.error.reopen_failed_title"], 5)
        assert len(spawned) == 1 and window.released and not task.done()     # refused before any spawn
    finally:
        await _stop(task)


@pytest.mark.only_on(sys.platform.startswith("linux"), reason="the release is Linux's (its process groups)")
async def test_three_releases_and_relaunches_through_the_real_process_owner_leave_nothing_behind(tmp_path, monkeypatch):
    """The window owner with the real process owner (``ProcessOwnership``) and
    a stand-in shell binary, released and relaunched three times: each
    release proves its shell's process gone; each relaunch is a new process,
    started hidden, that exported a new credential (read once and deleted)
    and was shown; and no task or descriptor is left over from a cycle."""
    import os
    import time

    import aiohttp
    from executors.process_ownership import ProcessOwnership
    from test_v103_desktop_lifecycle import FAKE_SHELL, _gone

    shell_dir = tmp_path / "shell"
    shell_dir.mkdir()
    binary = supervisor.shell_binary_path(shell_dir)
    binary.write_text(FAKE_SHELL.format(python=sys.executable), encoding="utf-8")
    binary.chmod(0o755)
    record = tmp_path / "shell-calls"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("FAKE_SHELL_RECORD", str(record))
    monkeypatch.delenv("FAKE_SHELL_MODE", raising=False)
    root = tmp_path / "root"
    credential = tmp_path / "xdg" / supervisor.APP_ID / ".tmp" / "auth_info.json"
    credential.parent.parent.mkdir(parents=True)                              # the shell's data home, as installed

    class Recorded(ProcessOwnership):
        def __init__(self, *args):
            super().__init__(*args)
            self.argvs = []

        async def spawn(self, attempt_id, argv, **kwargs):
            self.argvs.append(list(argv))
            return await super().spawn(attempt_id, argv, **kwargs)

    def pid():
        return int(Path(str(record) + ".pid").read_text())

    def calls():
        return record.read_text(encoding="utf-8").split() if record.exists() else []

    processes = Recorded(root / "desktop-runtime")
    async with aiohttp.ClientSession() as session:
        window = supervisor.Window(session=session, processes=processes, paths={"shell": shell_dir}, root=root, port=1)
        await window.spawn(time.monotonic() + 30)
        pids, baseline = [pid()], None
        try:
            for _cycle in range(3):
                window.hidden, window.hidden_since = True, time.monotonic()
                assert await window.release() is True                         # its process group proven gone
                assert window.shell is None and _gone(pids[-1])
                shows = calls().count("window.show")
                assert await window.relaunch() is True
                assert pid() not in pids and not _gone(pid())
                pids.append(pid())
                assert not credential.exists()                                # a new one, exported and consumed
                assert calls().count("window.show") == shows + 1 and not window.released
                await asyncio.sleep(0.3)                                      # the retired shell's reader ends
                counts = (len(asyncio.all_tasks()), len(os.listdir("/proc/self/fd")))
                if baseline is None:
                    baseline = counts
                assert counts[0] <= baseline[0] and counts[1] <= baseline[1], (counts, baseline)
        finally:
            await window.end()
        assert _gone(pids[-1])
    assert "--window-hidden=true" not in processes.argvs[0]                   # the first window opens as ever
    assert all("--window-hidden=true" in argv and "--window-use-saved-state=false" in argv
               for argv in processes.argvs[1:]) and len(processes.argvs) == 4
