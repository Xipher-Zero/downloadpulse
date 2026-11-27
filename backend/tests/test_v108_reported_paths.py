"""1.0.8 (WS-A10): executors' reported paths (AUDIT-1.0.8.md, A10: B6, B7).

* B6 -- an executor reports the file it produced as its resolved target
  relative to the resolved download root; on Windows ``resolve()`` gives an
  existing folder its on-disk case, so a planned ``movies/x.mkv`` under an
  existing ``Movies`` was rejected by core's exact comparison (rsync, media;
  aria2's own lowercasing was WS-F1). Core now compares by the platform's rule
  for paths (``os.path.normcase``: without case on Windows, exactly elsewhere)
  and still checks the plan's own target.
* B7 -- a SABnzbd ``storage`` reported as a file directly in SAB's complete
  folder made that folder the delivery source: every job's output would move
  into this job's plan root. The source must lie strictly beneath it.
"""
from __future__ import annotations

import asyncio
import ntpath
import os
from pathlib import Path

import pytest

from transfers.filesystem import verify_materialization
from transfers.models import (
    ExecutionFootprint, MaterializationKind, MaterializationPlan, MaterializationResult, MaterializedEntry,
)
from test_v113_sabnzbd_executor import lab, request_for  # noqa: F401
from transfers.models import ExecutionState

SIZE = 64


def _planned(tmp_path):
    root = (tmp_path / "downloads").resolve()
    target = root / "Movies" / "Film.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * SIZE)
    return root, MaterializationPlan(MaterializationKind.FILE, str(root), str(target))


def _verify(root, plan, relative):
    result = MaterializationResult(MaterializationKind.FILE, (MaterializedEntry(relative, SIZE),))
    return asyncio.run(verify_materialization(str(root), plan, result, ExecutionFootprint(()),
                                              reported_bytes=SIZE, observed_total=SIZE, delay=0))


def test_on_windows_a_report_that_differs_only_in_case_is_the_planned_file(tmp_path, monkeypatch):
    root, plan = _planned(tmp_path)
    monkeypatch.setattr(os.path, "normcase", ntpath.normcase)       # the Windows rule for names
    verified = _verify(root, plan, "movies/film.mkv")
    assert verified is not None and verified.total_bytes == SIZE


def test_elsewhere_names_still_compare_exactly(tmp_path):
    root, plan = _planned(tmp_path)
    assert _verify(root, plan, "Movies/Film.mkv") is not None
    if os.path.normcase("A") == "A":                                  # a case-sensitive platform
        assert _verify(root, plan, "movies/film.mkv") is None


def test_another_file_is_never_the_planned_one(tmp_path, monkeypatch):
    root, plan = _planned(tmp_path)
    monkeypatch.setattr(os.path, "normcase", ntpath.normcase)
    assert _verify(root, plan, "Movies/Other.mkv") is None
    assert _verify(root, plan, "Film.mkv") is None


@pytest.mark.asyncio
async def test_a_sab_file_reported_directly_in_the_complete_folder_moves_nothing(lab):
    sab, executor = lab.sab, lab.executor
    request = request_for(lab.root)
    handle = executor.prepare(request)
    bound = (await executor.start(request, handle)).handle
    nzo = bound.native["nzo_id"]
    complete = Path(lab.root) / ".dpwork" / "complete"
    other = complete / "another-job"
    other.mkdir()
    (other / "theirs.bin").write_bytes(b"y")
    job = sab.finish(nzo)
    loose = complete / "loose.bin"
    loose.write_bytes(b"z" * 4)
    job.storage = str(loose)                       # a file directly in the complete folder
    observed = (await executor.observe_many((bound,))).observations[0]
    assert observed.state == ExecutionState.UNKNOWN and observed.materialization is None
    assert (other / "theirs.bin").exists() and loose.exists()     # nothing was moved
