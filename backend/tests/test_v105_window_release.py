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
        assert set(body["restore"]["away"]) == {"completed", "failed", "attention", "through"}   # WS8g
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


@pytest.mark.asyncio
async def test_what_happened_while_released_is_counted_from_the_position_at_the_release(tmp_path, monkeypatch):
    """1.0.5 WS8g: after the journal id recorded at the release -- never
    before it -- the transfers completed and failed, each once, and what needs
    attention by meaning: a transfer waiting on the operator or with an error
    (a failure's own causes are that failure, said once; a completion heals only
    its delivery, so a failed post-processing or an abandoned provider cleanup
    still counts), and each other error once per subject. The events are the
    journal's own builders (transfers.journal_events)."""
    from db import database, event_journal
    from db.event_journal import JournalEvent
    from transfers import journal_events as je
    from types import SimpleNamespace

    from transfers.errors import Category, Domain, NormalizedError, Stage
    from transfers.models import TransferState
    unpack = NormalizedError(Domain.POST_PROCESSING, Category.EXTRACTION_FAILED, Stage.POST_PROCESSING)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "journal.sqlite")
    await database.init_db()

    def done(transfer):
        return je.lifecycle(transfer, "verifying", TransferState.COMPLETED)

    def failed(transfer):
        return je.lifecycle(transfer, "transferring", TransferState.FAILED)

    def plain(kind, severity, *, transfer=None, category="transfer", subject=None, outcome=None):
        return JournalEvent(category, kind, severity, kind, "transfer" if transfer else "service",
                            subject_id=subject if subject is not None else transfer, transfer_id=transfer, outcome=outcome)

    async def record(*events):
        async with database.get_db() as db:
            for item in events:
                assert await event_journal.record(db, item)
            await db.commit()
            return await event_journal.latest_id(db)

    async def summary(since):
        async with database.get_db() as db:
            return await event_journal.away_summary(db, since)

    # Before the release: none of this is counted.
    since = await record(plain("transfer.completed", "info", transfer=1), plain("transfer.failed", "error", transfer=2),
                         plain("storage.unavailable", "error", category="storage", subject="downloads"))
    assert await summary(since) == {"completed": 0, "failed": 0, "attention": 0, "through": since}
    through = await record(
        done(3), done(4), done(4),                                                          # the same transfer again: once
        # One failure and its own causes: said once, as the failure.
        je.recovery_quiesced(transfer_id=5, artifact_id=50, reason="recovery_exhausted"),
        je.recovery_decided(transfer_id=5, artifact_id=50, decision_id=1, action="fail_permanently", reason="exhausted"),
        plain("execution.file_failed", "error", transfer=5, category="execution"),
        failed(5),
        # A transfer waiting for the operator's input (a warning): attention, once however often it asks.
        je.input_requested(transfer_id=6, challenge_id=60, generation=1, integration_id="usenet", message="Password needed"),
        je.input_requested(transfer_id=6, challenge_id=60, generation=2, integration_id="usenet", message="Password needed"),
        # A recovery that waits for the operator: attention.
        je.recovery_decided(transfer_id=7, artifact_id=70, decision_id=2, action="wait_for_operator", reason="input"),
        # An error on a transfer that then completed: said by the completion.
        plain("execution.file_failed", "error", transfer=8, category="execution"), done(8),
        # A transfer that entered input-required (its lifecycle, info): attention.
        je.lifecycle(10, "resolving", TransferState.INPUT_REQUIRED),
        # Errors a completion does not heal: post-processing that failed before it, and a
        # provider cleanup abandoned after it -- each its transfer, once.
        je.extraction("finished", transfer_id=11, processor_id="extract", outcome=SimpleNamespace(error=unpack, kind="failed")),
        done(11),
        done(12),
        je.resource_cleanup_abandoned(transfer_id=12, binding_id=120, provider_id="realdebrid", error=unpack, token="t"),
        # A routine retry (a warning, no operator action): not counted.
        je.recovery_decided(transfer_id=9, artifact_id=90, decision_id=3, action="retry_same_candidate", reason="timeout"),
        # Another error, twice for the same subject: once.
        plain("storage.unavailable", "error", category="storage", subject="downloads"),
        plain("storage.unavailable", "error", category="storage", subject="downloads"))
    assert await summary(since) == {"completed": 5, "failed": 1, "attention": 6, "through": through}
