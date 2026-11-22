"""1.0.2 (operator, 2026-11-22): a connected browser's Pause and Resume are its person's.

Reported: once a second account's send had joined the first account's download, the
browser's controls refused -- "Other requests need this download, so it can't be
paused from here", and for a download paused in DownloadPulse "resume it there".
In DownloadPulse either person may pause or resume any download, a shared one
included; the people sort a disagreement out between them. A browser acts for the
person who connected it, so it may do the same. Everything paused at once stays
an administrator's to lift, and an Integration (no person) keeps its advisory hold.
"""
from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

import db.database as database
from auth.models import AuthMechanism, Principal
from test_canonical_same_source_and_late_completed_equivalence import (  # noqa: F401 -- fixture
    _ticks, world)
from test_v102_same_object_claims import _first_started, _send
from transfers.models import TransferState

pytestmark = pytest.mark.asyncio


def _person(account, kind="browser", role="user"):
    return Principal(authenticated=True, mechanism=AuthMechanism.API_TOKEN, account_id=account, role=role,
                     credential_id=f"t-{account}", client_id=f"c-{account}", credential_kind=kind)


@pytest.fixture
def clients(world, tmp_path, monkeypatch):
    """``ExternalClients`` over the world's engine, deciding through core's own
    ``ApplicationService.adjudicated_control`` (the operator-control boundary)."""
    import core.config as config
    from application.service import ApplicationService
    from external.service import ExternalClients

    repository, engine, _executor, _now = world
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    config.apply_settings(config.AppSettings(download_folder=str(tmp_path / "payloads")))

    async def published(_transfer_id):
        return None

    core = SimpleNamespace(engine=engine, repository=repository, operator_controls=asyncio.Lock(),
                           application_operation=contextlib.nullcontext, resolution_wakeup=asyncio.Event(),
                           execution_wakeup=asyncio.Event(), _publish=published,
                           integrations_starting=lambda: False)
    core.adjudicated_control = lambda transfer_id, decide: ApplicationService.adjudicated_control(
        core, transfer_id, decide)
    core.serving_transfer = lambda transfer_id: ApplicationService.serving_transfer(core, transfer_id)
    return ExternalClients(core)


async def _job(transfer, kind="browser"):
    async with database.get_db() as db:
        demand = await db.fetchone("SELECT id FROM transfer_demands WHERE transfer_id=?", (int(transfer.id),))
    return {"id": f"job-{transfer.id}", "demand_id": demand["id"], "client_id": "c", "kind": kind}


async def _control(clients, transfer, principal, kind, *, job_kind="browser"):
    decision = await clients._adjudicate(await _job(transfer, job_kind), principal, f"k-{kind}", kind, "ctl")
    return decision["disposition"], decision["reason"]


async def _paused(repository, transfer):
    hold = await repository.pause_hold(transfer.id)
    return bool(hold and hold["paused"]) or (await repository.get(transfer.id)).state == TransferState.PAUSED


async def _shared(world):
    """Una's download, joined by Bo's send of the same link."""
    repository, engine, _executor, now = world
    first, _artifact = await _first_started(world, account="una")
    second = await _send(engine, account="bo")
    await _ticks(engine, now, 12)
    assert (await repository.get(second.id)).state == TransferState.CONSOLIDATED
    return first, second


async def test_either_person_pauses_and_resumes_the_shared_download_from_a_browser(world, clients):
    repository, *_ = world
    first, second = await _shared(world)
    # Bo pauses the download Una's request also needs -- as Bo could in DownloadPulse.
    assert await _control(clients, second, _person("bo"), "pause") == ("honored", "paused")
    assert await _paused(repository, first)
    assert not (await repository.pause_hold(first.id))["holder"]      # the person's own Pause, no client hold
    # Una resumes it from hers; and Bo may pause and resume it again.
    assert await _control(clients, first, _person("una"), "resume") == ("honored", "resumed")
    assert not await _paused(repository, first)
    assert await _control(clients, second, _person("bo", role="admin"), "pause") == ("honored", "paused")
    assert await _control(clients, second, _person("bo"), "resume") == ("honored", "resumed")
    assert not await _paused(repository, first)


async def test_a_download_paused_in_downloadpulse_is_resumed_from_a_browser(world, clients):
    repository, engine, *_ = world
    first, second = await _shared(world)
    await engine.pause(first.id)                                      # paused in DownloadPulse
    assert await _control(clients, second, _person("bo"), "resume") == ("honored", "resumed")
    assert not await _paused(repository, first)
    assert await _control(clients, second, _person("bo"), "resume") == ("honored", "not_paused")


async def test_everything_paused_at_once_is_never_lifted_from_a_browser(world, clients):
    repository, engine, *_ = world
    first, second = await _shared(world)
    await engine.pause_all()
    assert await _control(clients, second, _person("bo"), "resume") == ("declined", "globally_paused")
    assert await _control(clients, first, _person("una"), "resume") == ("declined", "globally_paused")
    assert await repository.globally_paused()
    assert await _paused(repository, first)


async def test_an_integration_keeps_its_advisory_hold(world, clients):
    repository, *_ = world
    first, second = await _shared(world)
    integration = _person("bo", kind="integration", role="integration")
    assert await _control(clients, second, integration, "pause", job_kind="integration") == (
        "declined", "shared_interest")
    assert not await _paused(repository, first)


async def test_the_browser_that_paused_it_says_so(world, clients):
    """``paused_by`` is ``client`` while the standing pause is still the one this
    browser's person applied; a pause from anywhere else is DownloadPulse's."""
    repository, engine, *_ = world
    first, second = await _shared(world)
    bo, job = _person("bo"), await _job(second)
    async with database.get_db() as db:
        await db.execute(
            """INSERT INTO external_jobs(id,client_id,account_id,credential_id,idempotency_key,intent_digest,
                   source_kind,source_summary,category,profile_id,profile_snapshot,reserved_demand_id,demand_id,
                   delivery_id,created_at,updated_at,kind)
               VALUES(?,'c','bo','t-bo','k','d','links','{}','','','{}','r',?,'dlv',0,0,'browser')""",
            (job["id"], job["demand_id"]))
        await db.commit()
    decision = await clients._adjudicate(job, bo, "k1", "pause", "ctl1")
    await clients._record_control(job, bo, "k1", "pause", decision, "ctl1")
    assert (await clients._plan(job)).projection["paused_by"] == "client"
    await engine.resume(first.id)
    await engine.pause(first.id)                                      # paused again, in DownloadPulse
    assert (await clients._plan(job)).projection["paused_by"] == "operator"
