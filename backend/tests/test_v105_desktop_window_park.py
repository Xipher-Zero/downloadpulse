"""A released window keeps its shell where its tray is the shell's own (1.0.6:
Windows and macOS), and lets its page go instead.

The supervisor's loop (``_serve`` with ``park``): a window hidden for
RELEASE_AFTER_SECONDS whose page answers that nothing would be lost is
released by parking its page (op ``park``) -- no shell ends and none is
started; the current shell's end is still Quit; Open (the tray's, a connected
browser's, or the system showing the window itself) shows the window as a
hidden one is, with the parked page still in it, and only then sends the page
back (op ``unpark``, 1.0.8: the application never loads in a hidden window);
a page that could not
be told keeps the window as it was, and it is asked again. The backend's half
(``desktop_control``): only a window just released is parked, the kept state
is dropped when no page will take it, and the parked page's wait ends on Open,
at once when nothing is parked, and after its bound otherwise, an unanswered
wait carrying the mode in force. The page's half is pinned in its sources: its
colour on its own root, inline, with its color-scheme.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop import supervisor  # noqa: E402
from test_v103_desktop_lifecycle import Running, Words, _stop, _until  # noqa: E402
from test_v105_desktop_window_release import Channel as ReleaseChannel, TestWindow, Tray, quick  # noqa: E402,F401

STATIC = ROOT / "frontend" / "static"


class Channel(ReleaseChannel):
    """The release handshake, and the park: ``parks`` says whether a window
    page was there to be told."""

    def __init__(self, *args, parks=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.parks, self.order = parks, []        # ``order``: unpark and the shell's calls, as they happen

    async def ask(self, op, **fields):
        if op == "park":
            self.asked.append((op, fields))
            return {"parked": self.parks}
        if op == "unpark":
            self.asked.append((op, fields))
            self.order.append(op)
            return {"parked": True}
        return await super().ask(op, **fields)


class VisibleShell:
    """The shell's calls; ``visible``: what window.isVisible answers."""

    def __init__(self, shell, order):
        self.inner, self.visible, self.order = shell, False, order

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def call(self, method, data=None):
        if method == "window.isVisible":
            return {"returnValue": self.visible}
        self.order.append(method)
        return await self.inner.call(method, data)


async def _serving(channel):
    window = TestWindow()
    await window.spawn(0)
    window.shell = VisibleShell(window.shell, channel.order)
    native = Tray()
    task = asyncio.ensure_future(supervisor._serve(channel, Words(), Running(), native, window,
                                                   release_allowed=True, park=True))
    return window, native, task


def _ops(channel):
    return [op for op, _ in channel.asked]


@pytest.mark.asyncio
async def test_a_hidden_window_is_parked_on_its_pages_answer_and_open_brings_its_page_back(quick):
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    try:
        shell = window.shell
        await window.events.put(("windowClose", None))
        assert await _until(lambda: window.parked, 5)
        assert ("released", {"nonce": "n1"}) in channel.asked and "park" in _ops(channel)
        assert quick == [] and window.spawns == 1 and not window.released    # no shell ended or started
        assert window.hidden and window.shell is shell
        await asyncio.sleep(0.3)
        assert _ops(channel).count("release_prompt") == 1                    # parked: never asked again
        await window.events.put(("trayMenuItemClicked", {"id": "open"}))
        assert await _until(lambda: not window.parked and not window.hidden, 5)
        assert "unpark" in _ops(channel) and window.spawns == 1
        assert [call for call in shell.inner.calls if call[0] in {"window.show", "window.focus"}] == \
            [("window.show", None), ("window.focus", None)]
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_open_shows_a_parked_window_before_its_page_is_sent_back(quick):
    """1.0.8 (WS-D1): the window is shown first, with the parked page still in
    it (it repaints at the window's real size), and only then is ``unpark``
    asked, so the application never loads in a hidden window."""
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    try:
        await window.events.put(("windowClose", None))
        assert await _until(lambda: window.parked, 5)
        del channel.order[:]
        await window.events.put(("trayMenuItemClicked", {"id": "open"}))
        assert await _until(lambda: "unpark" in channel.order, 5)
        assert channel.order == ["window.show", "window.focus", "unpark"]
        assert not window.parked and not window.hidden
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_a_parked_window_shown_by_the_system_is_shown_before_its_page_is_sent_back(quick, monkeypatch):
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")              # the Dock: macOS's alone
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    try:
        await window.events.put(("windowClose", None))
        assert await _until(lambda: window.parked, 5)
        del channel.order[:]
        window.shell.visible = True
        assert await _until(lambda: "unpark" in channel.order, 5)
        assert channel.order.index("window.show") < channel.order.index("unpark")
    finally:
        await _stop(task)


