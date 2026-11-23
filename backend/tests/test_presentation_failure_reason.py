"""The failure a row's status names, from the one selection owner.

``transfers.presentation_repository.failure_selection`` picks the failure a
transfer's reason names -- its displayed error, else a source whose resolution
retry is scheduled, else a held file whose acquisition failed -- with only a
real schedule. Details' Reason (``api.routes._error_reason``) and both
presentation projections (the bounded Downloads/Dashboard list and Details)
select through it, so the list and Details carry the same
``presentation_error_category`` and ``presentation_retry_at``, and Details'
Reason names the same schedule. Real temporary SQLite, real repository, real
list and Details routes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

import api.operational_downloads as downloads
import db.database as database
from api.routes import get_torrent
from fake_integrations import MemoryExecutor, ParcelProvider
from integrations.catalog import definitions
from integrations.media.outcomes import outcome_error
from transfers import codec
from transfers.convergence_engine import TransferEngine
from transfers.errors import Stage
from transfers.models import ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.presentation_repository import effective_presentation, failure_selection
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

# Declared words exist for this error (yt_dlp's verification challenge), so
# Details' Reason is produced and its schedule can be compared.
CHALLENGE = outcome_error("verification_challenge", Stage.EXECUTION, integration_id="yt_dlp")
CATEGORY = CHALLENGE.category.value
RETRY_AT = 1_900_000_060.0


# -- the one selection rule ------------------------------------------------------------

def test_the_selection_prefers_the_displayed_error_then_a_scheduled_source_then_a_held_file():
    requests = [("resolved", "x", 5.0), ("pending", "", 9.0), ("pending", "source", 0), ("pending", "late", 7.0)]
    artifacts = [("completed", "old"), ("error", "settled"), ("queued", "held"), ("queued", "second")]
    assert failure_selection("shown", requests, artifacts, "retry_at:1.5") == ("shown", None)
    assert failure_selection(None, requests, artifacts, "retry_at:1.5") == ("late", 7.0)
    assert failure_selection(None, requests[:3], artifacts, "retry_at:1.5") == ("held", 1.5)
    for wake in (None, "operator_retry", "retry_at:not-a-time"):
        assert failure_selection(None, (), artifacts, wake) == ("held", None)
    assert failure_selection(None, requests[:3], artifacts[:2], "retry_at:1.5") is None


def test_the_presentation_names_no_failure_once_settled_or_without_failure_facts():
    held = (None, [], [("queued", CATEGORY)])
    settled = effective_presentation("completed", [], failure=held)
    assert settled["presentation_error_category"] is None and settled["presentation_retry_at"] is None
    assert "presentation_error_category" not in effective_presentation("queued", [])


# -- list == Details, and Details' Reason names the same schedule ------------------------

@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "failure-reason.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider("parcel-lab", file_manifest=True)
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=0, resolution_retry_delay=0, adoption_stability_seconds=0,
                              resource_poll_interval=5, max_active_executions=8),
    )
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, provider=provider)


async def _submit(runtime, payload):
    return await runtime.engine.submit(
        (TransferRequest("parcel", payload, name="Show S01", fingerprint="fp-" + payload),), deduplicate=False)


async def _with_artifact(runtime, payload):
    runtime.provider.responses.append(runtime.provider.parcel(
        payload, state=ResourceState.AVAILABLE, files=[("a.mkv", "S/a.mkv", 10)]))
    transfer = await _submit(runtime, payload)
    for _ in range(6):
        await runtime.engine.resolve_pending()
    artifacts = await runtime.repository.artifacts(transfer.id)
    assert artifacts
    return transfer.id, artifacts[0].id


async def _write(sql, params):
    async with database.get_db() as db:
        await db.execute(sql, params)
        await db.commit()


async def _hold(transfer_id, artifact_id, **recovery):
    await _write("UPDATE torrents SET status='queued',normalized_error=NULL,error_message=NULL WHERE id=?",
                 (transfer_id,))
    await _write("UPDATE download_files SET status='queued',normalized_error=? WHERE id=?",
                 (codec.dump(CHALLENGE), artifact_id))
    await _write("DELETE FROM artifact_recovery_state WHERE artifact_id=?", (artifact_id,))
    await _write("INSERT INTO artifact_recovery_state(artifact_id,transfer_id,decision_action,decision_reason,"
                 "quiescence_reason,wake_condition) VALUES(?,?,?,?,?,?)",
                 (artifact_id, transfer_id, recovery.get("action"), recovery.get("reason"),
                  recovery.get("quiescence"), recovery.get("wake")))


async def _surfaces(runtime, transfer_id):
    listed = await downloads.list_operational_torrents(
        status=None, search=None, limit=0, offset=0, application=SimpleNamespace(repository=None, definitions=[]))
    item = next(item for item in listed["items"] if item["id"] == transfer_id)
    details = await get_torrent(transfer_id, application=SimpleNamespace(
        repository=runtime.repository, definitions=definitions, engine=None))
    for key in ("presentation_status", "presentation_error_category", "presentation_retry_at"):
        assert item[key] == details[key], key
    return item, details


@pytest.mark.asyncio
async def test_a_failed_transfer_names_its_displayed_error_on_the_list_and_in_details(runtime):
    transfer_id, artifact_id = await _with_artifact(runtime, "failed")
    await _write("UPDATE torrents SET status='error',normalized_error=?,error_message=? WHERE id=?",
                 (codec.dump(CHALLENGE), CHALLENGE.message, transfer_id))
    await _write("UPDATE download_files SET status='error' WHERE id=?", (artifact_id,))
    item, details = await _surfaces(runtime, transfer_id)
    assert item["presentation_status"] == "failed"
    assert item["presentation_error_category"] == CATEGORY and item["presentation_retry_at"] is None
    assert details["error_reason"]["scheduled_retry_at"] is None
    # The list still carries no disclosure: only the category crosses.
    assert "error_disclosure" not in item and "error_disclosure" in details


@pytest.mark.asyncio
async def test_a_file_held_for_the_operator_names_its_failure_without_a_schedule(runtime):
    transfer_id, artifact_id = await _with_artifact(runtime, "attention")
    await _hold(transfer_id, artifact_id, action="wait_for_operator", reason="retry_budget_exhausted",
                wake="operator_retry")
    item, details = await _surfaces(runtime, transfer_id)
    assert item["presentation_status"] == "requires_attention"
    assert item["presentation_error_category"] == CATEGORY and item["presentation_retry_at"] is None
    assert details["error_reason"]["scheduled_retry_at"] is None


@pytest.mark.asyncio
async def test_a_file_waiting_for_retry_names_its_failure_and_its_real_schedule(runtime):
    transfer_id, artifact_id = await _with_artifact(runtime, "waiting")
    await _hold(transfer_id, artifact_id, action="retry", quiescence="retry_backoff", wake=f"retry_at:{RETRY_AT}")
    item, details = await _surfaces(runtime, transfer_id)
    assert item["presentation_status"] == "waiting_for_retry"
    assert item["presentation_error_category"] == CATEGORY and item["presentation_retry_at"] == RETRY_AT
    assert details["error_reason"]["scheduled_retry_at"] == RETRY_AT


@pytest.mark.asyncio
async def test_a_source_waiting_for_its_resolution_retry_names_that_failure_and_schedule(runtime):
    transfer = await _submit(runtime, "resolution")
    await _write("UPDATE transfer_requests SET state='pending',error=?,retry_at=? WHERE transfer_id=?",
                 (codec.dump(CHALLENGE), 1_900_000_000.5, transfer.id))
    item, details = await _surfaces(runtime, transfer.id)
    assert item["presentation_error_category"] == CATEGORY and item["presentation_retry_at"] == 1_900_000_000.5
    assert details["error_reason"]["scheduled_retry_at"] == 1_900_000_000.5


@pytest.mark.asyncio
async def test_a_transfer_without_a_failure_names_none(runtime):
    transfer_id, _artifact_id = await _with_artifact(runtime, "clean")
    item, details = await _surfaces(runtime, transfer_id)
    assert item["presentation_error_category"] is None and item["presentation_retry_at"] is None
    assert "error_reason" not in details


@pytest.mark.asyncio
async def test_retry_and_recovery_read_one_canonical_status_on_the_list_and_in_details(runtime):
    """WS10: the list and Details say one status for one state -- a file in a
    retry back-off is "Waiting for retry" on both, whatever its raw state or a
    held recovery claim; a file being picked up again is "Recovering" on both.
    Both surfaces word it by that status (``status.<presentation_status>``)."""
    transfer_id, artifact_id = await _with_artifact(runtime, "retry-label")
    for raw in ("queued", "recovery_wait", "refresh_pending"):
        await _hold(transfer_id, artifact_id, action="retry", quiescence="retry_backoff", wake=f"retry_at:{RETRY_AT}")
        await _write("UPDATE download_files SET status=? WHERE id=?", (raw, artifact_id))
        await _write("UPDATE artifact_recovery_state SET recovery_claim_token='claim' WHERE artifact_id=?", (artifact_id,))
        item, details = await _surfaces(runtime, transfer_id)
        assert item["presentation_status"] == details["presentation_status"] == "waiting_for_retry", raw
        assert item["presentation_label"] == details["presentation_label"] == "Waiting for retry", raw
    await _hold(transfer_id, artifact_id, action="retry")
    await _write("UPDATE download_files SET status='refresh_pending' WHERE id=?", (artifact_id,))
    item, details = await _surfaces(runtime, transfer_id)
    assert item["presentation_status"] == details["presentation_status"] == "recovering"
    assert item["presentation_label"] == details["presentation_label"] == "Recovering"
