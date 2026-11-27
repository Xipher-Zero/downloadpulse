"""WS27: an update's installer, downloaded through DownloadPulse, is the bytes
its release names -- never other material.

The real engine, the real ``multimeta`` and ``general_http`` providers and
the real verifier, with the one-file Metalink4 the update owner builds
(``application.updates.metalink``); only byte acquisition is an in-memory
copier, which writes exactly the bytes its origin object holds (so sampled
evidence and written material agree, as they do over the network).

* A single-file descriptor takes the FILE plan, carries the record's SHA-256
  as the routed candidate's integrity, and a mismatch is the canonical
  integrity failure (``integrity_failure``) with the rejected bytes gone.
* Material an earlier, hashless transfer of the same address left -- however
  it was tampered with -- never completes the update.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
import pytest_asyncio

from test_v113_multimeta_end_to_end import MirrorCopier, _ticks
from test_v113_transfer_auth_context import lab  # noqa: F401
from test_v113_transport_evidence_sampling import executor_for, guard_for, loopback  # noqa: F401
from transfers.models import MaterializationKind, TransferRequest, TransferState
from transfers.staged_input import StagedInputStore

pytestmark = pytest.mark.asyncio

GOOD, BAD = b"good", b"bad!"   # the same size: only the bytes differ
GOOD_SHA256 = hashlib.sha256(GOOD).hexdigest()
URL = "https://github.com/Xipher-Zero/downloadpulse/releases/download/v9.9.9/DownloadPulse-9.9.9-linux-x86_64.flatpak"
NAME = URL.rsplit("/", 1)[1]
KEY = "github.com" + URL.split("github.com", 1)[1]


class ObjectCopier(MirrorCopier):
    """Writes the origin object's own bytes, as a real download would."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.candidates = {}   # attempt id -> the candidate it writes

    async def start(self, request, handle):
        self.candidates[handle.attempt_id] = request.work.subject.candidate
        return await super().start(request, handle)

    def finish(self, handle, *, materialize=True):
        super().finish(handle, materialize=materialize)
        if materialize:
            current = self.jobs[handle.attempt_id].handle
            Path(current.correlation["destination"]).write_bytes(
                self.objects[self._object(self.candidates[handle.attempt_id])])


@pytest_asyncio.fixture
async def stage(lab, loopback, tmp_path):  # noqa: F811
    from providers.general_http.provider import GeneralHttpProvider
    from providers.multimeta.provider import MultimetaProvider
    repository, registry, engine, *_rest, now = lab
    store = StagedInputStore(str(tmp_path / "staged"))
    registry.register_provider(GeneralHttpProvider())
    registry.register_provider(MultimetaProvider(staged_input=store))
    copier = ObjectCopier(repository.authorize_execution, reader=executor_for(tmp_path, guard_for()),
                          objects={KEY: GOOD})
    registry.register_executor(copier)
    yield repository, engine, copier, store, now


def _update_request(store, sha256=GOOD_SHA256):
    from application.updates import metalink
    document = metalink({"name": NAME, "url": URL, "sha256": sha256, "size": len(GOOD)})
    return TransferRequest("meta4", store.stage_bytes(document), name=f"{NAME}.meta4")


async def _artifact(repository, transfer_id):
    return [item for item in await repository.artifacts(transfer_id)][0]


async def test_the_installer_takes_the_file_plan_with_the_records_hash_and_verifies(stage):
    repository, engine, copier, store, now = stage
    transfer = await engine.submit((_update_request(store),), name=NAME)
    await _ticks(engine, repository, transfer.id, now)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    artifact = await _artifact(repository, transfer.id)
    candidate = artifact.candidates[artifact.selected]
    # The routed candidate's integrity is exactly the record's SHA-256 ...
    assert [(item.algorithm, item.digest) for item in candidate.integrity] == [("sha256", GOOD_SHA256)]
    # ... its one source is the github.com address, never where it redirects to ...
    assert [endpoint.address for endpoint in candidate.endpoints] == [URL]
    # ... and one file is one FILE materialization (filesystem.verify_materialization's FILE plan).
    assert engine._work(artifact, candidate).materialization.kind == MaterializationKind.FILE
    assert Path(artifact.target).read_bytes() == GOOD


