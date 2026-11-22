"""1.0.2 (operator, 2026-11-22): one exact object is never acquired twice.

Reported: a link was sent, started, paused and left in the queue; sending the same
link again started a NEW transfer that downloaded from scratch. Two causes:

* the General HTTP(S) provider stated no object coordinate, so two candidates of
  the very same link were "non-independent sources" and never even compared --
  they now share one coordinate (the exact request target), which makes them
  PAIRABLE for the ordinary material proof, never equivalent by address alone;
* a later claim on a paused file left it parked. Now the claim resumes it from its
  own material; when the claim is another account's, the owner's journal records
  that another account's request resumed it (provenance). A global pause and an
  external client's advisory hold are left as they are.
"""
from __future__ import annotations

import pytest

import db.database as database
from test_canonical_same_source_and_late_completed_equivalence import (  # noqa: F401 -- fixture
    BYTES, _material, _origin_requests, _started, _ticks, _until, world)
from transfers.models import SubmissionDemand, TransferRequest, TransferState
from transfers.requests import remote_object_coordinate

pytestmark = pytest.mark.asyncio

URL = "https://mirror-a.example/file.bin"


def _send(engine, url=URL, account="una"):
    return engine.submit((TransferRequest(url.split(":", 1)[0], url),), deduplicate=False,
                         demand=SubmissionDemand(account))


async def _attached(repository, engine, artifact_id, transfer):
    requests = {record.id for record in await repository.requests(transfer.id)}
    return bool(requests & await _origin_requests(engine, artifact_id))


async def _journal(transfer_id):
    async with database.get_db() as db:
        return [row["event_type"] for row in await db.fetchall(
            "SELECT event_type FROM event_journal WHERE transfer_id=? ORDER BY id", (int(transfer_id),))]


async def _completed(repository, transfer_id):
    return (await repository.get(transfer_id)).state == TransferState.COMPLETED


async def _first_started(world, account="una"):
    repository, engine, executor, now = world
    executor.objects |= {"mirror-a.example/file.bin": BYTES}
    first = await _send(engine, account=account)
    assert await _until(engine, now, lambda: _started(executor))
    [artifact] = await _material(repository, [first.id])
    return first, artifact


async def _artifacts(repository, *transfers):
    return [row["id"] for row in await _material(repository, [transfer.id for transfer in transfers])]


async def test_the_same_link_sent_again_joins_the_one_download(world):
    repository, engine, executor, now = world
    first, artifact = await _first_started(world)
    second = await _send(engine)
    await _ticks(engine, now, 12)
    assert await _attached(repository, engine, artifact["id"], second)
    assert len(executor.writers()) == 1
    assert (await repository.get(second.id)).state == TransferState.CONSOLIDATED


async def test_sent_again_by_the_same_account_resumes_the_paused_download(world):
    repository, engine, executor, now = world
    first, artifact = await _first_started(world)
    await engine.pause(first.id)
    await _ticks(engine, now, 3)
    second = await _send(engine)
    await _ticks(engine, now, 12)
    assert await _attached(repository, engine, artifact["id"], second)
    assert (await repository.get(first.id)).state != TransferState.PAUSED
    # ONE object: every writer ever started is the first transfer's one artifact (resumed), none of the second's.
    assert await _artifacts(repository, first, second) == [artifact["id"]]
    assert "consolidation.resumed_for_claim" in await _journal(first.id)


async def test_another_account_resumes_it_and_the_owner_s_journal_says_so(world):
    repository, engine, executor, now = world
    first, artifact = await _first_started(world, account="una")
    await engine.pause(first.id)
    await _ticks(engine, now, 3)
    second = await _send(engine, account="bo")
    await _ticks(engine, now, 12)
    assert await _attached(repository, engine, artifact["id"], second)
    assert (await repository.get(first.id)).state != TransferState.PAUSED
    assert await _artifacts(repository, first, second) == [artifact["id"]]
    # Each keeps its own transfer and claim; the owner's record says another account resumed it.
    assert "consolidation.resumed_for_other_account" in await _journal(first.id)
    executor.finish_all()
    assert await _until(engine, now, lambda: _completed(repository, first.id), finishing=executor)


async def test_a_global_pause_is_never_lifted_by_a_new_claim(world):
    repository, engine, executor, now = world
    first, _artifact = await _first_started(world)
    await engine.pause(first.id)
    await repository.global_pause(True)
    await _send(engine)
    await _ticks(engine, now, 12)
    assert (await repository.get(first.id)).state == TransferState.PAUSED


@pytest.mark.parametrize("address, coordinate", [
    ("https://Mirror-A.example/pub/file.iso", "https://mirror-a.example:443/pub/file.iso"),
    ("http://h.example:8080/dl?id=7&x=1", "http://h.example:8080/dl?id=7&x=1"),
    ("https://h.example/file.iso#part", "https://h.example:443/file.iso"),
    ("https://user:pw@h.example/file.iso", ""),
])
def test_an_http_link_s_coordinate_is_its_exact_request_target(address, coordinate):
    assert remote_object_coordinate(address) == coordinate


def test_different_query_strings_are_different_objects():
    assert remote_object_coordinate("https://h.example/dl?id=1") != remote_object_coordinate("https://h.example/dl?id=2")
