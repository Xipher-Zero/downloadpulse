"""1.0.8 (WS-A8): one reader of GitHub's releases, and a turned-off update check
offers nothing (AUDIT-1.0.8.md, A8: B1/NS-2, B3).

* ``GET /api/changelog`` returned the local CHANGELOG.md only when it named the
  running version, and otherwise asked GitHub itself -- a second releases
  reader, outside ``application.updates``, that ignored
  ``update_check_interval_hours = 0``. It now serves the shipped file alone.
* ``updates.installer()`` (the desktop's "Download the update") ignored the
  update-check setting, while the page's projection already showed nothing with
  checks off: a stale record's installer could still be downloaded.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api import routes  # noqa: E402
from application import updates  # noqa: E402
from test_v103_update_notices import GitHub, _release, fresh  # noqa: E402,F401


def test_the_changelog_is_the_shipped_file_and_never_asks_github(monkeypatch):
    import aiohttp

    def refuse(*args, **kwargs):
        raise AssertionError("the changelog route must not reach the network")
    monkeypatch.setattr(aiohttp, "ClientSession", refuse)
    monkeypatch.setattr(routes, "read_version", lambda: "9.9.9", raising=False)   # no entry for it: still local
    answer = asyncio.run(routes.get_changelog())
    assert answer["source"] == "local" and answer["content"].startswith("# Changelog")
    source = Path(routes.__file__).read_text(encoding="utf-8")
    body = source.split("async def get_changelog():", 1)[1].split("\n\n\n", 1)[0]
    assert "aiohttp" not in body and "releases" not in body.split('"""', 2)[2]


def test_turned_off_checks_offer_no_installer(monkeypatch):
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release())))
    monkeypatch.setenv("DOWNLOADPULSE_DESKTOP", "1")
    monkeypatch.setattr(updates.sys, "platform", "linux")
    monkeypatch.setenv("FLATPAK_ID", "io.github.xipher_zero.DownloadPulse")
    monkeypatch.setattr(updates._platform, "machine", lambda: "x86_64")
    asyncio.run(updates.refresh())
    assert updates.installer() is not None
    monkeypatch.setattr(updates, "checks_enabled", lambda: False)
    assert updates.installer() is None
