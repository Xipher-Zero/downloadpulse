"""1.0.3 (review 2026-11-22): the Linux Flatpak keeps only the folders its
permissions grant. Its home directory exists and is writable without them, but
it is the sandbox's own and gone when the application exits -- files marked
Done vanished. The Download Storage validator (the one selectability owner,
for browsing and for saving) refuses a folder the sandbox does not keep; the
directory browser can still pass through it; a server is unaffected.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import api.settings_validation_routes as directory_routes
from core import desktop_storage
from core.desktop_storage import desktop_start_directory, parse_flatpak_filesystems
from transfers.storage import DiskCapacity, StorageHealthError, StorageReason, StorageState, persists

# A real /.flatpak-info shape: the manifest's grants, the operator's overrides
# (a removed grant, read-only and create modes), and grants that are no place
# for downloads.
FLATPAK_INFO = """[Application]
name=net.downloadpulse.desktop
runtime=runtime/org.freedesktop.Platform/x86_64/24.08

[Instance]
instance-id=1234567890
flatpak-version=1.16.0

[Context]
shared=ipc;network;
sockets=fallback-x11;wayland;x11;
filesystems=xdg-config/gtk-3.0:ro;xdg-download;/run/media;/media:rw;~/Archive:create;host-os;host-etc;!home;xdg-documents/Work;xdg-data/icons:ro;xdg-cache;

