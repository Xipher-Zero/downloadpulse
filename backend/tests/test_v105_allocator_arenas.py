"""glibc's malloc keeps one arena per thread that ever allocated, for good; a
long-running DownloadPulse process gathered 16 holding 33 MB (1.0.5 memory
investigation). MALLOC_ARENA_MAX=2 for the server image's and the Flatpak's
first process (SABnzbd and the shell inherit it) and for every process started
through ``ProcessOwnership.environment`` (the desktop backend, rsync, the media
worker...). aria2 keeps the stricter 1 it has set for itself since 1.0.0."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from executors import process_ownership
from executors.process_ownership import ProcessOwnership

ROOT = Path(__file__).resolve().parents[2]


def test_every_owned_native_process_on_linux_keeps_at_most_two_arenas(tmp_path, monkeypatch):
    owner = ProcessOwnership(tmp_path / "owned")
    monkeypatch.setattr(sys, "platform", "linux")
    assert owner.environment()["MALLOC_ARENA_MAX"] == "2"
    assert owner.environment({"DP_HARMLESS": "1"})["MALLOC_ARENA_MAX"] == "2"
    # Only glibc reads it: no other platform is given it.
    for platform in ("darwin", "win32"):
        monkeypatch.setattr(sys, "platform", platform)
        assert "MALLOC_ARENA_MAX" not in owner.environment()


def test_the_server_image_and_the_flatpak_give_their_first_process_the_same_bound():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.findall(r"(?m)^ENV MALLOC_ARENA_MAX=(\S+)$", dockerfile) == [process_ownership.MALLOC_ARENA_MAX]
    assert dockerfile.index("ENV MALLOC_ARENA_MAX=") < dockerfile.index("CMD [")
    manifest = yaml.safe_load((ROOT / "packaging/desktop/flatpak/net.downloadpulse.desktop.yml").read_text(
        encoding="utf-8"))
    assert [arg for arg in manifest["finish-args"] if arg.startswith("--env=MALLOC_ARENA_MAX=")] == [
        f"--env=MALLOC_ARENA_MAX={process_ownership.MALLOC_ARENA_MAX}"]


def test_aria2_keeps_its_own_stricter_single_arena():
    # aria2 starts from the backend's own environment, not ``environment``, and
    # sets one arena (with an aggressive trim) for itself: the declared exception.
    source = (ROOT / "backend/executors/aria2/runtime.py").read_text(encoding="utf-8")
    assert re.findall(r'env\["MALLOC_ARENA_MAX"\] = "(\d+)"', source) == ["1"]
