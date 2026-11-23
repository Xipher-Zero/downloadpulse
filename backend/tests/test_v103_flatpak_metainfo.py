"""The Flatpak's AppStream metainfo: what a software store shows for the
desktop app (packaging/desktop/flatpak), its release list generated from
CHANGELOG.md at build time (metainfo.py)."""
from __future__ import annotations

import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
FLATPAK = ROOT / "packaging/desktop/flatpak"


def _generator():
    spec = importlib.util.spec_from_file_location("flatpak_metainfo", FLATPAK / "metainfo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHANGELOG = """# Changelog

## [2.1.0] — Development

## [2.0.1] — Faster & <safer> resumes

### Fixed

- something

## [2.0.0] — First release
"""


def test_the_releases_are_the_changelogs_released_entries_newest_first():
    metainfo = _generator()
    entries = metainfo.released_entries(CHANGELOG)
    assert entries == [("2.0.1", "Faster & <safer> resumes"), ("2.0.0", "First release")]
    template = (FLATPAK / "net.downloadpulse.desktop.metainfo.xml").read_text(encoding="utf-8")
    filled = metainfo.fill(template, metainfo.releases_xml(entries, {"2.0.1": "2026-12-02", "2.0.0": "2026-11-21"}))
    releases = ET.fromstring(filled.encode()).find("releases")
    assert [(r.get("version"), r.get("date"), r.get("type"), r.findtext("description/p"), r.findtext("url"))
            for r in releases] == [
        ("2.0.1", "2026-12-02", "stable", "Faster & <safer> resumes",
         "https://github.com/Xipher-Zero/downloadpulse/releases/tag/v2.0.1"),
        ("2.0.0", "2026-11-21", "stable", "First release",
         "https://github.com/Xipher-Zero/downloadpulse/releases/tag/v2.0.0"),
    ]


def test_the_flatpak_installs_the_generated_metainfo_and_the_workflow_generates_it():
    manifest = yaml.safe_load((FLATPAK / "net.downloadpulse.desktop.yml").read_text(encoding="utf-8"))
    module = next(m for m in manifest["modules"] if m["name"] == "downloadpulse")
    assert {"type": "file", "path": "../../../build/net.downloadpulse.desktop.metainfo.xml"} in module["sources"]
    assert {"type": "file", "path": "net.downloadpulse.desktop.metainfo.xml"} not in module["sources"]
    commands = module["build-commands"]
    assert ("install -Dm644 net.downloadpulse.desktop.metainfo.xml "
            "/app/share/metainfo/net.downloadpulse.desktop.metainfo.xml") in commands
    # The icon the store and the launcher show: hicolor, named for the app ID the .desktop file names.
    for size in ("512x512", "256x256"):
        assert any(c.endswith(f"/app/share/icons/hicolor/{size}/apps/net.downloadpulse.desktop.png") for c in commands)
    entry = (FLATPAK / "net.downloadpulse.desktop.desktop").read_text(encoding="utf-8")
    assert "\nIcon=net.downloadpulse.desktop\n" in entry
    workflow = (ROOT / ".github/workflows/desktop-artifacts.yml").read_text(encoding="utf-8")
    generate = "python packaging/desktop/flatpak/metainfo.py build/net.downloadpulse.desktop.metainfo.xml"
    assert workflow.index("git fetch -q --depth=1 origin '+refs/tags/v*:refs/tags/v*'") < workflow.index(generate)
    assert workflow.index(generate) < workflow.index("bash packaging/desktop/flatpak/ci-build.sh")


def test_the_store_listing_describes_the_desktop_app_without_its_internals():
    path = FLATPAK / "net.downloadpulse.desktop.metainfo.xml"
    component = ET.parse(path).getroot()
    listing = " ".join([component.findtext("summary"), *component.find("description").itertext(),
                        *(c.text for c in component.iter("caption"))])
    desktop = (FLATPAK / "net.downloadpulse.desktop.desktop").read_text(encoding="utf-8")
    for text in (listing, desktop):
        assert not re.search(r"aria2|rsync|ffmpeg|yt-dlp|sabnzbd|neutralino|python|uvicorn|localhost|\bport\b|"
                             r"self-hosted|magnet|torrent|nzb", text, re.I), text
    assert "download manager" in component.findtext("summary").casefold()
    assert f"Comment={component.findtext('summary')}" in desktop
    developer = component.find("developer")
    assert developer.get("id") and developer.findtext("name") == "DownloadPulse"
    shots = component.findall("screenshots/screenshot")
    assert shots and shots[0].get("type") == "default"
    assert all(s.findtext("image").startswith("https://downloadpulse.net/") and s.findtext("caption") for s in shots)
    colors = {c.get("scheme_preference"): c.text for c in component.findall("branding/color[@type='primary']")}
    tokens = (ROOT / "frontend/static/design-tokens.css").read_text(encoding="utf-8").lower()
    assert set(colors) == {"light", "dark"} and all(colors[k].lower() in tokens for k in colors)
    assert [c.text for c in component.findall("categories/category")] == ["Network", "FileTransfer"]
    assert component.find("url[@type='bugtracker']").text == "https://github.com/Xipher-Zero/downloadpulse/issues"
    assert component.find("content_rating").get("type") == "oars-1.1"
    # The release list is never written by hand: the template holds only its placeholder.
    assert len(component.findall("releases")) == 1 and len(component.find("releases")) == 0
