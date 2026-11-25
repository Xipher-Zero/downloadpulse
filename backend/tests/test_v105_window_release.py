"""The desktop window released while hidden, its state kept (1.0.5 WS8a).

A real desktop backend, its control channel and its own window address: the
supervisor asks the window's own page whether the window can be released
(op ``release_prompt``, event ``desktop_release_prompt``); the page answers with
the prompt's single-use nonce, whether nothing would be lost, and the state it
would come back to (PUT /api/desktop/window-state); the supervisor is handed
that answer once (op ``show``: ``release``) and, releasing on it, says so (op
``released``); the relaunched page takes the state once (GET), with the
activity journal's position at the release. Never a browser tab or a Local Port
client, and nothing for an ordinary reload."""
from __future__ import annotations

import sys
from pathlib import Path

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.ports import free_port  # noqa: E402
from test_v103_desktop_lifecycle import _Stream  # noqa: E402
from test_v114_desktop_control import Backend  # noqa: E402
from test_v114_desktop_local_port import _port_backend  # noqa: E402

STATE = {"route": "/downloads", "owners": {"downloads": {"filter": "error", "page": 3}}}


@pytest.mark.asyncio
async def test_a_hidden_window_is_released_only_on_its_own_pages_answer_and_its_state_comes_back_once(tmp_path):
    local = free_port()
    desktop = await _port_backend(tmp_path / "desktop", local).start()
    streams = []
    try:
        assert await desktop.ready()
        ask = desktop.channel.ask
        put = lambda body: desktop.exchange("PUT", "/api/desktop/window-state", json=body)  # noqa: E731
        take = lambda: desktop.exchange("GET", "/api/desktop/window-state")  # noqa: E731

        # Nothing pending: no answer is taken, nothing is released, nothing comes back.
        status, body = await put({"nonce": "guess", "releasable": True, "state": STATE})
        assert (status, body["code"]) == (409, "desktop.window_state_refused")
        assert (await ask("show"))["release"] is None
        assert "error" in await ask("released", nonce="guess")
        assert await take() == (200, {"restore": None})
        assert (await ask("release_prompt"))["prompted"] is False          # no page at all

        # A tab through Local Port is never asked; the window's own page is.
        tab = await _Stream().open(local)
        streams.append(tab)
        assert (await ask("release_prompt"))["prompted"] is False
        window = await _Stream().open(desktop.port)
        streams.append(window)
        assert (await ask("release_prompt"))["prompted"] is True
        name, first = await window.event(10)
        assert name == "desktop_release_prompt" and set(first) == {"nonce"} and len(first["nonce"]) >= 32
        assert await tab.event(1) is None

        # Through Local Port nothing is answered or taken, whatever it names.
        forged = {"Host": f"127.0.0.1:{desktop.port}"}
        async with aiohttp.ClientSession() as session:
            for headers in (forged, {**forged, "X-Forwarded-For": "127.0.0.1"}):
                async with session.put(f"http://127.0.0.1:{local}/api/desktop/window-state", headers=headers,
                                       json={"nonce": first["nonce"], "releasable": True, "state": STATE}) as reply:
                    assert (reply.status, (await reply.json())["code"]) == (403, "desktop.window_request_only")
                async with session.get(f"http://127.0.0.1:{local}/api/desktop/window-state", headers=headers) as reply:
                    assert (reply.status, (await reply.json())["code"]) == (403, "desktop.window_request_only")

        # A page with something unfinished says so: nothing may be released.
        assert (await put({"nonce": first["nonce"], "releasable": False, "state": STATE}))[0] == 200
        assert (await ask("show"))["release"] == {"nonce": first["nonce"], "releasable": False}
        assert (await ask("show"))["release"] is None                       # handed once
        assert "error" in await ask("released", nonce=first["nonce"])
        assert await take() == (200, {"restore": None})

        # A new prompt replaces the old one: its nonce is spent.
        assert (await ask("release_prompt"))["prompted"] is True
        name, second = await window.event(10)
        assert second["nonce"] != first["nonce"]
        assert (await put({"nonce": first["nonce"], "releasable": True, "state": STATE}))[0] == 409
        # A state beyond its bound is no answer.
        huge = {"blob": "x" * 300_000}
        assert (await put({"nonce": second["nonce"], "releasable": True, "state": huge}))[0] == 409
        # Not yet handed to the supervisor: not released.
        assert (await put({"nonce": second["nonce"], "releasable": True, "state": STATE}))[0] == 200
        assert "error" in await ask("released", nonce=second["nonce"])
        assert (await ask("show"))["release"] == {"nonce": second["nonce"], "releasable": True}
        # Before the release, a reload takes nothing (an ordinary reload is never a restore).
        assert await take() == (200, {"restore": None})
        assert "error" in await ask("released", nonce="another")
        assert (await ask("released", nonce=second["nonce"]))["ok"] is True
        status, body = await take()
        assert status == 200 and body["restore"]["state"] == STATE and isinstance(body["restore"]["since"], int)
        assert await take() == (200, {"restore": None})                     # taken once
        assert "error" in await ask("released", nonce=second["nonce"])      # and released once
    finally:
        for stream in streams:
            await stream.close()
        await desktop.stop()


@pytest.mark.asyncio
async def test_a_server_has_no_window_state(tmp_path):
    server = await Backend(tmp_path / "server", desktop=False, channel=False, locked=False).start()
    try:
        assert await server.ready()
        status, body = await server.exchange("GET", "/api/desktop/window-state")
        assert (status, body["code"]) == (404, "desktop.window_only")
        status, body = await server.exchange("PUT", "/api/desktop/window-state",
                                             json={"nonce": "x", "releasable": True, "state": {}})
        assert (status, body["code"]) == (404, "desktop.window_only")
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_an_answer_sent_while_the_release_is_recorded_is_never_kept_in_place_of_the_handed_one(monkeypatch):
    """The page may answer again at any moment; ``released`` keeps only the
    answer the supervisor was handed, whatever arrives while it records the
    journal position."""
    import contextlib

    from application import desktop_control as control
    from db import event_journal
    monkeypatch.setattr(control, "_edge", {**control._edge, "prompt_release": _prompted})
    monkeypatch.setattr(control, "_restore", None)

    @contextlib.asynccontextmanager
    async def database():
        yield None

    async def latest_id(_db):
        # Meanwhile the page answers again: something is unfinished now.
        assert control.answer_release(nonce, False, {"v": "unfinished"})
        return 7

    monkeypatch.setattr("db.database.get_db", database)
    monkeypatch.setattr(event_journal, "latest_id", latest_id)
    assert (await control.answer(None, {"id": 1, "op": "release_prompt"}))["prompted"] is True
    nonce = control._release_nonce
    assert control.answer_release(nonce, True, {"v": "handed"})
    assert (await control.answer(None, {"id": 2, "op": "show"}))["release"] == {"nonce": nonce, "releasable": True}
    assert (await control.answer(None, {"id": 3, "op": "released", "nonce": nonce}))["error"] == "refused"
    assert control.take_restore() is None


async def _prompted(_nonce):
    return True
