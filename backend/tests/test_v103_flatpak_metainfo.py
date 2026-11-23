"""The Flatpak's AppStream metainfo: what a software store shows for the
desktop app (packaging/desktop/flatpak), its release list generated from
CHANGELOG.md at build time (metainfo.py)."""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
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

### Fixed

- **Not yet released.** Its bullets are not a release's.

## [2.0.1] — Faster & <safer> resumes

### Fixed

- **Resumes start at once.** A paused download waited a full
  retry delay; it now continues.
- **The `remove_remote` flag** is honoured.

### Changed

- **Dependencies:** FastAPI 1.0.

## [2.0.0] — First release

The first release, in prose.

### Included

- **Sources:** HTTP(S) and FTP.
"""


def _description(release) -> list:
    """A release's description as (tag, text) for each paragraph and item."""
    return [(node.tag, node.text) for node in release.find("description").iter() if node.tag in {"p", "li"}]


def test_the_releases_are_the_changelogs_released_entries_newest_first():
    metainfo = _generator()
    entries = metainfo.released_entries(CHANGELOG)
    assert entries == [
        ("2.0.1", "Faster & <safer> resumes",
         [("Fixed", ["Resumes start at once.", "The remove_remote flag"]), ("Changed", ["Dependencies"])]),
        ("2.0.0", "First release", [("Included", ["Sources"])]),
    ]
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
    # Each release says what it changed: its sections and their bullet titles, as plain text.
    assert _description(releases[0]) == [
        ("p", "Faster & <safer> resumes"), ("p", "Fixed:"), ("li", "Resumes start at once."),
        ("li", "The remove_remote flag"), ("p", "Changed:"), ("li", "Dependencies")]
    assert _description(releases[1]) == [("p", "First release"), ("p", "Included:"), ("li", "Sources")]
    # Only AppStream's description markup: paragraphs and lists of items, no other element.
    for release in releases:
        assert {node.tag for node in release.find("description").iter()} <= {"description", "p", "ul", "li"}


def _checkout(tmp_path, metainfo, monkeypatch, tags):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    (tmp_path / "VERSION").write_text("2.1.0\n", encoding="utf-8")
    monkeypatch.setattr(metainfo, "ROOT", tmp_path)
    monkeypatch.setattr(metainfo, "git_date", lambda ref: tags.get(ref))


def test_main_writes_the_filled_template_dated_by_each_tag(tmp_path, monkeypatch):
    metainfo = _generator()
    _checkout(tmp_path, metainfo, monkeypatch, {"v2.0.1": "2026-12-02", "v2.0.0": "2026-11-21", "HEAD": "2026-12-09"})
    out = tmp_path / "build" / "net.downloadpulse.desktop.metainfo.xml"
    assert metainfo.main(out) == 0
    component = ET.fromstring(out.read_bytes())
    assert component.findtext("id") == "net.downloadpulse.desktop"
    releases = component.find("releases")
    assert [(r.get("version"), r.get("date")) for r in releases] == [("2.0.1", "2026-12-02"), ("2.0.0", "2026-11-21")]
    assert _description(releases[0])[1:3] == [("p", "Fixed:"), ("li", "Resumes start at once.")]
    # Everything else is the template's, unchanged.
    template = (FLATPAK / "net.downloadpulse.desktop.metainfo.xml").read_text(encoding="utf-8")
    head = template.split("<releases/>", 1)[0]
    assert out.read_text(encoding="utf-8").startswith(head)


def test_main_dates_the_version_being_built_by_its_commit_and_refuses_an_untagged_entry(tmp_path, monkeypatch):
    metainfo = _generator()
    # The version being built is released before its tag exists: it takes the built commit's date.
    _checkout(tmp_path, metainfo, monkeypatch, {"v2.0.0": "2026-11-21", "HEAD": "2026-12-09"})
    (tmp_path / "VERSION").write_text("2.0.1\n", encoding="utf-8")
    out = tmp_path / "metainfo.xml"
    assert metainfo.main(out) == 0
    assert [(r.get("version"), r.get("date")) for r in ET.fromstring(out.read_bytes()).find("releases")] == [
        ("2.0.1", "2026-12-09"), ("2.0.0", "2026-11-21")]
    # Any other entry without its tag stops the build.
    (tmp_path / "VERSION").write_text("2.1.0\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"no tag v2\.0\.1"):
        metainfo.main(tmp_path / "again.xml")


def test_the_flatpak_installs_the_generated_metainfo_and_its_build_generates_and_validates_it():
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
    # The build script generates it from the fetched tags and validates it before building,
    # so a local run of the same script needs no hand-made metainfo.
    workflow = (ROOT / ".github/workflows/desktop-artifacts.yml").read_text(encoding="utf-8")
    assert workflow.index("git fetch -q --depth=1 origin '+refs/tags/v*:refs/tags/v*'") \
        < workflow.index("bash packaging/desktop/flatpak/ci-build.sh")
    assert "flatpak/metainfo.py" not in workflow
    build = (FLATPAK / "ci-build.sh").read_text(encoding="utf-8")
    generate = "python3 packaging/desktop/flatpak/metainfo.py build/net.downloadpulse.desktop.metainfo.xml"
    validate = "appstreamcli validate --no-net --explain build/net.downloadpulse.desktop.metainfo.xml"
    assert build.count(generate) == 1 and build.count(validate) == 1
    assert build.index(generate) < build.index(validate) < build.index("flatpak-builder --user")
    assert " appstream " in build[:build.index(generate)]          # Debian's appstreamcli, installed first


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


MARKDOWN_CHANGELOG = """## [3.0.0] — Plain text

### Fixed

- **The `remove_remote` flag, [the guide](https://example.test/a_b) and _really_ fast resumes.** More.
"""


def test_a_bullet_title_loses_its_markdown_but_keeps_a_code_spans_text(tmp_path):
    metainfo = _generator()
    assert metainfo.plain("The `remove_remote` flag, [the guide](https://example.test/a_b), **bold**, "
                          "_emphasis_ and __strong__ in snake_case:") == (
        "The remove_remote flag, the guide, bold, emphasis and strong in snake_case")
    entries = metainfo.released_entries(MARKDOWN_CHANGELOG)
    assert entries[0][2] == [("Fixed", ["The remove_remote flag, the guide and really fast resumes."])]
    template = (FLATPAK / "net.downloadpulse.desktop.metainfo.xml").read_text(encoding="utf-8")
    filled = metainfo.fill(template, metainfo.releases_xml(entries, {"3.0.0": "2027-01-04"}))
    releases = filled[filled.index("<releases>"):]
    for mark in ("](", "`", "*", " _", "_ "):
        assert mark not in releases, mark
    ET.fromstring(filled.encode())
    appstreamcli = shutil.which("appstreamcli")
    if appstreamcli:
        out = tmp_path / "net.downloadpulse.desktop.metainfo.xml"
        out.write_text(filled, encoding="utf-8")
        run = subprocess.run([appstreamcli, "validate", "--no-net", str(out)], capture_output=True, text=True)
        assert run.returncode == 0, run.stdout + run.stderr