async def test_a_tampered_installer_is_the_canonical_integrity_failure(stage):
    repository, engine, copier, store, now = stage
    copier.objects[KEY] = BAD
    transfer = await engine.submit((_update_request(store),), name=NAME)
    await _ticks(engine, repository, transfer.id, now)
    assert (await repository.get(transfer.id)).state == TransferState.FAILED
    artifact = await _artifact(repository, transfer.id)
    assert engine._work(artifact, artifact.candidates[artifact.selected]).materialization.kind == \
        MaterializationKind.FILE
    context = await repository.recovery_context(artifact.id)
    assert (context["decision_action"], context["decision_reason"]) == ("fail_permanently", "integrity_failure")
    # The verifier's rejection: an integrity-domain materialization failure ...
    assert (artifact.error.domain.value, artifact.error.category.value) == ("integrity", "materialization_failed")
    # ... whose bytes are never handed over.
    assert not Path(artifact.target).exists()


@pytest.mark.parametrize("tamper", ["served", "local"])
async def test_hashless_material_of_the_same_address_never_completes_the_update(stage, tamper):
    """STOP condition (WS27 REVISION 6): a completed, hashless transfer of the
    same address holds tampered bytes -- served so (the origin itself returned
    them, so its evidence matches the material) or changed on disk after it
    completed. The update must not complete from that material."""
    repository, engine, copier, store, now = stage
    copier.objects[KEY] = BAD if tamper == "served" else GOOD
    plain = await engine.submit((TransferRequest("https", URL, name=NAME),), name=NAME)
    await _ticks(engine, repository, plain.id, now)
    assert (await repository.get(plain.id)).state == TransferState.COMPLETED
    seeded = Path((await _artifact(repository, plain.id)).target)
    if tamper == "local":
        seeded.write_bytes(BAD)
    assert seeded.read_bytes() == BAD
    started = len(copier.started)

    update = await engine.submit((_update_request(store),), name=NAME)
    assert update.id != plain.id
    await _ticks(engine, repository, update.id, now)
    state = (await repository.get(update.id)).state
    artifact = await _artifact(repository, update.id)
    if tamper == "local":
        # The origin still serves the release's bytes: the update downloads
        # them again and verifies them; the tampered copy answered nothing.
        assert state == TransferState.COMPLETED
        assert len(copier.started) == started + 1
        assert Path(artifact.target).read_bytes() == GOOD
    else:
        # Nothing anywhere holds the release's bytes: the update fails, by the
        # verifier, and never completes from the seeded material.
        assert state == TransferState.FAILED
        context = await repository.recovery_context(artifact.id)
        assert context["decision_reason"] == "integrity_failure"


# ── the desktop route over the real engine (WS27 G9-R) ──────────────────────
# Every state below is reached through public paths: the window's own click
# (POST /api/desktop/update/download), the engine's own lifecycle, Delete.

WINDOW_PORT = 47815


