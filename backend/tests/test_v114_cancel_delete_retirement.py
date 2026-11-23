"""Cancel, then Delete: a cancelled or deleted transfer reclaims what its
stopped execution owned (WS3 I-2; Cancel since 1.0.3), through the one core
retirement owner.

Cancel stops the writer and retires the execution-owned material -- the partial
target and the declared transient material -- once the durable record proves the
writer stopped; a cancelled transfer is final, so Retry never reopens it and
nothing is written there again. Delete afterwards finds nothing left and stays
idempotent. A writer whose stop is unconfirmed keeps its material and
its obligation; unowned, unrelated and delivered material is never touched;
Delete is idempotent; a restart resolves a retained obligation from durable
state.

Every case runs ``cancel_delete_world.py`` as its own process: the product's
``DB_PATH`` names that process's database, the engine, repository, ownership
records and retirement are production code, and the executor is the existing
ledger fake. Nothing is patched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WORLD = Path(__file__).with_name("cancel_delete_world.py")
STOPPED = {"cleanup_state": "complete", "authorized": 0, "state": "cancelled"}


def world(scenario: str, work: Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([sys.executable, str(WORLD), scenario, str(work)], capture_output=True, text=True,
                            timeout=120, cwd=WORLD.parents[1], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_partial_transfers_off_cancel_reclaims_the_transient_material(tmp_path):
    report = world("partial-off", tmp_path)
    assert report["owned"]
    cancelled = report["after cancel"]
    assert not cancelled["material"] and not cancelled["journal"] and cancelled["cleanup"] == STOPPED
    assert cancelled["unrelated"] and cancelled["files"] == ["unrelated.bin"]
    after = report["after delete"]
    assert after["transfer"] == "deleted" and not after["journal"] and after["target"] is None
    assert after["unrelated"] and after["files"] == ["unrelated.bin"]


def test_partial_transfers_on_cancel_reclaims_the_partial_target_and_retry_never_reopens_it(tmp_path):
    report = world("partial-on", tmp_path)
    assert report["owned"]
    cancelled = report["after cancel"]
    assert cancelled["target"] is None and cancelled["cleanup"] == STOPPED
    assert cancelled["unrelated"] and cancelled["files"] == ["unrelated.bin"]
    assert report["retry refused"] == "transfer.retry_cancelled"
    retried = report["after retry"]
    assert retried["transfer"] == "cancelled" and retried["artifact"] == "cancelled"
    assert not retried["material"] and retried["files"] == ["unrelated.bin"]
    after = report["after delete"]
    assert after["transfer"] == "deleted" and after["target"] is None
    assert after["unrelated"] and after["files"] == ["unrelated.bin"]


def test_cancel_applies_to_the_own_request_and_reclaims_only_once_no_demand_remains(tmp_path):
    report = world("shared", tmp_path)
    assert report["one transfer"]
    first = report["after first withdrawal"]
    # Another account still needs the work: it continues, its partial intact.
    assert first["settled"] == [] and first["transfer"] not in {"cancelled", "deleted"}
    assert first["writer"] == "running" and first["target"] == repr(b"\x00" * 16)
    last = report["after last withdrawal"]
    assert last["transfer"] == "cancelled" and last["target"] is None and last["cleanup"] == STOPPED
    assert last["files"] == ["unrelated.bin"]


def test_a_writer_that_may_still_be_running_keeps_its_material_through_cancel_and_delete(tmp_path):
    report = world("live", tmp_path)
    assert report["after cancel"]["material"] and report["after cancel"]["writer"] == "running"
    after = report["after delete"]
    assert after["writer"] == "running"
    assert after["material"]
    assert after["cleanup"]["cleanup_state"] in {"pending", "blocked"}


def test_an_unconfirmed_stop_keeps_material_and_obligation_until_the_stop_is_observed(tmp_path):
    report = world("lost-ack", tmp_path)
    assert report["after cancel"]["material"]
    assert report["after delete"]["material"]
    assert report["after delete"]["cleanup"]["cleanup_state"] in {"pending", "blocked"}
    later = report["after the retained obligation's next pass"]
    assert not later["material"] and later["cleanup"]["cleanup_state"] == "complete"


def test_material_the_execution_does_not_own_survives_cancel_and_delete(tmp_path):
    report = world("unowned", tmp_path)
    assert not report["owned"]
    assert report["after cancel"]["target"] == repr(b"operator material") and report["after cancel"]["unrelated"]
    after = report["after delete"]
    assert after["target"] == repr(b"operator material") and after["unrelated"]


def test_a_delivered_sibling_survives_while_the_cancelled_files_partial_is_reclaimed(tmp_path):
    report = world("completed", tmp_path)
    assert report["sibling before cancel"] == "completed"
    cancelled = report["after cancel"]
    assert cancelled["sibling"] == {"state": "completed", "payload": repr(b"done")}
    assert cancelled["target"] is None and cancelled["files"] == ["delivered", "unrelated.bin"]
    after = report["after delete"]
    assert after["sibling"] == {"state": "completed", "payload": repr(b"done")}
    assert after["target"] is None
    assert after["files"] == ["delivered", "unrelated.bin"]


def test_a_repeated_delete_is_idempotent(tmp_path):
    report = world("repeat", tmp_path)
    assert report["after delete"]["files"] == ["unrelated.bin"]
    again = report["after second delete"]
    assert not again["material"] and again["unrelated"] and again["files"] == ["unrelated.bin"]
    assert again["cleanup"] == STOPPED


def test_a_retained_obligation_is_resolved_from_durable_state_after_a_restart(tmp_path):
    before = world("restart-before", tmp_path)
    assert before["after delete"]["material"] and before["after delete"]["writer"] == "running"
    assert before["after delete"]["cleanup"]["cleanup_state"] in {"pending", "blocked"}
    after = world("restart-after", tmp_path)["after restart"]
    assert not after["material"] and after["cleanup"]["cleanup_state"] == "complete"
    assert after["unrelated"] and after["files"] == ["unrelated.bin"]