[Session Bus Policy]
org.kde.StatusNotifierWatcher=talk
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A user's home reached through a symlink (/home -> var/home, as on
    Silverblue-style hosts), with its user-dirs file naming a moved Downloads."""
    real = tmp_path / "var" / "home" / "una"
    (real / ".config").mkdir(parents=True)
    (real / "Fetched").mkdir()
    (tmp_path / "home").symlink_to(tmp_path / "var" / "home")
    linked = tmp_path / "home" / "una"
    (real / ".config" / "user-dirs.dirs").write_text(
        'XDG_DOWNLOAD_DIR="$HOME/Fetched"\nXDG_DOCUMENTS_DIR="$HOME/Docs"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(linked))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(linked / ".config"))
    return linked, real


def test_the_writable_grants_are_the_folders_it_keeps(home):
    linked, real = home
    roots = parse_flatpak_filesystems(FLATPAK_INFO)
    assert roots == (real / "Fetched", Path("/run/media").resolve(), Path("/media").resolve(), real / "Archive",
                     real / "Docs" / "Work", real / ".var" / "app" / "net.downloadpulse.desktop")
    # Never the home (its grant was removed), the host's own files, or config/data/cache.
    assert real not in roots and Path("/") not in roots and not any("run/host" in str(root) for root in roots)


def test_a_folder_persists_only_inside_a_root_by_whole_components(home):
    _linked, real = home
    roots = (real / "Fetched", Path("/run/media"))
    assert persists(real / "Fetched", roots) and persists(real / "Fetched" / "a" / "b", roots)
    assert not persists(real, roots) and not persists(real / "Fetched2", roots)
    assert not persists(Path("/run/media2"), roots)
    assert persists(Path("/anything"), None)


def test_only_the_sandboxed_desktop_is_restricted(tmp_path, monkeypatch):
    info = tmp_path / "flatpak-info"
    info.write_text(FLATPAK_INFO, encoding="utf-8")
    monkeypatch.setattr(desktop_storage, "FLATPAK_INFO", info)
    for desktop, present, restricted in ((True, True, True), (False, True, False), (True, False, False)):
        monkeypatch.setenv("DOWNLOADPULSE_DESKTOP", "1" if desktop else "0")
        monkeypatch.setattr(desktop_storage, "FLATPAK_INFO", info if present else tmp_path / "absent")
        desktop_storage.sandbox_persistent_roots.cache_clear()
        assert (desktop_storage.sandbox_persistent_roots() is not None) is restricted
    desktop_storage.sandbox_persistent_roots.cache_clear()


def _capacity(tmp_path, roots):
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    return DiskCapacity(tmp_path / "unset", application_path=app / "db.sqlite", persistent_roots=roots)


def test_a_folder_the_sandbox_does_not_keep_is_refused_without_writing_there(tmp_path, home):
    linked, real = home
    capacity = _capacity(tmp_path, parse_flatpak_filesystems(FLATPAK_INFO))
    lost = capacity.validate_download_path(linked)          # the home, through its symlink
    assert (lost.state, lost.reason) == (StorageState.UNAVAILABLE, StorageReason.NOT_PERSISTENT)
    assert lost.exists is True and lost.accessible is True and lost.writable is None
    assert not [entry for entry in real.iterdir() if entry.name.startswith(".downloadpulse-probe-")]
    kept = capacity.validate_download_path(linked / "Fetched")
    assert (kept.state, kept.reason) == (StorageState.HEALTHY, StorageReason.NONE)
    # Saving goes through the same validator: refused.
    with pytest.raises(StorageHealthError):
        capacity.require_download_path(linked)
    # Missing still reads as missing, before anything about the sandbox.
    assert capacity.validate_download_path(linked / "nowhere").reason == StorageReason.MISSING


def test_an_unrestricted_installation_is_unchanged(tmp_path, home):
    linked, _real = home
    snapshot = _capacity(tmp_path, None).validate_download_path(linked)
    assert (snapshot.state, snapshot.reason) == (StorageState.HEALTHY, StorageReason.NONE)


def test_an_upgraded_install_whose_folder_is_not_kept_stops_downloading_and_says_why(tmp_path, home):
    linked, _real = home
    app = tmp_path / "app"
    app.mkdir()
    capacity = DiskCapacity(linked, application_path=app / "db.sqlite",
                            persistent_roots=parse_flatpak_filesystems(FLATPAK_INFO))
    capacity.check()
    assert capacity.download_work_permitted is False
    assert capacity.health()["download"]["reason"] == "not_persistent"


def test_the_browser_passes_through_a_folder_it_may_not_choose(tmp_path, home):
    linked, real = home
    capacity = _capacity(tmp_path, parse_flatpak_filesystems(FLATPAK_INFO))
    listed = directory_routes._browse_directory(linked.resolve(), capacity)
    assert listed.current.selectable is False and listed.current.reason == "not_persistent"
    assert "Fetched" in [child.name for child in listed.children]
    chosen = directory_routes._browse_directory((linked / "Fetched").resolve(), capacity)
    assert chosen.current.selectable is True


def test_the_start_is_the_users_downloads_or_a_kept_folder_never_the_lost_home(tmp_path, home):
    linked, real = home
    roots = parse_flatpak_filesystems(FLATPAK_INFO)
    assert desktop_start_directory(roots) == real / "Fetched"
    (real / "Fetched").rmdir()
    started = desktop_start_directory(roots)
    assert started != real and started is not None and persists(started, roots)
    # Nowhere kept exists: no start (the caller opens the filesystem root, which is not selectable).
    assert desktop_start_directory((tmp_path / "gone",)) is None
    # Unrestricted: the home, as before, when Downloads is missing.
    assert desktop_start_directory(None) == real


STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"


def test_the_desktop_names_its_own_folder_and_its_wizard_offers_the_suggestion():
    picker = (STATIC / "ui-settings-directory-picker.js").read_text(encoding="utf-8")
    # The label's key follows the application (and survives a language change: it is the data-i18n key).
    assert ("window.downloadPulseDesktop === true ? 'directory_picker.current_folder' : 'directory_picker.current'"
            in picker)
    assert 'data-i18n="${currentKey}"' in picker and "'not_persistent'" in picker
    wizard = (STATIC / "ui-setup-wizard.js").read_text(encoding="utf-8")
    # A draft for the person to keep or change: the saved folder stays unchosen until Next writes it.
    assert "data = {...data, folder: '', folderDraft: String(state.suggested_download_folder ?? '')};" in wizard
