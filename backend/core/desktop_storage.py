"""Where an installed desktop application may keep downloads.

The user's own Downloads folder, as the system names it, and -- inside the
Linux Flatpak -- the folders its sandbox actually keeps. A Flatpak's home
directory exists and is writable even without the ``home`` permission, but it
is the sandbox's own and gone when the application exits (1.0.3, review
2026-11-22: files marked Done vanished). Only the folders granted by the
sandbox's permissions persist; ``/.flatpak-info`` states them, overrides
included. Anywhere else nothing is restricted.

The one owner of these facts: the Download Storage validator
(``transfers.storage``) refuses a folder outside them, and the directory
browser and the setup wizard start from ``desktop_start_directory``.
"""
from __future__ import annotations

import configparser
import functools
import os
import sys
from pathlib import Path

FLATPAK_INFO = Path("/.flatpak-info")

# xdg-<name> filesystem grants naming a user directory (user-dirs.dirs keys).
_USER_DIRS = {
    "xdg-desktop": "DESKTOP", "xdg-documents": "DOCUMENTS", "xdg-download": "DOWNLOAD", "xdg-music": "MUSIC",
    "xdg-pictures": "PICTURES", "xdg-public-share": "PUBLICSHARE", "xdg-templates": "TEMPLATES",
    "xdg-videos": "VIDEOS",
}
_USER_DIR_DEFAULTS = {"DESKTOP": "Desktop", "DOCUMENTS": "Documents", "DOWNLOAD": "Downloads", "MUSIC": "Music",
                      "PICTURES": "Pictures", "PUBLICSHARE": "Public", "TEMPLATES": "Templates", "VIDEOS": "Videos"}


def _user_dir(name: str) -> Path | None:
    """A user directory as xdg-user-dirs names it (``user-dirs.dirs``; inside the
    Flatpak, the sandbox's copy of it), else its default under the home."""
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "user-dirs.dirs"
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    for line in lines:   # XDG_DOWNLOAD_DIR="$HOME/Downloads" (xdg-user-dirs' own format)
        key, _, value = line.strip().partition("=")
        if key == f"XDG_{name}_DIR" and len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
            if value.startswith("$HOME"):
                return home / value[len("$HOME"):].lstrip("/")
            return Path(value) if value.startswith("/") else None
    return home / _USER_DIR_DEFAULTS[name]


def user_downloads_folder() -> Path | None:
    """The running user's own Downloads folder, as the system names it:
    Windows' Downloads known folder (``SHGetKnownFolderPath``, wherever it was
    moved), the XDG user directory on Linux, ``~/Downloads`` on macOS. ``None``
    when the system names none."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        shell = ctypes.WinDLL("shell32")
        ole = ctypes.WinDLL("ole32")
        shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.c_void_p]
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        downloads = (ctypes.c_ubyte * 16).from_buffer_copy(   # FOLDERID_Downloads {374DE290-123F-4565-9164-39C4925E467B}
            bytes.fromhex("90e24d373f126545916439c4925e467b"))
        value = ctypes.c_void_p()
        if shell.SHGetKnownFolderPath(ctypes.byref(downloads), 0, None, ctypes.byref(value)) != 0:
            return None
        try:
            return Path(ctypes.wstring_at(value.value))
        finally:
            ole.CoTaskMemFree(value)
    if sys.platform == "darwin":
        return Path.home() / "Downloads"
    return _user_dir("DOWNLOAD")


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path


def parse_flatpak_filesystems(text: str) -> tuple[Path, ...]:
    """The folders a Flatpak's ``[Context] filesystems`` grant lets it keep files in.

    Writable grants only: ``:ro`` is skipped, ``:rw``/``:create`` (or none) kept,
    a negated ``!grant`` (an override taking one away) never counts. ``host``
    is the whole filesystem; ``home`` and ``~/x`` the home directory or below
    it; ``xdg-download`` and the other user directories (``/sub`` allowed)
    where xdg-user-dirs names them; an absolute path itself. ``host-os``,
    ``host-etc`` and the ``xdg-config``/``xdg-data``/``xdg-cache``/``xdg-run``
    grants are not places for downloads. The application's own data directory
    (``~/.var/app/<id>``) always persists and is included."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
        grants = parser.get("Context", "filesystems", fallback="")
        app_id = parser.get("Application", "name", fallback="")
    except configparser.Error:
        grants, app_id = "", ""
    home = Path.home()
    roots: list[Path] = []
    for raw in grants.split(";"):
        grant = raw.strip()
        if not grant or grant.startswith("!"):
            continue
        name, _, mode = grant.rpartition(":") if ":" in grant else (grant, "", "")
        if mode not in {"", "rw", "create"}:
            continue
        if name == "host":
            roots.append(Path("/"))
        elif name == "home":
            roots.append(home)
        elif name.startswith("~/"):
            roots.append(home / name[2:])
        elif name.startswith("/"):
            roots.append(Path(name))
        else:
            base, _, below = name.partition("/")
            if base in _USER_DIRS:
                folder = _user_dir(_USER_DIRS[base])
                if folder is not None:
                    roots.append(folder / below if below else folder)
    if app_id:
        roots.append(home / ".var" / "app" / app_id)
    return tuple(dict.fromkeys(_resolved(root) for root in roots))


@functools.lru_cache(maxsize=1)
def sandbox_persistent_roots() -> tuple[Path, ...] | None:
    """The folders the installed desktop application's sandbox keeps, resolved;
    ``None`` when nothing is restricted (not the desktop application, or not
    sandboxed). A sandbox's permissions cannot change while it runs, so this is
    read once."""
    from core.config import desktop_installation
    if not desktop_installation():
        return None
    try:
        text = FLATPAK_INFO.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_flatpak_filesystems(text)


def persists(path, roots: tuple[Path, ...] | None) -> bool:
    """Whether files kept at ``path`` (already resolved) outlive the application:
    always when ``roots`` is ``None``, else inside one of them, by whole path
    components (never a string prefix)."""
    if roots is None:
        return True
    candidate = Path(path)
    return any(candidate == root or candidate.is_relative_to(root) for root in roots)


def desktop_start_directory(roots: tuple[Path, ...] | None = None) -> Path | None:
    """Where an installed desktop's Download Folder browser opens, and what its
    setup wizard suggests, before a folder has been chosen: the user's
    Downloads folder, else their home, else (sandboxed) the first folder the
    sandbox keeps -- each only if it exists, can be listed and persists.
    ``None`` when there is none (the caller starts at the filesystem root).
    Only a starting point: nothing is chosen or saved."""
    if roots is None:
        roots = sandbox_persistent_roots()
    for candidate in (user_downloads_folder(), Path.home(), *(roots or ())):
        if candidate is None or not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_dir() or not persists(resolved, roots):
            continue
        try:
            with os.scandir(resolved) as entries:
                next(entries, None)
        except OSError:
            continue
        return resolved
    return None
