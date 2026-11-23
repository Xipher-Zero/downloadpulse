"""The Flatpak's AppStream metainfo: the template beside this file with its
<releases/> filled from CHANGELOG.md, so the software store's release list is
always the changelog's. Run from a checkout with the release tags fetched:

    python packaging/desktop/flatpak/metainfo.py build/net.downloadpulse.desktop.metainfo.xml

Each released entry (``## [X.Y.Z] — Title``) becomes a release with the entry's
title as its description; an entry still titled ``Development`` is not a release
and is left out. CHANGELOG.md carries no dates: a release's date is its tag's
commit date (``vX.Y.Z``), and the version being built, whose tag is created only
after promotion (docs/RELEASE_PROCESS.md), takes the built commit's date. Any
other entry without a tag stops the build.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = Path(__file__).with_name("net.downloadpulse.desktop.metainfo.xml")
HEADING = re.compile(r"^## \[(\d+(?:\.\d+)+)\] — (.+)$", re.M)
RELEASE_NOTES = "https://github.com/Xipher-Zero/downloadpulse/releases/tag/v{}"


def released_entries(changelog: str) -> list[tuple[str, str]]:
    """(version, title) of each released entry, newest first."""
    return [(version, title.strip()) for version, title in HEADING.findall(changelog)
            if title.strip().casefold() != "development"]


def releases_xml(entries: list[tuple[str, str]], dates: dict[str, str]) -> str:
    rows = [f'''    <release version="{version}" date="{dates[version]}" type="stable">
      <description>
        <p>{escape(title)}</p>
      </description>
      <url type="details">{RELEASE_NOTES.format(version)}</url>
    </release>''' for version, title in entries]
    return "<releases>\n" + "\n".join(rows) + "\n  </releases>"


def fill(template: str, releases: str) -> str:
    if template.count("<releases/>") != 1:
        raise SystemExit("the metainfo template needs exactly one <releases/>")
    return template.replace("<releases/>", releases)


def git_date(ref: str) -> str | None:
    run = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%cs", ref, "--"],
                         capture_output=True, text=True)
    return (run.stdout.strip() or None) if run.returncode == 0 else None


def main(out: Path) -> int:
    entries = released_entries((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    current = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    dates = {}
    for version, _ in entries:
        date = git_date(f"v{version}") or (git_date("HEAD") if version == current else None)
        if date is None:
            raise SystemExit(f"no tag v{version} for the CHANGELOG entry [{version}]")
        dates[version] = date
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(fill(TEMPLATE.read_text(encoding="utf-8"), releases_xml(entries, dates)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
