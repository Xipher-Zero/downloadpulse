"""An exception's text stays in the server log, never in an API response
(``/api/version/check``, ``/api/aria2/runtime``); a notification failure's
reason is one log line however many lines the remote endpoint answered."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import aiohttp

from api import routes
from services.notifications import NotificationService

SECRET = "internal /app/data/secret.db path\ntraceback detail"


class _Failing:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(SECRET)


def test_version_check_failure_names_no_exception_text(monkeypatch, caplog):
    routes._update_check_cache.clear()
    monkeypatch.setattr(aiohttp, "ClientSession", _Failing)
    with caplog.at_level(logging.WARNING, logger="downloadpulse.routes"):
        result = asyncio.run(routes.version_check())
    routes._update_check_cache.clear()
    assert result["update_available"] is False and result["latest"] is None
    assert result["error"] == "The latest release could not be checked"
    assert "secret.db" in caplog.text  # the reason is logged server-side


def test_runtime_diagnostics_failure_names_no_exception_text(monkeypatch, caplog):
    async def status():
        return {"running": True}

    async def diagnostics():
        raise RuntimeError(SECRET)

    monkeypatch.setattr(routes.aria2_runtime, "status", status)
    application = SimpleNamespace(integration_admin=lambda _id: SimpleNamespace(memory_diagnostics=diagnostics))
    with caplog.at_level(logging.WARNING, logger="downloadpulse.routes"):
        result = asyncio.run(routes.aria2_runtime_status(application))
    assert result["diagnostics"] == {"error": "Diagnostics are unavailable"}
    assert "secret.db" not in repr(result)
    assert "secret.db" in caplog.text


def test_notification_failure_reason_is_one_log_line(monkeypatch, caplog):
    forged = "Discord webhook 400: bad\n2026-01-01 | ERROR | forged | admin signed in"

    class _Raising:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(forged)

    import services.notifications as notifications
    monkeypatch.setattr(notifications.aiohttp, "ClientSession", _Raising)
    service = NotificationService("https://example.invalid/hook", dedup=False)
    with caplog.at_level(logging.ERROR):
        sent = asyncio.run(service.send("Title", "Body"))
    assert sent is False
    failures = [r.getMessage() for r in caplog.records if "notification failed" in r.getMessage()]
    assert failures and all("\n" not in line and "\r" not in line for line in failures)
    assert "\\n2026-01-01" in failures[0]
