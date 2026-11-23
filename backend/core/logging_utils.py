"""Logging helpers for Docker-friendly, secret-safe output."""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from core.branding import APP_NAME, APP_SHORT_NAME, REPOSITORY_URL

# A whole magnet URI, whatever order its parameters come in: any of them --
# trackers above all, whose URLs commonly carry a private passkey -- may be a
# credential, so the URI is shown only as its info-hash hint (or not at all).
_MAGNET_RE = re.compile(r"magnet:\?[^\s\"']*", re.IGNORECASE)
_MAGNET_BTIH_RE = re.compile(r"(?:^|[?&])xt=urn:btih:([0-9a-zA-Z]{32,40})(?=&|$)", re.IGNORECASE)
_WEBHOOK_URL_RE = re.compile(
    r"https?://(?:canary\.|ptb\.)?(?:discord(?:app)?\.com)/api/webhooks/[^\s\"']+",
    re.IGNORECASE,
)
_LONG_URL_RE = re.compile(r"https?://[^\s\"']{80,}")
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|apikey|token|secret|password|pass|key)=)([^&\s\"']+)"
)
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|token|basic)\s+)[^\s,\"']+")
# A scheme starts only where a scheme run starts, and the user and password are
# bounded: linear on any input, never a rescan from every position.
_CREDENTIAL_URL_RE = re.compile(r"(?<![a-z0-9+.-])([a-z][a-z0-9+.-]{0,31}://[^:\s/@]{1,256}:)([^@\s]{1,1024})(@)", re.IGNORECASE)
# The most of a value the patterns read: what is logged is far shorter
# (max_length), and redaction still runs before that truncation.
_SCAN_LIMIT = 4096


def _short_hash(value: str) -> str:
    if not value:
        return ""
    return f"{value[:8]}..."


def _magnet_hint(match: re.Match) -> str:
    btih = _MAGNET_BTIH_RE.search(match.group(0)[len("magnet:"):])
    return f"<magnet:{_short_hash(btih.group(1).lower())}>" if btih else "<magnet>"


def sanitize_log_value(value: Any, max_length: int = 300) -> str:
    """Return a short, log-safe string with secrets and long URLs redacted."""
    if value is None:
        return ""
    # A margin past what is kept: a credential that straddles the cut is never kept half-redacted.
    msg = str(value)[:max(_SCAN_LIMIT, max_length + 2048)]
    msg = _CREDENTIAL_URL_RE.sub(r"\1<redacted>\3", msg)
    msg = _AUTH_HEADER_RE.sub(r"\1<redacted>", msg)
    msg = _WEBHOOK_URL_RE.sub("<webhook-url>", msg)
    msg = _MAGNET_RE.sub(_magnet_hint, msg)
    msg = _SENSITIVE_QUERY_RE.sub(r"\1<redacted>", msg)
    msg = _LONG_URL_RE.sub("<url>", msg)
    # One log line per record: an embedded line break could forge another.
    msg = msg.replace("\r", "\\r").replace("\n", "\\n")
    return (msg[:max_length] + "...") if len(msg) > max_length else msg


def sanitize_exception(exc: BaseException, max_length: int = 300) -> str:
    """Return a safe exception message suitable for logs and API errors."""
    return sanitize_log_value(str(exc).strip() or repr(exc), max_length=max_length)


def configure_logging(level: str = "INFO", pretty: bool = False, log_format: str = "plain") -> None:
    """Configure the app's plaintext log format without forcing ANSI colors."""
    normalized = str(level or "INFO").upper()
    numeric_level = getattr(logging, normalized, logging.INFO)
    fmt_name = str(log_format or "plain").lower()
    if fmt_name == "compact":
        fmt = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
    elif pretty:
        fmt = "%(asctime)s | %(levelname)-5s | %(name)-16s | %(message)s"
    else:
        fmt = "%(asctime)s | %(levelname)-5s | %(name)-20s | %(message)s"
    logging.basicConfig(
        level=numeric_level,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger().setLevel(numeric_level)
    for lib in ("uvicorn.access", "uvicorn.error", "httpx", "aiosqlite"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def deployment_mode() -> str:
    """What the startup banner names this installation: the installed desktop
    application, a container (Docker, Podman, an Unraid template -- an
    image's /.dockerenv or Podman's /run/.containerenv), or else a process run
    directly on its host. Said as it is, never assumed (1.0.3: a generic
    host was announced as "Docker / Unraid")."""
    from core.config import desktop_installation
    if desktop_installation():
        return "Desktop application"
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return "Container"
    return "Host process"


def log_startup_banner(
    logger: logging.Logger,
    *,
    version: str,
    mode: str,
    database: str,
    download_client: str,
    web_ui: str,
    auth: str,
) -> None:
    """Emit a compact startup summary through the normal logger."""
    sep = "-" * 56
    lines = [
        sep,
        f" {APP_NAME}  v{version}",
        sep,
        f" Mode:            {mode}",
        f" Database:        {database}",
        f" Download Client: {download_client}",
        f" Web UI:          {web_ui}",
        f" Auth:            {auth}",
        f" GitHub:          {REPOSITORY_URL}",
        sep,
    ]
    for line in lines:
        logger.info(line)
