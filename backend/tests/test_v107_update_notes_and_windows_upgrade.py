"""1.0.7: the update notice says what is new in the app itself, and the Windows
installer updates a running DownloadPulse instead of refusing.

* ``application.updates.refresh`` keeps, beside the record, the notes of every
  published release newer than this one and no newer than the latest, newest
  first and bounded; a list that cannot be read is the latest release's own
  notes; no update, no notes and no list asked. The page reads them with the
  record (``notes``).
* The notice draws them in the dialog (``app.js``): a title with its chevron,
  the notes as text only; nothing links out of the app.
* Windows: the supervisor waits on a named quit event (``QUIT_EVENT``) that
  ends the run as Quit does; the installer, finding DownloadPulse running,
  asks, sets that event, waits for the data root's lock, offers Retry when it
  does not close, and starts it again after a silent update.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from application import updates  # noqa: E402
from test_v103_update_notices import GitHub, Releases, _release, fresh  # noqa: E402,F401


def _listed(tag, body, **extra):
    return {"tag_name": tag, "body": body, "draft": False, "prerelease": False, **extra}


def test_the_notes_are_each_newer_release_up_to_the_latest_newest_first(monkeypatch):
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release(tag="v1.0.6", body="Six"))))
    listed = Releases([
        _listed("v1.0.7", "Seven: newer than the latest, not yet the latest"),
        _listed("v1.0.6", "Six"),
        _listed("v1.0.6-rc1", "A candidate", prerelease=True),
        _listed("v1.0.5", "Five"),
        _listed("v1.0.4", "Four", draft=True),
        _listed("v1.0.3", "Three: the one running"),
        _listed("v1.0.2", "Two"),
        "not a release",
    ])
    monkeypatch.setattr(updates, "_fetch_releases", listed)
    record, _ = asyncio.run(updates.refresh())
    assert record["update_available"] is True
    assert updates.notes() == [{"version": "1.0.6", "notes": "Six"}, {"version": "1.0.5", "notes": "Five"}]
    assert listed.asked == 1


def test_notes_are_bounded_in_count_and_length(monkeypatch):
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release(tag="v1.0.30"))))
    monkeypatch.setattr(updates, "_fetch_releases",
                        Releases([_listed(f"v1.0.{n}", "x" * (updates.NOTES_CHARS + 50)) for n in range(30, 3, -1)]))
    asyncio.run(updates.refresh())
    kept = updates.notes()
    assert [item["version"] for item in kept] == [f"1.0.{n}" for n in range(30, 30 - updates.NOTES_RELEASES, -1)]
    assert all(len(item["notes"]) == updates.NOTES_CHARS for item in kept)


@pytest.mark.parametrize("answer", [None, {"message": "rate limited"}, OSError("unreachable")])
def test_a_list_that_cannot_be_read_is_the_latest_releases_own_notes(monkeypatch, answer):
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release(body="  Four's notes  "))))
    monkeypatch.setattr(updates, "_fetch_releases", Releases(answer))
    asyncio.run(updates.refresh())
    assert updates.notes() == [{"version": "1.0.4", "notes": "Four's notes"}]


def test_no_update_no_notes_and_no_list_asked(monkeypatch):
    monkeypatch.setattr(updates, "_fetch", GitHub((200, "", _release(tag="v1.0.3"))))
    listed = Releases([_listed("v1.0.3", "Three")])
    monkeypatch.setattr(updates, "_fetch_releases", listed)
    asyncio.run(updates.refresh())
    assert updates.notes() == [] and listed.asked == 0
    assert updates.projection(download_offered=False)["notes"] == []


def test_not_modified_keeps_the_notes_and_the_page_reads_them_with_the_record(monkeypatch):
    github = GitHub((200, '"e1"', _release()), (304, '"e1"', None))
    monkeypatch.setattr(updates, "_fetch", github)
    monkeypatch.setattr(updates, "_fetch_releases", Releases([_listed("v1.0.4", "Four")]))
    asyncio.run(updates.refresh())
    asyncio.run(updates.refresh())
    assert github.asked == ["", '"e1"']
    assert updates.projection(download_offered=False)["notes"] == [{"version": "1.0.4", "notes": "Four"}]


# ── the notice (app.js, ui-shell.css) ────────────────────────────────────────

STATIC = ROOT / "frontend" / "static"


def _notice_source() -> str:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    return app.split("function notesInline(parent, text) {", 1)[1].split("// Whether another dialog holds the page", 1)[0]


def test_the_notes_are_drawn_as_text_and_nothing_in_the_notice_links_out():
    notes = _notice_source()
    assert "innerHTML" not in notes and "insertAdjacentHTML" not in notes and "outerHTML" not in notes
    assert "createTextNode" in notes and ".textContent = " in notes
    assert "createElement('a')" not in notes
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function notesLink" not in app and "update.release_notes" in notes
    assert "if (releases.length) body.appendChild(notesDisclosure(releases));" in app
    assert "toggle.setAttribute('aria-expanded', 'false');" in notes and "body.hidden = !open;" in notes


def test_not_now_keeps_the_footers_far_start_and_the_notes_scroll_in_their_own_area():
    css = (STATIC / "ui-shell.css").read_text(encoding="utf-8")
    assert re.search(r"\.dp-update-dialog > \.dp-modal-footer:has\(\[data-modal-accept\]:not\(\[hidden\]\)\) "
                     r"\[data-modal-cancel\] \{\s*order: -1;\s*margin-inline-end: auto;", css)
    assert re.search(r"\.dp-update-notes-body \{[^}]*max-height: min\(50dvh, 420px\);[^}]*overflow-y: auto;", css)
    assert re.search(r'\.dp-update-notes-toggle\[aria-expanded="true"\] \.dp-update-notes-chevron \{ transform: rotate\(90deg\); \}', css)


# ── Windows: the installer closes, updates and restarts DownloadPulse ─────────

INSTALLER = ROOT / "packaging" / "desktop" / "windows" / "installer.nsi"


def test_the_installer_sets_the_event_the_supervisor_waits_on():
    from desktop import supervisor
    nsi = INSTALLER.read_text(encoding="utf-8")
    assert supervisor.QUIT_EVENT == "Local\\DownloadPulse-Quit"
    assert f'OpenEventW(i 0x0002, i 0, w "{supervisor.QUIT_EVENT}")' in nsi
    assert "System::Call 'kernel32::SetEvent(p r3)'" in nsi


def test_a_running_downloadpulse_is_asked_closed_waited_for_and_offered_retry():
    nsi = INSTALLER.read_text(encoding="utf-8")
    take = nsi.split("Function ${UN}TakeDataRootLock", 1)[1].split("FunctionEnd", 1)[0]
    installer, uninstaller = take.split('!if "${UN}" == ""', 1)[1].split("!else", 1)
    assert "Setup will close it, update it and start it again." in installer
    assert "/SD IDOK IDOK dpClose" in installer and "Call CloseRunningDownloadPulse" in installer
    assert "StrCpy $dpRelaunch 1" in installer
    # The uninstaller still asks the person to Quit: it never closes DownloadPulse itself.
    assert "Quit it from its tray menu (Quit), then run this again." in uninstaller
    close = nsi.split("Function CloseRunningDownloadPulse", 1)[1].split("FunctionEnd", 1)[0]
    assert "LockFile(p r1, i 0, i 0, i 1, i 0) i .r2" in close and "Sleep 500" in close
    assert "${LoopUntil} $4 >= 120" in close
    assert "MB_RETRYCANCEL" in close and "IDRETRY dpSignal" in close and "/SD IDCANCEL" in close
    # No quit event (a DownloadPulse older than 1.0.7): asked at once, not after the wait.
    first = close.split("${ElseIf} $5 == 1", 1)[1].split("${EndIf}", 1)[0]
    assert "Goto dpAsk" in first
    assert close.index("${LoopUntil} $4 >= 120") < close.index("dpAsk:") < close.index("MB_RETRYCANCEL")


def test_a_wizard_cancelled_after_closing_downloadpulse_starts_it_again():
    nsi = INSTALLER.read_text(encoding="utf-8")
    assert "!define MUI_CUSTOMFUNCTION_ABORT DpUserAbort" in nsi
    assert nsi.index("!define MUI_CUSTOMFUNCTION_ABORT DpUserAbort") < nsi.index("!insertmacro MUI_PAGE_WELCOME")
    abort = nsi.split("Function DpUserAbort", 1)[1].split("FunctionEnd", 1)[0]
    assert abort.index("Call ReleaseDataRootLock") < abort.index("Call LaunchDownloadPulse")
    assert "${If} $dpRelaunch == 1" in abort


def test_a_silent_update_starts_downloadpulse_again_and_an_interactive_one_offers_it():
    nsi = INSTALLER.read_text(encoding="utf-8")
    success = nsi.split("Function .onInstSuccess", 1)[1].split("FunctionEnd", 1)[0]
    assert success.index("Call ReleaseDataRootLock") < success.index("Call LaunchDownloadPulse")
    assert "${If} ${Silent}" in success and "${AndIf} $dpRelaunch == 1" in success
    assert "!define MUI_FINISHPAGE_RUN_FUNCTION LaunchDownloadPulse" in nsi


def test_the_supervisor_ends_the_run_as_quit_does_when_the_event_is_set():
    source = (ROOT / "desktop" / "supervisor.py").read_text(encoding="utf-8")
    event = source.split("class _WindowsQuitEvent:", 1)[1].split("class Signals:", 1)[0]
    assert "CreateEventW(None, True, False, QUIT_EVENT)" in event and "ResetEvent(self.quit)" in event
    assert 'loop.call_soon_threadsafe(callback, "the installer")' in event
    signals = source.split("class Signals:", 1)[1].split("async def start_failure_notice", 1)[0]
    assert "self.windows = _WindowsQuitEvent(loop, self.signalled)" in signals
    assert "self.windows.close()" in signals


def test_the_quit_event_takes_the_signal_path(monkeypatch):
    """What the event's thread calls is the signal path: serving, it asks the
    loop for the ordered Quit; while quitting, it changes nothing."""
    from desktop import supervisor

    async def scenario():
        signals = supervisor.Signals(asyncio.current_task())
        events: asyncio.Queue = asyncio.Queue()
        signals.serving(events)
        signals.signalled("the installer")
        assert events.get_nowait() == ("quitRequested", {"signal": "the installer"})
        signals.signalled("the installer")
        assert events.empty() and signals.phase == "quitting"
    asyncio.run(scenario())
