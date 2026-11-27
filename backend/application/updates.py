"""The latest DownloadPulse release: its one fetcher and its one record.

``refresh`` is the only reader of GitHub's ``releases/latest`` (called by the
scheduler's one update loop, ``core.scheduler.update_check_loop``), and, when
an update is available, of ``releases`` for the notes of every release newer
than this one (``notes``); what it keeps lives in this process's memory and
nothing else holds a copy. Core
never learns what an update is: an update's installer reaches it only as an
ordinary one-file Metalink4 (``metalink``) that the desktop route submits
through the canonical submission seam, carrying the release's SHA-256, which
core's own verifier checks at completion.

The record::

    {checked_at, current, latest, update_available, release_url, published_at,
     installer: {name, url, sha256, size} | None}

``installer`` is filled only on a desktop installation, for this platform's
asset named exactly so (``INSTALLERS``), with a ``digest`` GitHub states and a
download address on ``github.com`` under this repository's release downloads;
anything else is no installer, and the desktop then only says that an update
exists. A failed check (network error, non-200, rate limit) keeps the last good
record, publishes nothing and logs its sanitized reason.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import platform as _platform
import posixpath
import re
import sys
import time
from urllib.parse import urlsplit

import aiohttp

from core.branding import REPOSITORY_API_URL
from core.logging_utils import sanitize_exception
from core.version import is_version_newer, normalize_version_tag, parse_version, read_version

logger = logging.getLogger("downloadpulse.updates")

# The first check after start; later checks follow update_check_interval_hours.
FIRST_CHECK_SECONDS = 60

# Where a release page and an installer may be: this repository's releases on github.com.
_HOST = "github.com"
_RELEASES_PATH = "/Xipher-Zero/downloadpulse/releases/"
_DOWNLOAD_PATH = _RELEASES_PATH + "download/"
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")

# This platform's installer, by exact asset name (docs/RELEASE_PROCESS.md, Release assets),
# and the one machine architecture each is built for: macOS arm64, Windows and
# Linux x86_64. A computer of any other architecture (an Intel Mac, Windows or
# Linux on ARM) gets the notice only: no installer is offered.
INSTALLERS = {
    "win32": ("DownloadPulse-{version}-windows-x64.exe", {"amd64", "x86_64"}),
    "darwin": ("DownloadPulse-{version}-macos-arm64.dmg", {"arm64", "aarch64"}),
    "linux": ("DownloadPulse-{version}-linux-x86_64.flatpak", {"x86_64", "amd64"}),
}

# The notes shown in the update notice: each release newer than this one, up to
# the latest, newest first, as its Markdown body. Bounded: a notice is not an archive.
NOTES_RELEASES = 10
NOTES_CHARS = 20_000

_record: dict | None = None
_etag = ""
_notes: list[dict] = []
# The update download a click started: (version, transfer id). The update's own
# fact, so a second click returns that transfer while it still serves.
_download: tuple[str, int] | None = None
# One click at a time decides whether to reuse that transfer or submit a new one.
download_lock = asyncio.Lock()


class CheckFailed(Exception):
    """A check that produced no release (its reason is logged, never shown)."""


def record() -> dict | None:
    """The latest check's record, or ``None`` before the first good check."""
    return dict(_record) if _record is not None else None


def checks_enabled() -> bool:
    """Whether update checks are on (``update_check_interval_hours`` > 0):
    0 turns off the check and the in-app notice together."""
    from core.config import get_settings
    try:
        return int(getattr(get_settings(), "update_check_interval_hours", 12)) > 0
    except Exception:
        return True


def _platform_installer_name(version: str) -> str | None:
    from core.config import desktop_installation
    if not desktop_installation():
        return None
    platform = "linux" if sys.platform.startswith("linux") else sys.platform
    if platform == "linux" and not _inside_flatpak():
        return None   # a desktop run from source has no installer of its own
    pattern, machines = INSTALLERS.get(platform, (None, ()))
    if pattern is None or _platform.machine().casefold() not in machines:
        return None
    return pattern.format(version=version)