@pytest_asyncio.fixture
async def window(stage, monkeypatch, tmp_path):
    import sys as _sys

    import httpx
    import main
    import core.config as config
    from account_support import account
    from application import updates
    from application.service import ApplicationService
    from auth.accounts import DEFAULT_ADMIN_ID, Role
    from auth.models import Principal
    from auth.sessions import HTTP_SESSION_COOKIE, session_store

    repository, engine, copier, store, now = stage
    monkeypatch.setenv("DOWNLOADPULSE_DESKTOP", "1")
    monkeypatch.setenv("DOWNLOADPULSE_DESKTOP_PORT", str(WINDOW_PORT))
    monkeypatch.setenv("FLATPAK_ID", "io.github.xipher_zero.DownloadPulse")
    monkeypatch.setattr(_sys, "platform", "linux")
    monkeypatch.setattr(updates._platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(config, "_settings", config.AppSettings(auth_password_enabled=True))
    monkeypatch.setattr(updates, "_record", None)
    monkeypatch.setattr(updates, "_download", None)
    monkeypatch.setattr(updates, "read_version", lambda: "1.0.3")
    application = ApplicationService(engine)
    application.staged_input = store
    monkeypatch.setitem(main.app.state._state, "application", application)
    session_store.clear()
    token, _ = session_store.create(Principal.password_session(account("admin", account_id=DEFAULT_ADMIN_ID,
                                                                       role=Role.ADMIN)), lifetime_seconds=3600)
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)

    async def call(method, path):
        async with httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{WINDOW_PORT}") as client:
            response = await client.request(method, path, headers={
                "Cookie": f"{HTTP_SESSION_COOKIE}={token}", "X-CSRF-Token": session_store.csrf_token(token)})
        return response.status_code, response.json()

    async def fetch(_etag):
        return 200, "", {"tag_name": "v9.9.9", "html_url": "https://github.com/Xipher-Zero/downloadpulse/releases/tag/v9.9.9",
                         "published_at": "2026-12-01", "draft": False, "prerelease": False,
                         "assets": [{"name": NAME, "size": len(GOOD), "digest": f"sha256:{GOOD_SHA256}",
                                     "browser_download_url": URL}]}

    async def no_releases():
        return None    # the release notes' list: never the network here
    monkeypatch.setattr(updates, "_fetch", fetch)
    monkeypatch.setattr(updates, "_fetch_releases", no_releases)
    assert (await updates.refresh())[0]["installer"]["sha256"] == GOOD_SHA256

    async def click():
        status, body = await call("POST", "/api/desktop/update/download")
        assert status == 200, body
        return body["id"]

    yield SimpleNamespace(repository=repository, engine=engine, copier=copier, now=now, call=call, click=click,
                          application=application)
    session_store.clear()


from types import SimpleNamespace  # noqa: E402


async def _status(window, transfer_id):
    return (await window.repository.presentation(transfer_id))["presentation_status"]


async def test_a_completed_installer_still_on_disk_is_reused_and_a_removed_one_is_downloaded_again(window):
    first = await window.click()
    await _ticks(window.engine, window.repository, first, window.now)
    assert await _status(window, first) == "completed"
    target = Path((await _artifact(window.repository, first)).target)
    assert target.read_bytes() == GOOD
    # Completed, its file present (core's completed_location answers): the same transfer.
    assert await window.click() == first
    # Its file removed from the download folder: a new lineage.
    target.unlink()
    second = await window.click()
    assert second != first
    await _ticks(window.engine, window.repository, second, window.now)
    assert await _status(window, second) == "completed"
    # Deleted (the person's own Delete): a new lineage again.
    status, _body = await window.call("DELETE", f"/api/torrents/{second}")
    assert status == 200
    third = await window.click()
    assert third not in (first, second)


async def test_a_live_download_is_returned_and_concurrent_clicks_start_one(window):
    first, again = await asyncio.gather(window.click(), window.click())
    assert first == again
    assert await window.click() == first        # still live: the same transfer


async def test_after_an_integrity_failure_a_click_starts_a_new_lineage(window):
    window.copier.objects[KEY] = BAD
    first = await window.click()
    await _ticks(window.engine, window.repository, first, window.now)
    assert await _status(window, first) == "failed"
    second = await window.click()
    assert second != first


async def test_a_download_whose_source_is_gone_does_not_hold_the_click(window):
    # The release's only source answers "not found": the transfer ends failed.
    window.copier.broken.add("github.com")
    first = await window.click()
    await _ticks(window.engine, window.repository, first, window.now)
    assert await _status(window, first) == "failed"
    window.copier.broken.clear()
    second = await window.click()
    assert second != first


@pytest.mark.parametrize("status,serves", [
    ("downloading", True), ("queued", True), ("paused", True), ("pending", True),
    ("requires_attention", False), ("input_required", False), ("failed", False), ("cancelled", False),
    ("consolidated", False), ("deleted", False),
])
async def test_which_earlier_download_still_answers_a_click(status, serves):
    """A download waiting on the person (requires_attention, input_required)
    is treated like a failure: the click starts a new download."""
    from api.routes import _update_download_serves

    class Repository:
        async def presentation(self, _id):
            return {"presentation_status": status}

    assert await _update_download_serves(SimpleNamespace(repository=Repository()), 7) is serves
