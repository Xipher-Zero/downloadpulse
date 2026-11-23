"""The Flatpak's AppStream metainfo: the template beside this file with its
<releases/> filled from CHANGELOG.md, so the software store's release list is
always the changelog's. Run from a checkout with the release tags fetched:

    python packaging/desktop/flatpak/metainfo.py build/net.downloadpulse.desktop.metainfo.xml

Each released entry (``## [X.Y.Z] — Title``) becomes a release whose description
is the entry's title, then each of its sections (``### Fixed``...) as a
paragraph naming it and a list of its bullets' bold leads -- the bullet titles
alone, as plain text, which is what AppStream's description markup allows (no
Markdown, no inline markup). An entry still titled ``Development`` is not a
release and is left out. CHANGELOG.md carries no dates: a release's date is its tag's
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
SECTION = re.compile(r"^### (.+)$", re.M)
# A bullet's bold lead: ``- **Title.** the rest`` or ``- **Title:** the rest``.
LEAD = re.compile(r"^- \*\*(.+?)\*\*", re.M)
RELEASE_NOTES = "https://github.com/Xipher-Zero/downloadpulse/releases/tag/v{}"


def plain(text: str) -> str:
    """A bullet title as AppStream text: a link as its text, a code span as its
    contents (kept as written: ``remove_remote``), no emphasis marks (``*``,
    ``_``) elsewhere, and no closing colon (``**Sources:**``)."""
    def outside_code(part: str) -> str:
        part = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", part)
        part = re.sub(r"(?<![\w])_+([^_]+?)_+(?![\w])", r"\1", part)
        return part.replace("*", "")
    parts = re.split(r"(`[^`]*`)", text)
    joined = "".join(part[1:-1] if part.startswith("`") and part.endswith("`") and len(part) > 1
                     else outside_code(part) for part in parts)
    return joined.strip().rstrip(":").strip()


def released_entries(changelog: str) -> list[tuple[str, str, list[tuple[str, list[str]]]]]:
    """(version, title, sections) of each released entry, newest first; each
    section is (its name, its bullets' titles), in the entry's order."""
    headings = list(HEADING.finditer(changelog))
    entries = []
    for index, heading in enumerate(headings):
        title = heading.group(2).strip()
        if title.casefold() == "development":
            continue
        body = changelog[heading.end():headings[index + 1].start() if index + 1 < len(headings) else len(changelog)]
        marks = list(SECTION.finditer(body))
        sections = []
        for at, mark in enumerate(marks):
            text = body[mark.end():marks[at + 1].start() if at + 1 < len(marks) else len(body)]
            leads = [plain(lead) for lead in LEAD.findall(text)]
            if leads:
                sections.append((mark.group(1).strip(), leads))
        entries.append((heading.group(1), title, sections))
    return entries


def description_xml(title: str, sections: list[tuple[str, list[str]]]) -> str:
    parts = [f"        <p>{escape(title)}</p>"]
    for name, leads in sections:
        parts.append(f"        <p>{escape(name)}:</p>")
        parts.append("        <ul>\n" + "\n".join(f"          <li>{escape(lead)}</li>" for lead in leads)
                     + "\n        </ul>")
    return "\n".join(parts)


def releases_xml(entries, dates: dict[str, str]) -> str:
    rows = [f'''    <release version="{version}" date="{dates[version]}" type="stable">
      <description>
{description_xml(title, sections)}
      </description>
      <url type="details">{RELEASE_NOTES.format(version)}</url>
    </release>''' for version, title, sections in entries]
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
    for version, _title, _sections in entries:
        date = git_date(f"v{version}") or (git_date("HEAD") if version == current else None)
        if date is None:
            raise SystemExit(f"no tag v{version} for the CHANGELOG entry [{version}]")
        dates[version] = date
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(fill(TEMPLATE.read_text(encoding="utf-8"), releases_xml(entries, dates)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
