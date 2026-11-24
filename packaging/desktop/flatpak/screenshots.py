"""The Flatpak's store screenshots, checked on both sides of the build.

The metainfo names its screenshots by URL (the website's /screenshots/). When
flatpak-builder composes the store catalog the software centres read
(files/share/app-info/xmls/<id>.xml.gz), appstreamcli compose fetches each one
and silently drops any it cannot: 1.0.2 and 1.0.3 shipped with no screenshots
because the site did not serve them yet when their Flatpaks were built. So
the build checks, and stops, instead:

    python packaging/desktop/flatpak/screenshots.py reachable METAINFO
        before the build: every screenshot URL answers 200 with an image
    python packaging/desktop/flatpak/screenshots.py composed METAINFO CATALOG
        after it: the composed catalog lists as many screenshots as the metainfo
"""
from __future__ import annotations

import gzip
import sys
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path

TIMEOUT_SECONDS = 30


def screenshot_urls(metainfo: Path) -> list[str]:
    """The source image URL of each screenshot the metainfo lists, in order."""
    root = ElementTree.parse(metainfo).getroot()
    return [(image.text or "").strip() for image in root.iterfind("./screenshots/screenshot/image[@type='source']")]


def composed_count(catalog: Path) -> int:
    """How many screenshots the composed catalog (gzip-compressed XML) lists."""
    with gzip.open(catalog) as stream:
        root = ElementTree.parse(stream).getroot()
    return len(root.findall(".//component/screenshots/screenshot"))


def unreachable(urls: list[str], opener=urllib.request.urlopen) -> list[str]:
    """Each URL that does not answer 200 with an image, and why."""
    problems = []
    for url in urls:
        if not url.startswith("https://"):
            problems.append(f"{url}: not an https URL")
            continue
        try:
            with opener(urllib.request.Request(url, method="GET"), timeout=TIMEOUT_SECONDS) as response:
                kind = response.headers.get("Content-Type", "")
                if response.status != 200 or not kind.startswith("image/"):
                    problems.append(f"{url}: HTTP {response.status}, {kind or 'no Content-Type'}")
        except OSError as exc:
            problems.append(f"{url}: {exc}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "reachable":
        urls = screenshot_urls(Path(argv[1]))
        problems = ["the metainfo lists no screenshots"] if not urls else unreachable(urls)
        for problem in problems:
            print(f"screenshot unreachable: {problem}", file=sys.stderr)
        if not problems:
            print(f"screenshots reachable: {len(urls)}")
        return 1 if problems else 0
    if len(argv) == 3 and argv[0] == "composed":
        listed, composed = len(screenshot_urls(Path(argv[1]))), composed_count(Path(argv[2]))
        if composed != listed:
            print(f"the composed catalog lists {composed} of the metainfo's {listed} screenshots", file=sys.stderr)
            return 1
        print(f"screenshots composed: {composed}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
