"""1.0.2 (operator, 2026-11-22): core owns a submission's sign-in and referrer
(``transfers.request_context``) and hands it out only by the rules the
operator set, through the real engine: on the request core passes to the
resolving provider and to the executing writer, for a transfer whose every
outstanding demand is the context owner's, while the registrant's grant (a
browser's token) stands; ``settle`` releases it when the submission ends.
Integrations never reach for it. Each scenario is its own process
(``request_context_world.py``)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

WORLD = Path(__file__).with_name("request_context_world.py")


def run(scenario: str, work: Path) -> dict:
    done = subprocess.run([sys.executable, str(WORLD), scenario, str(work)], capture_output=True, text=True,
                          timeout=120, check=False)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_owner_s_own_transfer_is_handed_it_in_resolution_and_execution_until_it_ends(tmp_path):
    report = run("one", tmp_path)
    assert report["unknown transfer"] is None
    assert report["resolution was handed it"] == [True]
    assert report["execution was handed it"] == [True]
    assert report["the stored request carries none"] is True
    assert report["settled while running"] == 0
    assert report["settled after withdrawal"] == 1 and report["held"] is False
    assert report["after withdrawal"] is None


@pytest.mark.parametrize("scenario", ["merged", "revoked", "unregistered"])
def test_a_shared_transfer_a_revoked_grant_or_no_registration_is_handed_none(tmp_path, scenario):
    report = run(scenario, tmp_path)
    assert report["resolution was handed it"] == [False]
    assert report["execution was handed it"] in ([False], [])
    if scenario == "merged":
        assert report["merged into the same transfer"] is True
    if scenario == "revoked":
        assert report["settled while running"] == 1


def test_capacity_refuses_rather_than_drops_and_entries_no_grant_stands_behind_are_released(tmp_path):
    assert run("capacity", tmp_path) == {"unknown transfer": None, "over capacity": "refused",
                                         "settled": 64, "held after settle": 0}