def test_bring_forward_shows_the_window_before_it_unparks_and_keeps_the_relaunch_path():
    source = (ROOT / "desktop" / "supervisor.py").read_text(encoding="utf-8")
    block = source.split("    async def bring_forward(why: str) -> None:", 1)[1].split("\n    try:\n", 1)[0]
    shown = block.index("for method in methods:\n            await window.call(method)")
    assert block.index("if window.released:") < shown < block.index("await window.unpark(channel)")
    assert block.count("await window.unpark(channel)") == 1
    assert "if await window.relaunch():" in block                          # Linux: released, launched again


@pytest.mark.asyncio
async def test_the_shells_own_end_while_parked_is_quit(quick):
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    await window.events.put(("windowClose", None))
    assert await _until(lambda: window.parked, 5)
    await window.events.put(("shellClosed", {"generation": window.generation}))   # macOS: the Dock's Quit
    assert await asyncio.wait_for(task, 5) == 0


@pytest.mark.asyncio
async def test_a_page_that_could_not_be_told_keeps_the_window_and_it_is_asked_again(quick):
    channel = Channel(releasable=True, parks=False)
    window, native, task = await _serving(channel)
    try:
        await window.events.put(("windowClose", None))
        assert await _until(lambda: _ops(channel).count("park") >= 2, 5)     # asked again after the retry
        assert not window.parked and not window.released and window.hidden and quick == []
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_a_parked_window_the_system_shows_by_itself_gets_its_page_back(quick, monkeypatch):
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")              # the Dock: macOS's alone
    channel = Channel(releasable=True)
    window, native, task = await _serving(channel)
    try:
        await window.events.put(("windowClose", None))
        assert await _until(lambda: window.parked, 5)
        window.shell.visible = True                                          # macOS: the Dock showed it
        assert await _until(lambda: not window.parked and not window.hidden, 5)
        assert "unpark" in _ops(channel)
    finally:
        await _stop(task)


def test_the_run_parks_only_where_its_tray_is_the_shells():
    source = (ROOT / "desktop" / "supervisor.py").read_text(encoding="utf-8")
    assert 'park = not sys.platform.startswith("linux")' in source
    assert "release_allowed=bus is not None or park, park=park)" in source
    assert "await (window.park(channel) if park else window.release())" in source


# ── the backend's half ──────────────────────────────────────────────────────

@pytest.fixture
def control(monkeypatch):
    from types import SimpleNamespace

    from application import desktop_control
    state = SimpleNamespace(page=True, told=[], module=desktop_control)

    async def park_window():
        state.told.append(True)
        return state.page
    monkeypatch.setattr(desktop_control, "_edge", {**desktop_control._edge, "park_window": park_window})
    monkeypatch.setattr(desktop_control, "_restore", None)
    monkeypatch.setattr(desktop_control, "_parked", None)
    monkeypatch.setattr(desktop_control, "_unparked", None)
    return state


@pytest.mark.asyncio
async def test_only_a_window_just_released_is_parked_and_a_state_no_page_takes_is_dropped(control):
    dc = control.module
    assert await dc.answer(None, {"id": 1, "op": "park"}) == {"id": 1, "parked": False}
    assert control.told == []                                                # nothing released: no page told
    dc._restore = {"state": {"route": "/"}, "since": 4}
    control.page = False
    assert await dc.answer(None, {"id": 2, "op": "park"}) == {"id": 2, "parked": False}
    assert dc._restore is None and not dc.parked()                           # no page: the window stays as it was
    dc._restore = {"state": {"route": "/"}, "since": 4}
    control.page = True
    assert await dc.answer(None, {"id": 3, "op": "park"}) == {"id": 3, "parked": True}
    assert dc.parked() and dc.restore_pending()
    assert await dc.answer(None, {"id": 4, "op": "park", "extra": 1}) == {"id": 4, "error": "refused"}


