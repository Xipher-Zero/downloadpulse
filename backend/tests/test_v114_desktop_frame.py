"""The desktop application's window frame takes the page's theme.

The page says its canvas and text colours, dark or light (PUT /desktop/frame,
on load and on every theme change); the backend keeps the latest (op "frame"
of the control channel); the supervisor colours the window's own frame with it
-- macOS through the shell's window.setFrameTheme (our Neutralino patch),
Windows through DwmSetWindowAttribute on the shell's windows, Linux not at all.
Only the window's own page says it (``_require_window``), never a client
through Local Port. A server has no frame.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import aiohttp
import pytest

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(ROOT))

from application import desktop_control  # noqa: E402
from desktop import supervisor  # noqa: E402
from core.ports import free_port  # noqa: E402
from test_v114_desktop_control import Backend  # noqa: E402
from test_v114_desktop_local_port import _port_backend  # noqa: E402


def test_only_two_hex_colours_and_a_dark_flag_make_a_frame():
    assert desktop_control.set_frame("#05081A", "#F7F8FF", True) == {"caption": "#05081a", "text": "#f7f8ff",
                                                                   "dark": True}
    for caption, text, dark in (("05081a", "#f7f8ff", True), ("#05081", "#f7f8ff", True),
                                ("#05081a", "rgb(1,2,3)", True), ("#05081a", "#f7f8ff", "yes")):
        with pytest.raises(ValueError):
            desktop_control.set_frame(caption, text, dark)


@pytest.mark.asyncio
async def test_the_page_says_its_frame_and_the_supervisor_reads_it_and_a_server_has_none(tmp_path):
    local = free_port()
    desktop = await _port_backend(tmp_path / "desktop", local).start()
    server = await Backend(tmp_path / "server", desktop=False, channel=False, locked=False).start()
    try:
        assert await desktop.ready() and await server.ready()
        assert (await desktop.channel.ask("frame"))["frame"] is None              # until the page says
        # A client through Local Port never colours the window's frame, whatever it names (1.0.8).
        forged = {"Host": f"127.0.0.1:{desktop.port}"}
        async with aiohttp.ClientSession() as session:
            for headers in ({}, forged, {**forged, "X-Forwarded-For": "127.0.0.1"}):
                async with session.put(f"http://127.0.0.1:{local}/api/desktop/frame", headers=headers,
                                       json={"caption": "#000000", "text": "#ffffff", "dark": True}) as reply:
                    assert (reply.status, (await reply.json())["code"]) == (403, "desktop.window_request_only")
        assert (await desktop.channel.ask("frame"))["frame"] is None
        status, body = await desktop.exchange("PUT", "/api/desktop/frame",
                                              json={"caption": "#f7f7fb", "text": "#111a34", "dark": False})
        assert status == 200 and body == {"caption": "#f7f7fb", "text": "#111a34", "dark": False}
        assert (await desktop.channel.ask("frame"))["frame"] == body
        status, body = await desktop.exchange("PUT", "/api/desktop/frame",
                                              json={"caption": "white", "text": "#111a34", "dark": False})
        assert status == 422 and body["code"] == "desktop.frame_invalid"
        status, body = await server.exchange("PUT", "/api/desktop/frame",
                                             json={"caption": "#f7f7fb", "text": "#111a34", "dark": False})
        assert status == 404 and body["code"] == "desktop.frame_only"
    finally:
        await desktop.stop()
        await server.stop()


def test_windows_takes_the_colours_as_colorref():
    assert supervisor.colorref("#05081a") == 0x1A0805
    assert supervisor.colorref("#ffffff") == 0xFFFFFF


@pytest.mark.asyncio
@pytest.mark.only_on(sys.platform == "darwin", reason="the macOS path: the shell's own method")
async def test_macos_gives_the_frame_to_the_shells_method():
    calls = []

    class Shell:
        async def call(self, method, data=None, timeout=15):
            calls.append((method, data))

    await supervisor.apply_frame(Shell(), {"caption": "#05081a", "text": "#f7f8ff", "dark": True})
    assert calls == [("window.setFrameTheme", {"caption": "#05081a", "dark": True})]


def test_the_frame_is_wired_from_the_page_to_the_shell():
    import json
    assert "window.setFrameTheme" in supervisor.NATIVE_METHODS
    config = json.loads((ROOT / "desktop" / "shell" / "neutralino.config.json").read_text(encoding="utf-8"))
    assert "window.setFrameTheme" in config["nativeAllowList"]
    source = (ROOT / "desktop" / "supervisor.py").read_text(encoding="utf-8")
    assert 'frame = (await channel.ask("frame")).get("frame")' in source
    # Only a change is applied, and only to a window that has a shell (1.0.5 WS8d: a released one has none).
    assert "if frame and frame != applied_frame and window.shell is not None:" in source
    app = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    block = app[app.index("(function desktopFrame() {"):app.index("})();", app.index("(function desktopFrame() {"))]
    assert "if (window.downloadPulseWindow !== true) return;" in block                # the desktop window's own page only
    assert "document.addEventListener('downloadpulse:theme-changed', say);" in block
    assert "api('PUT', '/desktop/frame', frame)" in block


@pytest.mark.only_on(sys.platform.startswith("linux"), reason="the Linux path: the desktop's own frame")
def test_linux_leaves_its_desktops_frame_alone():
    calls = []

    class Shell:
        async def call(self, *args, **kwargs):
            calls.append(args)

    asyncio.run(supervisor.apply_frame(Shell(), {"caption": "#05081a", "text": "#f7f8ff", "dark": True}))
    assert calls == []
