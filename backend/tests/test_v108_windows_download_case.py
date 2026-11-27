"""1.0.8 (WS-F1): on Windows, a finished aria2 download keeps its own name.

aria2 on Windows reports its file with "/" separators, and Windows paths compare
without case, so ``Aria2Executor._observation`` compares the reported path with
the planned target case-insensitively. Until 1.0.8 it then also *reported* the
lowercased path as the file it produced; core's verification compares that name
exactly with the plan's target, so every download whose name (or folder below
the download root) had a capital letter was rejected and its data deleted after
it finished -- the in-app update's installer included. The comparison alone
ignores case; the reported file is the target's own name.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from execution_requests import file_request
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from test_aria2_executor_contract import NativeDaemon
from transfers.filesystem import verify_materialization
from transfers.models import Endpoint, ExecutionState, TransferCandidate

class _WindowsOs:
    name = "nt"

    def __getattr__(self, attribute):
        return getattr(os, attribute)


NAME = "DownloadPulse-1.0.8-windows-x64.exe"
SIZE = 100


def _execution(tmp_path, monkeypatch, target):
    async def validated(address):
        return address
    monkeypatch.setattr("executors.aria2.executor.validate_resolved_public_destination", validated)
    daemon = NativeDaemon()
    grants = {}

    async def authorize(handle, action):
        return grants.get(handle.attempt_id) == handle

    egress = SimpleNamespace(ensure_started=AsyncMock(),
                             job_options=lambda address, scope=None, budget=None: {"all-proxy": "http://guard:8888"})
    executor = Aria2Executor(daemon, Aria2Configuration(str(tmp_path), confirmation_delay=0,
                                                        control_confirmation_timeout=0.02), authorize, egress=egress)
    candidate = TransferCandidate("payload", (Endpoint("https", "https://download.example/file"),), expected_bytes=SIZE)
    request = file_request(candidate, str(target), "durable-attempt", root=tmp_path)
    handle = executor.prepare(request)
    grants[handle.attempt_id] = handle
    return SimpleNamespace(executor=executor, daemon=daemon, request=request, handle=handle)


async def _finished(execution, reported: str, monkeypatch):
    await execution.executor.start(execution.request, execution.handle)
    target = Path(execution.request.work.materialization.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * SIZE)
    native = execution.daemon.jobs[execution.handle.native["gid"]]
    native.status, native.total_length, native.completed_length = "complete", SIZE, SIZE
    native.files = [{"path": reported}]
    # The Windows branch of _observation, for this module only (pathlib itself
    # stays POSIX here: the comparison is ntpath's, as on Windows).
    monkeypatch.setattr("executors.aria2.executor.os", _WindowsOs())
    return await execution.executor.observe(execution.handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["", "Installers/Desktop"])
async def test_a_windows_download_with_capitals_is_reported_by_its_own_name_and_verifies(tmp_path, monkeypatch, folder):
    target = (tmp_path / folder / NAME).resolve()
    execution = _execution(tmp_path, monkeypatch, target)
    # aria2 on Windows: "/" separators, and here a different case than the plan.
    reported = str(target).replace(os.sep, "/").lower()
    result = await _finished(execution, reported, monkeypatch)
    assert result.state == ExecutionState.SUCCEEDED
    entry, = result.materialization.entries
    assert entry.relative_path == (Path(folder) / NAME).as_posix()
    work = execution.request.work
    verified = await verify_materialization(str(tmp_path), work.materialization, result.materialization,
                                            execution.executor.footprint(work), reported_bytes=SIZE,
                                            observed_total=SIZE, delay=0)
    assert verified is not None and verified.total_bytes == SIZE


@pytest.mark.asyncio
async def test_a_windows_download_of_another_file_is_still_an_ownership_conflict(tmp_path, monkeypatch):
    target = (tmp_path / NAME).resolve()
    execution = _execution(tmp_path, monkeypatch, target)
    other = str(tmp_path / "Other.exe").replace(os.sep, "/")
    result = await _finished(execution, other, monkeypatch)
    assert result.state != ExecutionState.SUCCEEDED
    assert result.materialization is None


def test_only_the_comparison_ignores_case():
    source = (Path(__file__).resolve().parents[1] / "executors" / "aria2" / "executor.py").read_text(encoding="utf-8")
    body = source.split("def _observation(", 1)[1].split("async def observe_many", 1)[0]
    assert "relative = target.relative_to(" in body
    assert "Path(expected).relative_to(" not in body