@pytest.mark.asyncio
async def test_the_parked_pages_wait_ends_on_open_and_its_page_takes_the_kept_state(control):
    dc = control.module
    dc._restore = {"state": {"route": "/"}, "since": 4}
    await dc.answer(None, {"id": 1, "op": "park"})
    waiting = asyncio.ensure_future(dc.wait_unparked())
    await asyncio.sleep(0.05)
    assert not waiting.done()
    assert await dc.answer(None, {"id": 2, "op": "unpark"}) == {"id": 2, "parked": True}
    assert await asyncio.wait_for(waiting, 2) is True
    assert not dc.parked() and dc.take_restore() == {"state": {"route": "/"}, "since": 4}
    assert await dc.wait_unparked() is True                                  # nothing parked: back at once


@pytest.mark.asyncio
async def test_open_before_the_page_left_drops_the_state_and_an_unanswered_wait_is_asked_again(control, monkeypatch):
    dc = control.module
    dc._restore = {"state": {"route": "/"}, "since": 4}
    await dc.answer(None, {"id": 1, "op": "park"})
    assert await dc.answer(None, {"id": 2, "op": "unpark"}) == {"id": 2, "parked": False}
    assert dc._restore is None                                               # the page never left: nothing to restore
    monkeypatch.setattr(dc, "PARKED_WAIT_SECONDS", 0.05)
    dc._restore = {"state": {"route": "/"}, "since": 5}
    await dc.answer(None, {"id": 3, "op": "park"})
    assert await dc.wait_unparked() is False and dc.parked()                 # bounded: the page asks again


def test_the_parked_page_and_the_route_are_the_windows_alone():
    routes = (ROOT / "backend" / "api" / "routes.py").read_text(encoding="utf-8")
    block = routes.split('@router.get("/desktop/parked")', 1)[1].split("\n\n\n", 1)[0]
    assert "_require_window(request)" in block and "wait_unparked()" in block
    assert '_sse_window_only("desktop_park"' in routes
    assert "if owner and not opened:" in block and '"mode": mode if mode in {"dark", "light"} else None' in block
    html = (STATIC / "desktop-parked.html").read_text(encoding="utf-8")
    assert '<script src="/desktop-parked.js"></script>' in html and "<script>" not in html and "<style" not in html
    # Its colour is the page's own, on its root, inline, with the matching color-scheme (dark until
    # its script, in the head, sets the mode before the body is parsed).
    assert '<html lang="en" style="background:#000000;color-scheme:dark">' in html
    assert html.index('<script src="/desktop-parked.js"></script>') < html.index("</head>")
    script = (STATIC / "desktop-parked.js").read_text(encoding="utf-8")
    assert "fetch('/api/desktop/parked'" in script and "window.location.replace('/')" in script
    assert "BACKGROUNDS = Object.freeze({dark: '#000000', light: '#ffffff'})" in script
    assert "root.style.background = BACKGROUNDS[mode];" in script and "root.style.colorScheme = mode;" in script
    assert "show(window.location.hash.slice(1));" in script and "if (body) show(body.mode);" in script
    css = (STATIC / "desktop-parked.css").read_text(encoding="utf-8")
    assert "#000" not in css and "#fff" not in css                          # never only the stylesheet's colour
    state = (STATIC / "ui-window-state.js").read_text(encoding="utf-8")
    assert "window.location.replace('/desktop-parked.html' + (light ? '#light' : '#dark'));" in state
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "es.addEventListener('desktop_park', function() {\n          DPWindowState.park();" in app


@pytest.mark.asyncio
async def test_the_runs_end_answers_a_parked_pages_wait_at_once_and_a_failed_telling_keeps_nothing(control):
    """Quit while parked: the channel's end answers the parked page's wait, so
    no request holds the backend's shutdown; a park whose telling fails drops
    the kept state, as one no page was told."""
    dc = control.module
    dc._restore = {"state": {"route": "/"}, "since": 4}
    await dc.answer(None, {"id": 1, "op": "park"})
    waiting = asyncio.ensure_future(dc.wait_unparked())
    await asyncio.sleep(0.05)
    dc.end_park()
    assert await asyncio.wait_for(waiting, 1) is True and not dc.parked()
    source = (ROOT / "backend" / "application" / "desktop_control.py").read_text(encoding="utf-8")
    assert "loop.call_soon_threadsafe(end_park)\n            loop.call_soon_threadsafe(signal.raise_signal" in source

    async def failing():
        raise RuntimeError("the SSE edge failed")
    dc._edge = {**dc._edge, "park_window": failing}
    dc._restore = {"state": {"route": "/"}, "since": 5}
    assert await dc.answer(None, {"id": 2, "op": "park"}) == {"id": 2, "parked": False}
    assert dc._restore is None and not dc.parked()
