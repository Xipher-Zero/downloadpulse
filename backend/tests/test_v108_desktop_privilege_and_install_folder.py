"""1.0.8 WS-A6: DownloadPulse runs with its account's own rights, and the
Windows installer touches only its own folder.

* The supervisor refuses a UAC-elevated token (Windows) or root (Linux,
  macOS) before it opens, creates or locks anything in the data root, says so
  on standard error and in the start-failure notice (its own words, in every
  catalog); ``DOWNLOADPULSE_DESKTOP_ALLOW_PRIVILEGED=1`` allows it on a
  disposable system (the Flatpak's root-only launch check).
* An installer started "as administrator" starts DownloadPulse through the
  desktop's unelevated Explorer (test_v114_windows_installer_ui pins the
  command).
* The install folder is inside %LOCALAPPDATA% (outside the data root), and an
  ``app`` folder that is not DownloadPulse's is never removed or replaced.
* The WebView2 bootstrapper is signature-checked like the Redistributable
  (test_v114_desktop_artifacts_contract).
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop import supervisor  # noqa: E402

NSI = (ROOT / "packaging" / "desktop" / "windows" / "installer.nsi").read_text(encoding="utf-8")


def _function(name: str) -> str:
    return re.search(rf"Function {re.escape(name)}\n(.*?)FunctionEnd", NSI, re.S).group(1)


# ── the supervisor ──────────────────────────────────────────────────────────

class _Api:
    def __init__(self, kind=None, error=None):
        self.kind, self.error = kind, error

    def elevation_type(self):
        if self.error:
            raise self.error
        return self.kind


@pytest.mark.parametrize("kind, refused", [(1, False), (2, True), (3, False)])
def test_windows_refuses_only_a_token_uac_elevated(monkeypatch, kind, refused):
    """2 is TokenElevationTypeFull (Run as administrator); 3 the same
    account's limited token; 1 a token with no split (UAC off, the built-in
    Administrator, a standard account), which has no lesser token to run with."""
    monkeypatch.setattr(supervisor, "_win32_api", lambda: _Api(kind))
    assert (supervisor.privileged("win32") is not None) is refused


def test_windows_refuses_a_token_whose_elevation_cannot_be_read(monkeypatch):
    monkeypatch.setattr(supervisor, "_win32_api", lambda: _Api(error=PermissionError("no token")))
    assert "cannot be read" in supervisor.privileged("win32")


@pytest.mark.only_on(sys.platform != "win32", reason="POSIX: the effective user id")
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_posix_refuses_root_only(monkeypatch, platform):
    monkeypatch.setattr(supervisor.os, "geteuid", lambda: 0)
    assert supervisor.privileged(platform) == "root"
    monkeypatch.setattr(supervisor.os, "geteuid", lambda: 1000)
    assert supervisor.privileged(platform) is None


@pytest.mark.only_on(sys.platform == "win32", reason="Windows: this process's own token")
def test_this_process_s_elevation_is_read_from_windows():
    assert supervisor._win32_api().elevation_type() in (1, 2, 3)


def _isolated(monkeypatch, tmp_path, what):
    """A run whose data root is under ``tmp_path`` and whose privilege is ``what``."""
    root = tmp_path / "data-root"
    root.parent.mkdir(exist_ok=True)
    monkeypatch.setattr(supervisor, "data_root", lambda: root)
    monkeypatch.setattr(supervisor, "privileged", lambda: what)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
    monkeypatch.setenv("STATIC_DIR", str(ROOT / "frontend" / "static"))
    notices = []

    async def notice(words, platform=sys.platform, *, body="desktop.error.start_failed"):
        notices.append((words("desktop.error.start_failed_title"), words(body)))
    monkeypatch.setattr(supervisor, "start_failure_notice", notice)
    return root, notices


def test_a_privileged_run_is_refused_before_it_touches_the_data_root(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv(supervisor.PRIVILEGED_OVERRIDE, raising=False)
    root, notices = _isolated(monkeypatch, tmp_path, "root")
    opened = []
    monkeypatch.setattr(supervisor, "single_instance", lambda path: opened.append(path))
    with caplog.at_level("ERROR", logger=supervisor.logger.name):
        assert asyncio.run(supervisor.run()) == supervisor.PRIVILEGED_EXIT == 4
    assert not root.exists() and opened == []                       # no lock, no log, no directory
    assert any(record.getMessage().startswith("DownloadPulse does not run as root: ")
               and "Nothing was started or written" in record.getMessage() for record in caplog.records)
    english = json.loads((ROOT / "frontend" / "static" / "i18n" / "en.json").read_text(encoding="utf-8"))["messages"]
    assert notices == [(english["desktop.error.start_failed_title"], english["desktop.error.privileged"])]


def test_the_refusal_s_notice_speaks_the_installation_s_language(monkeypatch, tmp_path):
    monkeypatch.delenv(supervisor.PRIVILEGED_OVERRIDE, raising=False)
    root, notices = _isolated(monkeypatch, tmp_path, "an elevated (administrator) token")
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.json").write_text(json.dumps({"default_locale": "de"}), encoding="utf-8")
    assert asyncio.run(supervisor.run()) == 4
    german = json.loads((ROOT / "frontend" / "static" / "i18n" / "de.json").read_text(encoding="utf-8"))["messages"]
    assert notices == [(german["desktop.error.start_failed_title"], german["desktop.error.privileged"])]
    assert sorted(path.name for path in root.iterdir()) == ["config"]   # read, nothing added


def test_the_override_lets_a_privileged_run_go_on_and_says_so(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv(supervisor.PRIVILEGED_OVERRIDE, "1")
    root, notices = _isolated(monkeypatch, tmp_path, "root")
    monkeypatch.setattr(supervisor, "single_instance", lambda path: None)   # stop at the lock: "already running"

    async def inactive():
        return False
    monkeypatch.setattr(supervisor.SessionBus, "activate_running", inactive, raising=False)
    handlers = list(supervisor.logger.handlers)
    try:
        with caplog.at_level("INFO", logger=supervisor.logger.name):
            assert asyncio.run(supervisor.run()) == 3
    finally:
        for handler in supervisor.logger.handlers[len(handlers):]:
            supervisor.logger.removeHandler(handler)
            handler.close()
    assert notices == []
    assert not any("does not run as" in record.getMessage() for record in caplog.records)


def test_the_refusal_comes_before_the_data_root_and_its_lock():
    source = (ROOT / "desktop" / "supervisor.py").read_text(encoding="utf-8")
    run = source.split("async def run() -> int:", 1)[1].split("\nasync def ", 1)[0]
    assert run.index("privileged()") < run.index("refuse_privileged") < run.index("data_root()") \
        < run.index("single_instance(") < run.index("diagnostics_handler(")


def test_every_catalog_says_the_refusal():
    for path in (ROOT / "frontend" / "static" / "i18n").glob("*.json"):
        if path.name == "manifest.json":
            continue
        messages = json.loads(path.read_text(encoding="utf-8"))["messages"]
        assert "DownloadPulse" in messages["desktop.error.privileged"], path.name


# ── the installer's folder ─────────────────────────────────────────────────

def test_the_install_folder_is_inside_local_app_data_and_outside_the_data_root():
    allowed = _function("DpInstDirAllowed")
    assert "kernel32::GetFullPathNameW(w \"$INSTDIR\"" in allowed           # "..", "." and "/" resolved first
    assert '${If} $2 == "$LOCALAPPDATA\\"' in allowed                       # a folder inside it ...
    assert '${If} $2 != ""' in allowed                                      # ... never the folder itself
    assert '${If} $2 == "$LOCALAPPDATA\\DownloadPulse\\"' in allowed        # nor the data root, or in it
    assert '${If} $LOCALAPPDATA != ""' in allowed
    assert "Call ProgramFilesOurs" in allowed and "StrCpy $dpInstDirOk 2" in allowed
    verify = _function(".onVerifyInstDir")
    assert "Call DpInstDirAllowed" in verify and "Abort" in verify          # Next stays disabled
    assert "!define MUI_DIRECTORYPAGE_TEXT_TOP" in NSI
    assert NSI.index("!define MUI_DIRECTORYPAGE_TEXT_TOP") < NSI.index("!insertmacro MUI_PAGE_DIRECTORY")


def test_a_silent_install_checks_the_folder_before_the_lock_and_refuses_with_exit_4():
    on_init = _function(".onInit")
    silent = on_init.split("${If} ${Silent}", 1)[1].split("${EndIf}\n  ${EndIf}", 1)[0]
    assert "Call DpInstDirAllowed" in silent and "Call RefuseInstDir" in silent
    assert on_init.index("Call RequireVcRedist") < on_init.index("Call DpInstDirAllowed") \
        < on_init.index("Call TakeDataRootLock")
    refuse = _function("RefuseInstDir")
    assert "Call TellCaller" in refuse and refuse.index("SetErrorLevel 4") < refuse.index("Quit")
    assert "Nothing was changed." in refuse
    # And once more before the first prerequisite or file is written.
    prerequisites = NSI.split('Section "Prerequisites" SecPrerequisites', 1)[1].split("SectionEnd", 1)[0]
    assert prerequisites.index("Call DpInstDirAllowed") < prerequisites.index("InitPluginsDir")


def test_only_downloadpulse_s_own_app_folder_is_ever_removed():
    ours = NSI.split("Function ${UN}ProgramFilesOurs", 1)[1].split("FunctionEnd", 1)[0]
    assert 'ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "InstallLocation"' in ours and "${AndIf} $0 == $INSTDIR" in ours
    assert '${ElseIf} ${FileExists} "$INSTDIR\\app\\desktop\\supervisor.py"' in ours
    assert '${AndIf} ${FileExists} "$INSTDIR\\app\\runtime\\python\\pythonw.exe"' in ours
    remove = NSI.split("Function ${UN}RemoveProgramFiles", 1)[1].split("FunctionEnd", 1)[0]
    assert remove.index("Call ${UN}ProgramFilesOurs") < remove.index("${If} $dpOurs != 1") \
        < remove.index("Return") < remove.index('RMDir /r "$INSTDIR\\app"')
    assert NSI.count('RMDir /r "$INSTDIR\\app"') == 1                       # that one guarded removal only
    install = NSI.split('Section "DownloadPulse" SecMain', 1)[1].split("SectionEnd", 1)[0]
    assert install.index("Call ProgramFilesOurs") < install.index("Abort") < install.index("Call RemoveProgramFiles") \
        < install.index("WriteUninstaller")
    uninstall = NSI.split('Section "Uninstall"', 1)[1].split("SectionEnd", 1)[0]
    assert uninstall.index("Call un.ProgramFilesOurs") < uninstall.index("Abort") \
        < uninstall.index('Delete "$SMPROGRAMS\\DownloadPulse.lnk"') < uninstall.index("Call un.RemoveProgramFiles")


def test_the_installer_s_elevation_test_is_the_supervisor_s():
    elevated = _function("DpElevated")
    assert "advapi32::OpenProcessToken(p r1, i 0x8, *p 0 r1)" in elevated
    assert "advapi32::GetTokenInformation(p r1, i 18, *i 0 r2, i 4, *i 0)" in elevated   # TokenElevationType
    assert "${AndIf} $2 == 2" in elevated                                   # TokenElevationTypeFull
    source = (ROOT / "desktop" / "supervisor.py").read_text(encoding="utf-8")
    assert "adv.GetTokenInformation(handle, 18," in source and "if kind == 2 else None" in source