def _inside_flatpak() -> bool:
    from core.desktop_storage import FLATPAK_INFO
    return bool(os.environ.get("FLATPAK_ID")) or FLATPAK_INFO.exists()


def _github_address(value, prefix: str) -> bool:
    try:
        parts = urlsplit(str(value or ""))
        port = parts.port
    except ValueError:
        return False
    path = parts.path
    # No dot segment and no escape: the path is exactly the one it names, so
    # the prefix cannot be climbed out of (``/download/../../evil/...``).
    return (parts.scheme == "https" and parts.hostname == _HOST and parts.netloc == _HOST and port is None
            and not parts.query and not parts.fragment and "%" not in path and "\\" not in path
            and posixpath.normpath(path) == path.rstrip("/") and path.startswith(prefix))


def release_page(value) -> str | None:
    """A release page address this repository publishes, else ``None``."""
    return str(value) if _github_address(value, _RELEASES_PATH) else None


def _installer(release: dict, version: str) -> dict | None:
    name = _platform_installer_name(version)
    if name is None:
        return None
    asset = next((item for item in release.get("assets") or () if isinstance(item, dict) and item.get("name") == name),
                 None)
    if asset is None:
        return None
    url, size = asset.get("browser_download_url"), asset.get("size")
    digest = _DIGEST.fullmatch(str(asset.get("digest") or ""))
    if digest is None:
        return None   # no stated hash: no download, the notice only
    if not _github_address(url, _DOWNLOAD_PATH) or not str(url).endswith("/" + name):
        logger.warning("Release asset %s refused: its address is not this repository's release download", name)
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return None
    return {"name": name, "url": str(url), "sha256": digest.group(1), "size": size}


def _built(release: dict) -> dict:
    if release.get("draft") or release.get("prerelease"):
        raise CheckFailed("the latest release is a draft or a pre-release")
    latest = normalize_version_tag(release.get("tag_name") or "")
    if parse_version(latest) is None:
        raise CheckFailed("the latest release names no version")
    current = read_version()
    newer = is_version_newer(latest, current)
    return {
        "checked_at": int(time.time()),
        "current": current,
        "latest": latest,
        "update_available": newer,
        "release_url": release_page(release.get("html_url")),
        "published_at": str(release.get("published_at") or "")[:10] or None,
        "installer": _installer(release, latest) if newer else None,
    }


async def _fetch(etag: str) -> tuple[int, str, object]:
    """One conditional GET of ``releases/latest``: (status, ETag, body)."""
    headers = {"Accept": "application/vnd.github+json"}
    if etag:
        headers["If-None-Match"] = etag
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.get(f"{REPOSITORY_API_URL}/releases/latest", headers=headers) as reply:
            body = await reply.json(content_type=None) if reply.status == 200 else None
            return reply.status, str(reply.headers.get("ETag") or ""), body


async def _fetch_releases() -> object:
    """One GET of ``releases`` (the newest first): the body, or ``None``."""
    headers = {"Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.get(f"{REPOSITORY_API_URL}/releases",
                               params={"per_page": str(NOTES_RELEASES * 2)}, headers=headers) as reply:
            return await reply.json(content_type=None) if reply.status == 200 else None


def _note(version: str, body) -> dict:
    return {"version": version, "notes": str(body or "").strip()[:NOTES_CHARS]}


async def _newer_notes(record: dict, latest_body) -> list[dict]:
    """The notes of each published release newer than this one and no newer
    than the latest, newest first. When the list cannot be read, the latest
    release's own notes."""
    current, latest = record["current"], record["latest"]
    fallback = [_note(latest, latest_body)]
    try:
        releases = await _fetch_releases()
    except Exception as exc:
        logger.warning("Release notes check failed: %s", sanitize_exception(exc))
        return fallback
    if not isinstance(releases, list):
        return fallback
    found: dict[str, dict] = {}
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        version = normalize_version_tag(release.get("tag_name") or "")
        if (parse_version(version) is None or not is_version_newer(version, current)
                or is_version_newer(version, latest) or version in found):
            continue
        found[version] = _note(version, release.get("body"))
    found.setdefault(latest, fallback[0])
    newest_first = functools.cmp_to_key(
        lambda a, b: -1 if is_version_newer(a["version"], b["version"])
        else 1 if is_version_newer(b["version"], a["version"]) else 0)
    return sorted(found.values(), key=newest_first)[:NOTES_RELEASES]


