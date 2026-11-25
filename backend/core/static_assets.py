"""The interface's static files, cached by the browser only where that is provably safe.

Every ``.html``/``.js``/``.css`` response is revalidated by default
(``main.request_id_middleware``), so a new release can never run an older
JS/CSS/HTML mix. A script or stylesheet is cached for good
(``private, max-age=31536000, immutable``) only when the request itself proves
it asks for the current bytes: its ``v`` equals the version this owner derives
from the content it serves. A stale, missing or hand-written ``v`` is simply
revalidated as before -- a wrong version can never pin stale content.

Versions are derived, never kept by hand. The shell (``index.html``) and the
stylesheet that imports the others (``style.css``) are served rewritten: each
``?v=`` of a script or stylesheet this directory serves becomes that file's
version. A file's version is the sha256[:16] of the bytes served for it -- a
rewritten file's of its rewritten body, its children's first -- so an
imported stylesheet that changes changes its importer's version too. A
reference to an address a route answers (``/app.js``, ``/ui-theme-bootstrap.js``,
``/i18n/<tag>.js``: built per request) is never rewritten. A rewritten body is
served from memory with its own ETag (its version) and no Last-Modified, so a
change in a child is never answered 304 from the file's own date.

``private``, never ``public``: these files are served behind authentication,
and no shared cache may keep them.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

import anyio
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.routing import Match, Mount
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

IMMUTABLE = "private, max-age=31536000, immutable"
# The files served rewritten: the shell, and the one stylesheet that imports
# the others (one level deep).
REWRITTEN = frozenset({"index.html", "style.css"})
# A versioned script or stylesheet reference: an absolute local path, then ?v=.
_REFERENCE = re.compile(r"(?P<path>/[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:js|css))\?v=(?P<v>[A-Za-z0-9_.-]*)")
_VERSIONED = (".js", ".css")
_MEDIA = {"index.html": "text/html; charset=utf-8", "style.css": "text/css; charset=utf-8"}


class VersionedStaticFiles(StaticFiles):
    """``StaticFiles`` whose scripts and stylesheets carry derived versions.

    ``routed(path)`` says whether an application route answers ``path``: such
    an address is never this directory's file, whatever lies on disk."""

    def __init__(self, *, directory: str | os.PathLike, routed: Callable[[str], bool], **kwargs):
        super().__init__(directory=directory, **kwargs)
        self._root = Path(directory).resolve()
        self._routed = routed
        self._hashes: dict[str, tuple[tuple, str]] = {}

    async def get_response(self, path: str, scope: Scope) -> Response:
        name = path.strip("/")
        if name in ("", "."):                       # the directory itself: html=True serves its index.html
            name = "index.html"
        if name in REWRITTEN:
            if scope["method"] not in ("GET", "HEAD"):
                raise HTTPException(status_code=405)       # as StaticFiles answers every other file
            return await anyio.to_thread.run_sync(self._rewritten_response, name, scope)
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304) and name.endswith(_VERSIONED) and await anyio.to_thread.run_sync(
                self._proven, name, scope):
            response.headers["Cache-Control"] = IMMUTABLE
        return response

    # ── versions ─────────────────────────────────────────────────────────

    def version(self, name: str) -> str | None:
        """The version of the bytes served for ``name``, or None when this
        directory does not serve it."""
        return self._version(name, ())

    def _version(self, name: str, seen: tuple) -> str | None:
        if name in seen:
            return None                                     # an import cycle: left unversioned
        if name in REWRITTEN:
            body = self._body(name, seen + (name,))
            return None if body is None else _digest(body)
        target = self._file(name)
        if target is None:
            return None
        stat = target.stat()
        key = (stat.st_mtime_ns, stat.st_size, stat.st_ino, stat.st_ctime_ns)
        cached = self._hashes.get(name)
        if cached is None or cached[0] != key:
            cached = (key, _digest(target.read_bytes()))
            self._hashes[name] = cached
        return cached[1]

    def _file(self, name: str) -> Path | None:
        """``name`` as this directory's own regular file, never outside it and
        never an address a route answers."""
        if self._routed("/" + name):
            return None
        target = (self._root / name).resolve()
        if self._root not in target.parents or not target.is_file():
            return None
        return target

    def _body(self, name: str, seen: tuple) -> bytes | None:
        target = self._file(name)
        if target is None:
            return None
        text = target.read_text(encoding="utf-8")

        def stamp(match: re.Match) -> str:
            child = self._version(match.group("path").lstrip("/"), seen)
            return match.group(0) if child is None else f"{match.group('path')}?v={child}"
        return _REFERENCE.sub(stamp, text).encode("utf-8")

    def _proven(self, name: str, scope: Scope) -> bool:
        asked = self._asked(scope)
        return asked is not None and asked == self.version(name)

    @staticmethod
    def _asked(scope: Scope) -> str | None:
        asked = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("v")
        return asked[-1] if asked else None

    # ── the rewritten files ──────────────────────────────────────────────

    def _rewritten_response(self, name: str, scope: Scope) -> Response:
        body = self._body(name, (name,))
        if body is None:
            return Response(status_code=404)
        version = _digest(body)
        headers = {"ETag": f'"{version}"'}
        if name != "index.html" and self._asked(scope) == version:   # the very body sent, never recomputed
            headers["Cache-Control"] = IMMUTABLE
        asked = Headers(scope=scope).get("if-none-match", "")
        if f'"{version}"' in [item.strip().removeprefix("W/") for item in asked.split(",")]:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type=_MEDIA[name], headers=headers)


def routed_by(app) -> Callable[[str], bool]:
    """Whether one of ``app``'s routes -- not a mount, such as the static
    directory's own -- answers a GET of ``path``: asked of each route itself,
    included routers too, when asked (routes added later count)."""
    def routed(path: str) -> bool:
        scope = {"type": "http", "path": path, "method": "GET", "root_path": "", "headers": [], "query_string": b""}
        return any(not isinstance(route, Mount) and route.matches(scope)[0] == Match.FULL for route in app.routes)
    return routed


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]