def notes() -> list[dict]:
    """The kept release notes (empty when no update is available)."""
    current = record()
    if current is None or not current.get("update_available"):
        return []
    return [dict(item) for item in _notes]


def _signal(record: dict | None) -> tuple:
    if record is None:
        return (None,)
    installer = record.get("installer") or {}
    return (record.get("latest"), record.get("update_available"), record.get("release_url"),
            installer.get("name"), installer.get("sha256"))


async def refresh() -> tuple[dict, str] | None:
    """Check the latest release once: ``(record, release notes)`` on success
    (304 Not Modified keeps the record as it was), ``None`` on failure. A
    changed record is announced as ``update_status`` (a bare signal: the page
    reads the record again)."""
    global _record, _etag, _notes
    try:
        status, etag, body = await _fetch(_etag if _record is not None else "")
        if status == 304 and _record is not None:
            _record = {**_record, "checked_at": int(time.time())}
            return record(), ""
        if status != 200 or not isinstance(body, dict):
            raise CheckFailed(f"GitHub answered {status}")
        built = _built(body)
    except Exception as exc:
        # The reason stays in the server log; the record keeps the last good check.
        logger.warning("Update check failed: %s", sanitize_exception(exc))
        return None
    kept = await _newer_notes(built, body.get("body")) if built["update_available"] else []
    previous, _record, _etag, _notes = _record, built, etag, kept
    if _signal(previous) != _signal(built):
        from services.event_bus import publish
        await publish("update_status", {})
    return record(), str(body.get("body") or "").strip()


def projection(*, download_offered: bool) -> dict:
    """What an Admin's page reads: never the installer's address or hash
    (``url``, ``sha256``), only its name and size, and whether this request may
    download it (``download_offered``)."""
    from core.config import desktop_installation
    current = record()
    if current is None or not checks_enabled():
        return {"checked": False}
    installer = current.get("installer") if desktop_installation() else None
    return {
        "checked": True,
        "checked_at": current["checked_at"],
        "current": current["current"],
        "latest": current["latest"],
        "update_available": bool(current["update_available"]),
        "release_url": current["release_url"],
        "published_at": current["published_at"],
        "installer": {"name": installer["name"], "size": installer["size"]} if installer else None,
        "download_offered": bool(installer and download_offered and current["update_available"]),
        "notes": notes(),
    }


def installer() -> dict | None:
    """The installer the record names for this platform, when an update is
    available and update checks are on: turned off, no earlier record offers a
    download either (1.0.8, WS-A8), as the page's projection already shows none."""
    current = record()
    if current is None or not current.get("update_available") or not checks_enabled():
        return None
    return current.get("installer")


def metalink(installer: dict) -> bytes:
    """The one-file Metalink4 an update's installer is downloaded through:
    its name, size, SHA-256 and the one address, exactly as the record holds them."""
    from xml.sax.saxutils import escape
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<metalink xmlns="urn:ietf:params:xml:ns:metalink">\n'
        f'  <file name="{escape(installer["name"], {chr(34): "&quot;"})}">\n'
        f'    <size>{int(installer["size"])}</size>\n'
        f'    <hash type="sha-256">{escape(installer["sha256"])}</hash>\n'
        f'    <url>{escape(installer["url"])}</url>\n'
        '  </file>\n'
        '</metalink>\n'
    ).encode("utf-8")


def remembered_download(version: str) -> int | None:
    """The transfer an earlier click started for ``version``, if any."""
    return _download[1] if _download is not None and _download[0] == version else None


def remember_download(version: str, transfer_id: int) -> None:
    global _download
    _download = (version, int(transfer_id))
