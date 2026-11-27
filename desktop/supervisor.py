"""The DownloadPulse desktop supervisor: the thin native shim around the one
DownloadPulse backend of an installed desktop application.

It owns, and only owns, what a desktop process needs and DownloadPulse does not:

* the per-user data root (``data_root``) the backend's ``DB_PATH`` and
  ``CONFIG_PATH`` name, and one supervisor per data root (an exclusive lock);
* the backend process: started through DownloadPulse's own process ownership
  (``executors.process_ownership``, the WS2 owner) on a free loopback port, in
  desktop mode (``DOWNLOADPULSE_DESKTOP=1``), with the backend's private control
  channel (``application.desktop_control``): two anonymous pipes only the two
  processes hold;
* the native window, tray and notices: a Neutralino process whose page is the
  backend's own loopback interface, with no native token or library injected
  into it; the supervisor alone holds Neutralino's exported credential and
  calls its allow-listed native methods. The shell reads its resources from
  its bundle only (``shell_bundle``), and the supervisor takes the one-time
  access key Neutralino would give its first asker (``Shell._take_globals``).
  On Linux the tray and the notices are
  the desktop's own session-bus interfaces instead (``SessionBus``), and so is
  opening the running instance again;
* Show in folder: a completed download the window's page asked to see, as the
  backend itself resolved it, opened in the desktop's own file manager
  (``show_in_folder``: a fixed command or the OpenURI portal, never a shell);
* Quit: the native shell ends first, in order (``_end_shell``: its own
  ``app.exit`` on macOS, SIGTERM on Linux, its job on Windows; a stuck shell
  is stopped by the process owner), and its window and every connection its
  page holds go with it; then
  closing the control channel makes the backend stop itself through its own
  shutdown, bounded by uvicorn's graceful-shutdown timeout for any other open
  request; only a backend that does not end in time is ended forcibly (its
  process group or job), the crash backstop, and the log says so. Quit is the
  tray's, macOS's Cmd+Q, a termination signal (``Signals``: SIGTERM, SIGINT,
  SIGHUP on POSIX) or -- where no tray can open the window again (Linux) --
  closing the window, once its own page has asked and the person confirmed;
* a start that fails: logged with its cause, and said in a notice where one
  can be shown (``start_failure_notice``); a run with administrator or root
  rights is refused before it touches the data root (``privileged``).

It reads no attempt marker, keeps no transfer state of its own, and calls no
DownloadPulse HTTP API but the public health check: the tray's status, events
and Pause/Resume All travel on the private channel to their canonical owners.
Nothing it does signs a session in or unlocks the interface. What the native
side refuses or fails to do is logged (``logs/desktop.log``), never assumed
done. Its own log and the backend's and shell's output (``backend.log``,
``shell.log``) are size-bounded: each rotates at LOG_BYTES, keeping LOG_BACKUPS.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_ID = "net.downloadpulse.desktop"
BACKEND_ID = "desktop-backend"
SHELL_ID = "desktop-shell"
READY_SECONDS = 120
# Linux, the tray the supervisor's own: a window hidden this long is released --
# its shell ended to free its memory -- once its page says nothing would be lost
# (1.0.5 WS8d); a page that says otherwise is asked again after the retry.
RELEASE_AFTER_SECONDS = 600
RELEASE_RETRY_SECONDS = 60
# A relaunch's own bound (Open after a release), well inside READY_SECONDS.
RELAUNCH_SECONDS = 20
# How long a spawn waits for the interface's mode (its background) before opening on the default.
APPEARANCE_SECONDS = 2
# The shell's run-scoped cookie store (DOWNLOADPULSE_COOKIE_STORE): the sign-in
# outlives a relaunch within one run, never a new run.
COOKIE_STORE = "shell-cookies"
QUIT_SECONDS = 60
SHELL_EXIT_SECONDS = 10
# How long the backend's shutdown waits for a request still open (an event
# stream of a page that is not the shell's) before uvicorn cancels it and goes
# on with DownloadPulse's own shutdown.
GRACEFUL_SECONDS = 5
POLL_SECONDS = 3.0
# How soon a connected browser's Open DownloadPulse brings the window forward:
# the backend's ``show`` is asked this often (one short line each way).
SHOW_POLL_SECONDS = 0.5
NATIVE_METHODS = ("os.setTray", "os.showNotification", "window.show", "window.hide", "window.focus",
                  "window.unminimize", "window.minimize", "window.setMainMenu", "os.showSaveDialog", "app.exit",
                  "window.setFrameTheme",
                  # The window's geometry, read at a release; a relaunch is started at it
                  # (``relaunch_arguments``), so nothing sets it afterwards (1.0.5 WS8d).
                  "window.getSize", "window.isMaximized",
                  # Whether a parked window was shown by the system itself (macOS: the Dock).
                  "window.isVisible")
# How long a Save dialog may stay open: the person's own time.
DIALOG_SECONDS = 600
# How long Quit waits for the loop's background work once it is cancelled.
BACKGROUND_END_SECONDS = 2
# Each log file's bound, and how many earlier files are kept beside it.
LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
logger = logging.getLogger("downloadpulse.desktop.supervisor")


# ── layout ─────────────────────────────────────────────────────────────────

def layout() -> dict[str, Path]:
    """Where the bundled pieces are, relative to this file: the application
    (``backend``, ``frontend/static``), the bundled tools (``tools``) and the
    native shell (``shell``). ``DOWNLOADPULSE_DESKTOP_LAYOUT`` names another
    root for a development run."""
    root = Path(os.environ.get("DOWNLOADPULSE_DESKTOP_LAYOUT") or HERE.parent).resolve()
    return {"root": root, "backend": root / "backend", "static": root / "frontend" / "static",
            "tools": root / "tools", "shell": root / "desktop" / "shell"}


def data_root() -> Path:
    """The per-user application data root, outside the program files:
    Windows LocalAppData, macOS Application Support, XDG data (Flatpak's own
    writable data) elsewhere."""
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "DownloadPulse"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DownloadPulse"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "downloadpulse"


def shell_data_home() -> Path:
    """Neutralino's own data home for ``dataLocation: system`` (its
    ``sago::getDataHome``): where it exports its native credential. On
    Windows, the Roaming AppData known folder, asked of Windows itself
    (``SHGetKnownFolderPath``) as Neutralino does -- not an environment
    variable."""
    if os.name == "nt":
        return Path(_win32_api().known_folders()[1])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


# Windows: the trustees a checked object's ACEs may name -- this account,
# SYSTEM, Administrators, OWNER RIGHTS (the owner, verified to be one of
# them) and, on inherit-only ACEs, CREATOR OWNER (whoever creates a child:
# inside a private directory, a trusted account).
_SYSTEM, _ADMINISTRATORS, _OWNER_RIGHTS, _CREATOR_OWNER = "S-1-5-18", "S-1-5-32-544", "S-1-3-4", "S-1-3-0"
# The only rights anyone else may hold on a directory ABOVE the credential's
# own (the profile's parent, the profile, every directory down to the data
# home): list/read, read extended attributes, traverse, read attributes, read
# the security descriptor, synchronize, generic read and execute. Any other
# right -- to add, delete or rename an entry, write data or attributes (a
# reparse point), change the DACL or the owner -- could replace or redirect
# the path, and fails closed.
_READ_ONLY = 0x1 | 0x8 | 0x20 | 0x80 | 0x00020000 | 0x00100000 | 0x80000000 | 0x20000000
# Above the profiles root (the volume root down to the profiles root's
# parent), Windows lets ordinary accounts add entries (e.g. Authenticated
# Users: Modify on C:\). What would replace or rename an EXISTING entry of the
# path is: on the entry, DELETE (moot on a volume root), WRITE_DAC,
# WRITE_OWNER, GENERIC_ALL; on its parent, FILE_DELETE_CHILD or GENERIC_ALL.
_DELETE, _REPLACE_SELF, _REPLACE_CHILD = 0x00010000, 0x00010000 | 0x00040000 | 0x00080000 | 0x10000000, 0x40 | 0x10000000
_TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"


def private_directory(path: Path) -> None:
    """Make ``path`` -- the directory Neutralino exports its native credential
    into -- and its parent (the application's own data directory) private to
    this OS account BEFORE the credential is written; or raise
    ``PermissionError`` (the shell is then not started). Neutralino itself
    creates them world-readable (0755, the file 0644), leaving the
    credential's privacy to whatever the home directory happens to be.

    POSIX: each is a real directory (never a link) owned by this account, mode
    0700.

    Windows -- the one supported layout: this account's profile
    (``GetUserProfileDirectoryW``) is a direct child of the system's profiles
    root (``GetProfilesDirectoryW``, e.g. ``C:\\Users``), and the data home
    (the Roaming known folder) lies inside the profile. Starting from the
    profiles root's volume root, nothing on the path may be replaceable or
    redirectable by another account:

    * the volume root down to the profiles root's parent: real directories,
      never reparse points, each resolving to itself, owned by this account,
      SYSTEM, Administrators or TrustedInstaller, on which nobody else holds a
      right that could replace or rename the next entry (``_REPLACE_SELF`` on
      it, ``_REPLACE_CHILD`` on its parent);
    * the profiles root, the profile and every directory below it down to the
      data home: real directories, never reparse points, owned by this
      account, SYSTEM or Administrators (TrustedInstaller above the profile),
      on which nobody else holds any right beyond reading and traversing
      (``_READ_ONLY``) -- Windows itself grants such rights there (Users,
      Everyone, capability SIDs); the profiles root and the profile resolve to
      themselves;
    * the application directory and ``.tmp``: real directories, never reparse
      points, owned by this account, whose every allow ACE names this account,
      SYSTEM, Administrators or OWNER RIGHTS (CREATOR OWNER on inherit-only
      ACEs); ``.tmp`` must also hand those to every file created in it (at
      least one file-inheriting allow ACE) -- otherwise Windows gives a new file
      the creating token's default DACL, which names the logon session too.

    A nested or relocated profile, a data home outside the profile, a
    redirected path, an intermediate junction or mount point, or an ACL that
    names anyone else is refused, not repaired."""
    if os.name != "nt":
        for directory in (path.parent, path):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            import stat
            info = os.lstat(directory)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise PermissionError(f"{directory} is not a directory of this account")
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fchmod(descriptor, 0o700)
                if os.fstat(descriptor).st_mode & 0o077:
                    raise PermissionError(f"{directory} cannot be made private")
            finally:
                os.close(descriptor)
        return
    api = _win32_api()
    account = api.account_sid()
    trusted = {account, _SYSTEM, _ADMINISTRATORS}
    profile = Path(api.known_folders()[0])
    profiles = Path(api.profiles_directory())
    if os.path.normcase(str(profile.parent)) != os.path.normcase(str(profiles)):
        raise PermissionError(f"{profile} is not a direct child of the profiles root ({profiles}): "
                              f"a nested or relocated profile is not supported")
    application, tmp = path.parent, path
    chain = [Path(os.path.abspath(application.parent))]   # the data home ...
    if os.path.normcase(str(chain[0])) != os.path.normcase(str(profile)):
        if not os.path.normcase(str(chain[0])).startswith(os.path.normcase(str(profile)) + os.sep):
            raise PermissionError(f"{chain[0]} is not inside this account's profile ({profile}): not supported")
        while os.path.normcase(str(chain[-1].parent)) != os.path.normcase(str(profile)):
            chain.append(chain[-1].parent)                # ... and every directory up to the profile
        chain.append(profile)
    chain.reverse()                                       # profile first, data home last

    def facts(target: Path, *, directory: bool = True):
        handle = api.open(target, directory=directory)
        try:
            attributes = handle.attributes()
            owner, aces = handle.security()
            final = handle.final_path()
        finally:
            handle.close()
        if attributes & 0x400:                            # FILE_ATTRIBUTE_REPARSE_POINT
            raise PermissionError(f"{target} is a reparse point")
        if not attributes & 0x10:
            raise PermissionError(f"{target} is not a directory")
        if aces is None:
            raise PermissionError(f"{target} has no DACL (open to everyone)")
        return owner, aces, final

    def checked(target: Path, owners: set[str], rule: str) -> str:
        """``target``'s facts against ``rule`` ("unreplaceable", "read-only" or
        "private"); its final path."""
        owner, aces, final = facts(target)
        if owner not in owners:
            raise PermissionError(f"{target} is owned by {owner}")
        if rule != "private" and os.path.normcase(final) != os.path.normcase(str(target)):
            raise PermissionError(f"{target} resolves to {final}: a redirected path is not supported")
        root = target.parent == target
        for kind, flags, mask, trustee in aces:
            if kind not in (0, 1):                        # only ACCESS_ALLOWED / ACCESS_DENIED
                raise PermissionError(f"{target} has an ACE of type {kind}")
            if kind == 1 or trustee in owners or trustee in trusted or trustee == _OWNER_RIGHTS:
                continue
            if rule == "private":
                if not (trustee == _CREATOR_OWNER and flags & 0x08):   # INHERIT_ONLY
                    raise PermissionError(f"{target} grants access to {trustee}")
            elif flags & 0x08:                            # an inherit-only ACE grants nothing on this object
                continue
            elif rule == "read-only" and mask & ~_READ_ONLY:
                raise PermissionError(f"{target} grants {trustee} rights to change it (0x{mask:08x})")
            elif rule == "unreplaceable" and mask & ((_REPLACE_SELF & ~(_DELETE if root else 0)) | _REPLACE_CHILD):
                raise PermissionError(f"{target} lets {trustee} replace or rename an entry of the path (0x{mask:08x})")
        if rule == "private" and target == tmp and not any(
                kind == 0 and flags & 0x01 for kind, flags, _mask, _trustee in aces):   # OBJECT_INHERIT
            raise PermissionError(f"{target} does not hand its private ACL to the files created in it")
        return final

    # The trusted starting point: the profiles root's volume root, down to it.
    for ancestor in (*reversed(profiles.parents), ):
        checked(ancestor, trusted | {_TRUSTED_INSTALLER}, "unreplaceable")
    checked(profiles, trusted | {_TRUSTED_INSTALLER}, "read-only")
    # The profile and everything down to the data home.
    for directory in chain:
        checked(directory, trusted, "read-only")
    for directory in (application, tmp):                 # the credential's own: private
        try:
            directory.mkdir(mode=0o700)   # CPython: an owner-only, protected DACL
        except FileExistsError:
            pass
        checked(directory, {account}, "private")


def read_credential(path: Path) -> dict:
    """Neutralino's exported credential: a regular file of this account in the
    protected directory, opened without following a link (POSIX) or a reparse
    point (Windows), read once and deleted -- on Windows through the very
    handle that was verified, after the whole path is verified again
    (``private_directory``)."""
    if os.name == "nt":
        private_directory(path.parent)
        api = _win32_api()
        folder = api.open(path.parent, directory=True)
        try:
            expected = folder.final_path()
        finally:
            folder.close()
        credential = api.open(path, directory=False, delete=True)
        try:
            attributes = credential.attributes()
            owner, _aces = credential.security()
            if attributes & 0x400:
                raise PermissionError(f"{path} is a reparse point")
            if attributes & 0x10:
                raise PermissionError(f"{path} is not a regular file")
            if owner != api.account_sid():
                raise PermissionError(f"{path} is owned by {owner}")
            # The credential's own DACL, on the handle, before it is read.
            _owner, aces = credential.security()
            if aces is None:
                raise PermissionError(f"{path} has no DACL (open to everyone)")
            for kind, _flags, _mask, trustee in aces:
                if kind not in (0, 1) or (kind == 0 and trustee not in {
                        owner, _SYSTEM, _ADMINISTRATORS, _OWNER_RIGHTS}):
                    raise PermissionError(f"{path} grants access to {trustee or f'an ACE of type {kind}'}")
            if os.path.normcase(os.path.dirname(credential.final_path())) != os.path.normcase(expected):
                raise PermissionError(f"{path} is not inside its protected directory")
            value = json.loads(credential.read().decode("utf-8"))
            credential.delete()
        finally:
            credential.close()
        return value
    import errno
    import stat
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PermissionError(f"{path} is a link") from None
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError(f"{path} is not this account's file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            value = json.loads(handle.read().decode("utf-8"))
    finally:
        os.close(descriptor)
    path.unlink(missing_ok=True)
    return value


# ── raw Windows bindings (facts only; the policy is above) ─────────────────

class _Win32Handle:
    """One file-system object opened WITHOUT following a reparse point."""

    def __init__(self, api, path: Path, handle):
        self.api, self.path, self.handle = api, path, handle

    def attributes(self) -> int:
        info = (self.api.wintypes.DWORD * 13)()            # BY_HANDLE_FILE_INFORMATION
        if not self.api.k32.GetFileInformationByHandle(self.handle, info):
            raise PermissionError(f"{self.path} cannot be inspected")
        return info[0]

    def security(self) -> tuple[str, list | None]:
        """(owner SID, [(ACE type, ACE flags, access mask, trustee SID)] or
        None for a NULL DACL)."""
        api, ctypes = self.api, self.api.ctypes
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        if api.adv.GetSecurityInfo(self.handle, 1, 0x1 | 0x4, ctypes.byref(owner), None, ctypes.byref(dacl), None,
                                   ctypes.byref(descriptor)) != 0:
            raise PermissionError(f"the security descriptor of {self.path} cannot be read")
        try:
            owner_sid = api.sid_text(owner)
            if not dacl:
                return owner_sid, None
            size = (api.wintypes.DWORD * 3)()               # ACL_SIZE_INFORMATION
            if not api.adv.GetAclInformation(dacl, size, ctypes.sizeof(size), 2):
                raise PermissionError(f"the DACL of {self.path} cannot be read")
            aces = []
            for index in range(size[0]):
                ace = ctypes.c_void_p()
                if not api.adv.GetAce(dacl, index, ctypes.byref(ace)):
                    raise PermissionError(f"an ACE of {self.path} cannot be read")
                kind, flags = ctypes.c_ubyte.from_address(ace.value).value, ctypes.c_ubyte.from_address(ace.value + 1).value
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                trustee = api.sid_text(ctypes.c_void_p(ace.value + 8)) if kind in (0, 1) else ""
                aces.append((kind, flags, mask, trustee))
            return owner_sid, aces
        finally:
            api.k32.LocalFree(descriptor)

    def final_path(self) -> str:
        buffer = self.api.ctypes.create_unicode_buffer(32768)
        length = self.api.k32.GetFinalPathNameByHandleW(self.handle, buffer, 32768, 0)
        if not 0 < length < 32768:
            raise PermissionError(f"the final path of {self.path} cannot be read")
        return buffer.value.removeprefix("\\\\?\\")

    def read(self) -> bytes:
        chunks, buffer, got = [], self.api.ctypes.create_string_buffer(65536), self.api.wintypes.DWORD()
        while True:
            if not self.api.k32.ReadFile(self.handle, buffer, 65536, self.api.ctypes.byref(got), None):
                raise PermissionError(f"{self.path} cannot be read")
            if not got.value:
                return b"".join(chunks)
            chunks.append(buffer.raw[:got.value])

    def delete(self) -> None:
        flag = self.api.ctypes.c_ubyte(1)                  # FILE_DISPOSITION_INFO.DeleteFile
        if not self.api.k32.SetFileInformationByHandle(self.handle, 4, self.api.ctypes.byref(flag), 1):
            raise PermissionError(f"{self.path} cannot be deleted")

    def close(self) -> None:
        if self.handle:
            self.api.k32.CloseHandle(self.handle)
            self.handle = None


def _win32_api():
    """The raw kernel32/advapi32/userenv/shell32 bindings the policy above uses."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    env = ctypes.WinDLL("userenv", use_last_error=True)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32")
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    k32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    adv.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    adv.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.c_void_p]
    env.GetUserProfileDirectoryW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    env.GetProfilesDirectoryW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.c_void_p]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    class Api:
        pass

    api = Api()
    api.ctypes, api.wintypes, api.k32, api.adv = ctypes, wintypes, k32, adv
    invalid = wintypes.HANDLE(-1).value

    def open(path: Path, *, directory: bool, delete: bool = False) -> _Win32Handle:
        access = 0x80000000 | 0x00020000 | 0x80 | (0x00010000 if delete else 0)  # GENERIC_READ|READ_CONTROL|READ_ATTRIBUTES|DELETE
        flags = 0x00200000 | (0x02000000 if directory else 0)    # FILE_FLAG_OPEN_REPARSE_POINT | BACKUP_SEMANTICS
        handle = k32.CreateFileW(str(path), access, 0x1 | 0x4 | (0x2 if directory else 0), None, 3, flags, None)
        if handle in (None, 0, invalid):
            error = ctypes.get_last_error()
            if error in (2, 3):                   # not there (yet): ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
                raise FileNotFoundError(error, "not found", str(path))
            raise PermissionError(f"{path} cannot be opened ({error})")
        return _Win32Handle(api, path, handle)

    def sid_text(sid) -> str:
        value = ctypes.c_void_p()
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise PermissionError("a SID cannot be read")
        try:
            return ctypes.wstring_at(value.value)
        finally:
            k32.LocalFree(value)                  # ConvertSidToStringSidW allocates with LocalAlloc

    def token():
        handle = wintypes.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x8, ctypes.byref(handle)):   # TOKEN_QUERY
            raise PermissionError("this process's token cannot be read")
        return handle

    def account_sid() -> str:
        handle, user = token(), ctypes.create_string_buffer(512)
        try:
            if not adv.GetTokenInformation(handle, 1, user, 512, ctypes.byref(wintypes.DWORD())):   # TokenUser
                raise PermissionError("this account's SID cannot be read")
        finally:
            k32.CloseHandle(handle)
        return sid_text(ctypes.c_void_p(ctypes.c_void_p.from_buffer(user).value))

    def elevation_type() -> int:
        """This process's TokenElevationType: 1 default (no split token: UAC
        off, the built-in Administrator, or a standard account), 2 full (a
        split token, elevated), 3 limited (a split token, not elevated)."""
        handle, value = token(), wintypes.DWORD()
        try:
            if not adv.GetTokenInformation(handle, 18, ctypes.byref(value), 4,     # TokenElevationType
                                           ctypes.byref(wintypes.DWORD())):
                raise PermissionError("this process's elevation cannot be read")
        finally:
            k32.CloseHandle(handle)
        return value.value

    def known_folders() -> tuple[str, str]:
        """(this account's profile directory, its Roaming AppData folder)."""
        handle, size = token(), wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(32768)
        try:
            if not env.GetUserProfileDirectoryW(handle, buffer, ctypes.byref(size)):
                raise PermissionError("this account's profile directory cannot be read")
        finally:
            k32.CloseHandle(handle)
        roaming_id = (ctypes.c_ubyte * 16).from_buffer_copy(   # FOLDERID_RoamingAppData {3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}
            bytes.fromhex("db85b63ef965f64ca03ae3ef65729f3d"))
        value = ctypes.c_void_p()
        if shell.SHGetKnownFolderPath(ctypes.byref(roaming_id), 0, None, ctypes.byref(value)) != 0:
            raise PermissionError("the Roaming AppData folder cannot be read")
        try:
            roaming = ctypes.wstring_at(value.value)
        finally:
            ole.CoTaskMemFree(value)              # SHGetKnownFolderPath allocates with CoTaskMemAlloc
        return buffer.value, roaming

    def profiles_directory() -> str:
        """The system's profiles root (``GetProfilesDirectoryW``)."""
        size, buffer = wintypes.DWORD(32768), ctypes.create_unicode_buffer(32768)
        if not env.GetProfilesDirectoryW(buffer, ctypes.byref(size)):
            raise PermissionError("the profiles directory cannot be read")
        return buffer.value

    api.open, api.sid_text, api.account_sid, api.known_folders = open, sid_text, account_sid, known_folders
    api.elevation_type = elevation_type
    api.profiles_directory = profiles_directory
    return api


def single_instance(root: Path):
    """The supervisor's exclusive lock on its data root, or ``None`` when
    another supervisor holds it. Held as long as the returned file is open."""
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / "desktop.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


WINDOW_PORT_FILE = "window-port"


def window_port(runtime: Path) -> int:
    """The window's loopback port: the one it had at the last launch while
    that is still free -- so its address, and with it the browser cache of the
    page's scripts and styles (core.static_assets), stays the same across
    launches -- else a free one, remembered for the next. A local hint only:
    a missing or unreadable record is a free port."""
    from core.ports import free_port, loopback_port
    record = runtime / WINDOW_PORT_FILE
    try:
        previous = int(record.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        previous = 0
    if not 1024 <= previous <= 65535:
        previous = 0
    port = loopback_port(previous, name="the DownloadPulse window") if previous else free_port()
    if port != previous:
        try:
            runtime.mkdir(parents=True, exist_ok=True)
            temporary = record.with_suffix(".tmp")
            temporary.write_text(f"{port}\n", encoding="ascii")
            os.replace(temporary, record)
        except OSError as exc:
            logger.warning("the window's port could not be remembered: %s", exc)
    return port


# ── the private channel (supervisor end) ───────────────────────────────────

class Channel:
    """The supervisor's ends of the backend's control pipes: one request at a
    time, answered in order (``application.desktop_control``)."""

    def __init__(self):
        self.request_read, self.request_write = os.pipe()     # supervisor writes, backend reads
        self.answer_read, self.answer_write = os.pipe()       # backend writes, supervisor reads
        self._lock = asyncio.Lock()
        self._next = 0
        self._out = self._in = None

    def child_ends(self) -> tuple[int, int]:
        """The backend's two ends, made inheritable (POSIX descriptors, or the
        Windows handles behind them) for that one child."""
        for fd in (self.request_read, self.answer_write):
            os.set_inheritable(fd, True)
        if os.name == "nt":
            import msvcrt
            return msvcrt.get_osfhandle(self.request_read), msvcrt.get_osfhandle(self.answer_write)
        return self.request_read, self.answer_write

    def started(self) -> None:
        """The backend holds its ends: the supervisor's copies are closed, so
        the pipes end exactly when one of the two processes lets go."""
        os.close(self.request_read)
        os.close(self.answer_write)
        self._out = os.fdopen(self.request_write, "wb", buffering=0)
        self._in = os.fdopen(self.answer_read, "rb", buffering=0)

    async def ask(self, op: str, **fields) -> dict:
        async with self._lock:
            self._next += 1
            line = json.dumps({"id": self._next, "op": op, **fields}, separators=(",", ":")).encode() + b"\n"
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._out.write, line)
            answer = await loop.run_in_executor(None, self._in.readline)
            if not answer:
                raise ConnectionError("the backend's control channel ended")
            return json.loads(answer)

    def close(self) -> None:
        """Quit: the backend reads the end of its input and stops itself."""
        for end in (self._out, self._in):
            try:
                if end is not None:
                    end.close()
            except OSError:
                pass


# ── words ──────────────────────────────────────────────────────────────────

class Words:
    """The tray's and notices' words, from DownloadPulse's own catalogs: at
    start the installation's default language (English when none is chosen),
    then the language the interface is shown in (op ``locale``), followed
    while the application runs (``choose``)."""

    def __init__(self, config_path: Path):
        from core import localization
        self._message = localization.message
        # Admitted by the manifest alone: the tray never reads every catalog
        # (message() falls back to English for one it cannot read).
        self._tags = localization.locale_tags(catalogs=False)
        try:
            tag = json.loads(config_path.read_text(encoding="utf-8")).get("default_locale")
        except (OSError, ValueError):
            tag = None
        self.tag = tag if tag in self._tags else localization.DEFAULT_LOCALE

    def choose(self, tag) -> bool:
        """Show the words in ``tag`` from now on; True when that is a change
        (an unknown tag changes nothing)."""
        if tag not in self._tags or tag == self.tag:
            return False
        self.tag = tag
        return True

    def __call__(self, key: str, **params) -> str:
        return self._message(key, self.tag, **params)


# ── the native shell ───────────────────────────────────────────────────────

class Shell:
    """Neutralino, driven over its own loopback WebSocket with the credential
    it exported for this supervisor (never given to the page)."""

    def __init__(self, session, events: asyncio.Queue | None = None, generation: int = 1):
        self.session, self.ws, self.token, self._next = session, None, "", 0
        # The window's one event queue (``Window``), shared by every shell it
        # launches; each shell's own end names its generation.
        self.events: asyncio.Queue = events if events is not None else asyncio.Queue()
        self.generation = generation
        self._pending: dict[str, asyncio.Future] = {}

    async def connect(self, auth_file: Path, deadline: float, owned) -> None:
        """Connect to the shell ``owned`` (its owned process) once it has
        exported its credential; a shell that ends first fails the start at
        once, never after the whole wait."""
        while True:
            try:
                info = read_credential(auth_file)   # read once, then deleted; it is nobody else's
                break
            except PermissionError:
                raise
            except (OSError, ValueError):
                if owned.process.returncode is not None:
                    raise RuntimeError(f"the native shell ended during start ({owned.process.returncode})") from None
                if time.monotonic() > deadline:
                    raise TimeoutError("the native shell exported no credential") from None
                await asyncio.sleep(0.05)   # soon after export: its key is taken next (_take_globals)
        self.token = str(info["nlToken"])
        await self._take_globals(int(info["nlPort"]), deadline)
        self.ws = await self.session.ws_connect(
            f"ws://127.0.0.1:{int(info['nlPort'])}?connectToken={info['nlConnectToken']}")
        asyncio.ensure_future(self._receive())

    async def _take_globals(self, port: int, deadline: float) -> None:
        """Neutralino gives its access key (``NL_TOKEN``) once, to whoever
        first asks for ``/__neutralino_globals.js`` (tokenSecurity
        ``one-time``). The window never asks (no globals are injected into
        its page), so the supervisor asks first and discards the answer: no
        other program, and no web page, can then have the key. The answer
        must carry the key the shell exported (none: someone else took it)
        and say the shell serves its resource bundle (``NL_RESMODE``), never
        its folder. Anything else fails the start (the shell is ended)."""
        try:
            async with self.session.get(f"http://127.0.0.1:{port}/__neutralino_globals.js",
                                        timeout=max(1.0, deadline - time.monotonic())) as response:
                body = await response.text() if response.status == 200 else ""
        except Exception as exc:  # noqa: BLE001 -- no answer is the same answer: not taken
            body = ""
            logger.error("the native shell's access key could not be taken: %s", exc or type(exc).__name__)
        token = re.search(r"var NL_TOKEN='([^']*)';", body)
        mode = re.search(r"var NL_RESMODE='([^']*)';", body)
        if token is None or token.group(1) != self.token:
            logger.error("the native shell's access key was %s; the shell is ended",
                         "taken by another program first" if token is not None else "not taken")
            raise PermissionError("the native shell's access key was not taken by DownloadPulse")
        if mode is None or mode.group(1) != "bundle":
            logger.error("the native shell serves %s, not its resource bundle; the shell is ended",
                         f"its {mode.group(1)}" if mode is not None else "an unknown resource mode")
            raise PermissionError("the native shell does not serve its resource bundle")

    async def _receive(self) -> None:
        async for message in self.ws:
            try:
                value = json.loads(message.data)
            except (TypeError, ValueError):
                continue
            future = self._pending.pop(str(value.get("id")), None) if value.get("id") else None
            if future is not None and not future.done():
                future.set_result(value.get("data"))
            elif value.get("event"):
                await self.events.put((value["event"], value.get("data")))
        await self.events.put(("shellClosed", {"generation": self.generation}))

    async def call(self, method: str, data: dict | list | None = None, timeout: float = 15):
        """One native call's answer; ``NativeError`` when Neutralino says it
        failed (its answer's ``error``), never taken for done."""
        if method not in NATIVE_METHODS:
            raise PermissionError(method)
        self._next += 1
        ident = f"dp-{self._next}"
        self._pending[ident] = asyncio.get_running_loop().create_future()
        await self.ws.send_str(json.dumps({"id": ident, "method": method, "accessToken": self.token,
                                           "data": data or {}}))
        answer = await asyncio.wait_for(self._pending[ident], timeout)
        if isinstance(answer, dict) and answer.get("error"):
            raise NativeError(method, answer["error"])
        return answer

    # Where the tray and notices are shown, everywhere but Linux (SessionBus).
    tray_shown = False
    # The shell's own executable (run()), whose windows get the frame on Windows.
    binary = ""

    async def tray(self, menu: dict) -> None:
        try:
            await self.call("os.setTray", menu)
            self.tray_shown = True
        except NativeError as exc:
            if self.tray_shown or not getattr(self, "_tray_reported", False):
                logger.error("the tray could not be shown: %s", exc)
            self._tray_reported, self.tray_shown = True, False

    async def notice(self, title: str, body: str) -> None:
        # Windows: a toast under DownloadPulse's own application ID (its name and icon),
        # not Neutralino's tray balloon (the shell's file name and a generic info icon).
        if sys.platform == "win32" and await windows_notice(title, body):
            return
        try:
            await self.call("os.showNotification", {"title": title, "content": body or title})
        except NativeError as exc:
            logger.error("a notice was not shown: %s", exc)

    async def save_destination(self, name: str, title: str) -> str | None:
        """Where the person saves ``name``: the platform's own Save dialog
        (Neutralino's, everywhere but Linux), starting in their Downloads
        folder; None when they cancel."""
        downloads = Path.home() / "Downloads"
        start = downloads / name if downloads.is_dir() else Path(name)
        try:
            chosen = await self.call("os.showSaveDialog", {"title": title, "defaultPath": str(start)},
                                     timeout=DIALOG_SECONDS)
        except (NativeError, TimeoutError) as exc:
            logger.error("the Save dialog failed: %s", exc)
            return None
        # Neutralino answers {"returnValue": <path, "" when cancelled>, "success": true}
        # (api/os/os.cpp showSaveDialog), never the bare path.
        if isinstance(chosen, dict):
            chosen = chosen.get("returnValue")
        return str(chosen) if isinstance(chosen, str) and chosen else None


    async def exit(self) -> None:
        """Neutralino's own exit (macOS, ``_end_shell``); it answers nothing,
        so only the process ending says it happened."""
        if self.ws is not None and not self.ws.closed:
            self._next += 1
            await self.ws.send_str(json.dumps({"id": f"dp-{self._next}", "method": "app.exit",
                                               "accessToken": self.token, "data": {}}))


class NativeError(RuntimeError):
    def __init__(self, method: str, error):
        detail = error.get("message") or error.get("code") if isinstance(error, dict) else error
        super().__init__(f"{method}: {detail}")


# ── Linux: the tray, notices and activation on the session bus ─────────────

class SessionBus:
    """Linux: the tray, the notices and opening the running instance, through
    the desktop's own standard interfaces on the session bus. Neutralino's
    tray needs an AppIndicator library no Flatpak runtime provides (and fails
    without it), and its notices go to a helper that can report success
    without showing anything; these do not depend on either.

    * The tray is a StatusNotifierItem (``/StatusNotifierItem``) with its
      ``com.canonical.dbusmenu`` menu (``/MenuBar``), registered with
      ``org.kde.StatusNotifierWatcher`` -- again whenever a watcher appears --
      showing the favicon-derived pixmaps; its menu is the supervisor's own
      (``tray_menu``). It is shown (``tray_shown``) only while the watcher
      accepted it and says a host displays items.
    * A notice is ``org.freedesktop.portal.Notification.AddNotification``.
    * The running instance owns its application ID on the bus and answers
      ``org.freedesktop.Application.Activate`` (``activate_running``): starting
      DownloadPulse again opens its window.

    A menu click, the item's own activation and Activate become the shell's
    events (``trayMenuItemClicked``), handled where every click is. Nothing
    here talks to the backend.

    **The native control boundary.** Anyone on the session bus may read the
    item and its menu and may open the window (the menu's Open, the item's
    activation, Activate: the window, password-locked or not, is no action).
    The actions -- Pause All, Resume All, Quit -- are taken only from the tray
    host: a caller that owns an ``org.kde.StatusNotifierHost-*`` name (the
    name every StatusNotifier host registers under: Plasma, waybar, ...) or
    the watcher itself (a host that is its own watcher, as GNOME's
    AppIndicator extension is). Anyone else is refused with AccessDenied
    before anything reaches the backend's channel -- a peer inside this
    sandbox above all (a confined Media Downloads helper can reach the bus,
    but xdg-dbus-proxy lets nothing in the sandbox own such a name). The
    Flatpak may *see* (never talk to) the ``org.kde.*`` names for this check.
    An unconfined process of the same account could still register a host
    name of its own; it could equally read this account's DownloadPulse files,
    so that is not a boundary the bus could add.

    **Availability.** The tray is shown (``tray_shown``) only while a watcher
    holds the item and says a host is registered; the watcher leaving, or its
    last host, takes it away at once, and it is registered again as soon as a
    watcher or host returns -- the bus's own signals, never a poll: with no
    watcher there is nothing to register with, and nothing is tried. Each
    change is the shell's event (``trayAvailability``), so the window is never
    left hidden where nothing can open it, and the log says it once."""

    # The menu entries that act, and so are the tray host's alone.
    ACTIONS = frozenset({"pause", "resume", "quit"})
    HOSTS = "org.kde.StatusNotifierHost-"

    ITEM, MENU = "/StatusNotifierItem", "/MenuBar"
    APPLICATION = "/" + APP_ID.replace(".", "/")
    WATCHER = ("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher")
    PORTAL = ("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
              "org.freedesktop.portal.Notification")
    CHOOSER = "org.freedesktop.portal.FileChooser"
    SIZES = (22, 32, 48, 64)

    def __init__(self, router, events: asyncio.Queue, pixmaps: list):
        self.router, self.events, self.pixmaps = router, events, pixmaps
        self.items: list[dict] = []
        self.revision, self.notices_sent, self.tray_shown = 0, 0, False
        self.availability_logged = False
        self.status_line = ""
        self.tasks: list[asyncio.Task] = []
        self.filters: list = []

    # -- starting and stopping --

    @classmethod
    async def open(cls, events: asyncio.Queue, icons: Path) -> SessionBus | None:
        from jeepney.io.asyncio import open_dbus_router
        try:
            context = open_dbus_router("SESSION")
            router = await context.__aenter__()
        except (OSError, KeyError, ValueError) as exc:   # no session bus at all
            logger.error("the session bus is unavailable; no tray or notices: %s", exc)
            return None
        bus = cls(router, events, cls._pixmaps(icons))
        bus.context = context
        await bus._start()
        return bus

    @classmethod
    def _pixmaps(cls, icons: Path) -> list:
        """The tray icon at each size, as StatusNotifierItem pixmaps: ARGB32 in
        network byte order (the files are the favicon's RGBA, assemble.py)."""
        pixmaps = []
        for size in cls.SIZES:
            try:
                rgba = (icons / f"tray-{size}.rgba").read_bytes()
            except OSError:
                continue
            if len(rgba) != size * size * 4:
                continue
            argb = bytearray(len(rgba))
            argb[0::4], argb[1::4], argb[2::4], argb[3::4] = rgba[3::4], rgba[0::4], rgba[1::4], rgba[2::4]
            pixmaps.append((size, size, bytes(argb)))
        if not pixmaps:
            logger.error("no tray icon pixmaps in %s", icons)
        return pixmaps

    async def _start(self) -> None:
        from jeepney import MatchRule, message_bus
        calls = self.router.filter(MatchRule(type="method_call"), bufsize=64)
        owners = self.router.filter(MatchRule(type="signal", sender="org.freedesktop.DBus",
                                              interface="org.freedesktop.DBus", member="NameOwnerChanged",
                                              path="/org/freedesktop/DBus"), bufsize=16)
        hosts = self.router.filter(MatchRule(type="signal", interface=self.WATCHER[2]), bufsize=16)
        self.filters = [calls, owners, hosts]
        self.tasks = [asyncio.ensure_future(self._answer(calls.queue)),
                      asyncio.ensure_future(self._watch(owners.queue)),
                      asyncio.ensure_future(self._hosts(hosts.queue))]
        rule = MatchRule(type="signal", sender="org.freedesktop.DBus", interface="org.freedesktop.DBus",
                         member="NameOwnerChanged", path="/org/freedesktop/DBus")
        rule.add_arg_condition(0, self.WATCHER[0])
        await self._ask(message_bus.AddMatch(rule), "watching for a tray watcher")
        await self._ask(message_bus.AddMatch(MatchRule(type="signal", sender=self.WATCHER[0],
                                                       interface=self.WATCHER[2])), "watching for tray hosts")
        owned = await self._ask(message_bus.RequestName(APP_ID, 0x4), "owning the application ID")   # DO_NOT_QUEUE
        if owned is not None and owned.body[0] not in (1, 4):
            logger.error("the application ID %s is owned by another process; DownloadPulse cannot be "
                         "opened by starting it again", APP_ID)
        await self._register()
        await self._check_portal()

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        for handle in self.filters:
            handle.close()
        try:
            await self.context.__aexit__(None, None, None)
        except (OSError, RuntimeError):
            pass

    @staticmethod
    async def activate_running() -> bool:
        """Ask the instance running for this account to open its window."""
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.asyncio import open_dbus_router
        try:
            async with open_dbus_router("SESSION") as router:
                reply = await asyncio.wait_for(router.send_and_get_reply(new_method_call(
                    DBusAddress(SessionBus.APPLICATION, bus_name=APP_ID, interface="org.freedesktop.Application"),
                    "Activate", "a{sv}", ({},))), 10)
        except (OSError, KeyError, ValueError, TimeoutError) as exc:
            logger.error("the running DownloadPulse could not be asked to open: %s", exc)
            return False
        if reply.header.message_type.name == "error":
            logger.error("the running DownloadPulse did not open: %s", reply.body)
            return False
        return True

    # -- what the supervisor shows --

    async def tray(self, menu: dict) -> None:
        self.items = [item for item in menu["menuItems"]]
        self.status_line = next((item["text"] for item in self.items if item.get("id") == "status"), "")
        self.revision += 1
        await self._signal(self.MENU, "com.canonical.dbusmenu", "LayoutUpdated", "ui", (self.revision, 0))
        await self._signal(self.ITEM, "org.kde.StatusNotifierItem", "NewToolTip", "", ())

    async def notice(self, title: str, body: str) -> None:
        """One new notice. Its id is never one this application used before,
        in this run or an earlier one: the portal replaces a notice whose id
        it already holds for the application, and the desktop shows such a
        replacement quietly (Plasma: no popup) -- a per-run counter restarting
        at 1 turned every later run's notices into silent replacements."""
        self.notices_sent += 1
        reply = await self._ask(self._notification(title, body), "showing a notice")
        if reply is None:
            logger.error("a notice was not shown (%s)", title)

    @classmethod
    def _notification(cls, title: str, body: str):
        """One new notice's ``AddNotification`` call (``notice``)."""
        from jeepney import DBusAddress, new_method_call
        name, path, interface = cls.PORTAL
        return new_method_call(
            DBusAddress(path, bus_name=name, interface=interface), "AddNotification", "sa{sv}",
            (notice_id(), {"title": ("s", title), "body": ("s", body or title), "priority": ("s", "normal")}))

    @classmethod
    async def notice_once(cls, title: str, body: str) -> bool:
        """One notice on a connection of its own, as ``activate_running`` asks:
        for a run whose start failed, which may have no SessionBus open."""
        from jeepney.io.asyncio import open_dbus_router
        try:
            async with open_dbus_router("SESSION") as router:
                reply = await asyncio.wait_for(router.send_and_get_reply(cls._notification(title, body)), 10)
        except (OSError, KeyError, ValueError, TimeoutError) as exc:
            logger.error("the notice was not shown (%s): %s", title, exc)
            return False
        if reply.header.message_type.name == "error":
            logger.error("the notice was not shown (%s): %s", title, reply.body)
            return False
        return True

    async def save_destination(self, name: str, title: str) -> str | None:
        """Where the person saves ``name``: the desktop's own Save dialog
        through the file chooser portal (inside the Flatpak it also grants
        this application the place chosen; Neutralino's own dialog needs a
        zenity or kdialog the sandbox has not got); None when they cancel."""
        import secrets
        from urllib.parse import unquote, urlparse
        from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
        token = "dp" + secrets.token_hex(8)
        sender = (self.router.unique_name or "").lstrip(":").replace(".", "_")
        request = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        # The answer arrives as the request's Response signal: listened for
        # before asking, so it cannot come first.
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response", path=request)
        with self.router.filter(rule, bufsize=1) as answers:
            await self._ask(message_bus.AddMatch(rule), "listening for the Save dialog's answer")
            name_, path, _interface = self.PORTAL
            asked = await self._ask(new_method_call(
                DBusAddress(path, bus_name=name_, interface=self.CHOOSER), "SaveFile", "ssa{sv}",
                ("", title, {"handle_token": ("s", token), "current_name": ("s", name), "modal": ("b", True)})),
                "showing the Save dialog")
            if asked is None:
                return None
            try:
                answer = await asyncio.wait_for(answers.get(), DIALOG_SECONDS)
            except TimeoutError:
                logger.error("the Save dialog gave no answer in %ss", DIALOG_SECONDS)
                return None
            finally:
                await self._ask(message_bus.RemoveMatch(rule), "no longer listening for the Save dialog")
        response, results = answer.body
        uris = results.get("uris", ("as", []))[1] if response == 0 else []
        if not uris:
            return None                             # cancelled (or nothing chosen)
        location = urlparse(uris[0])
        return unquote(location.path) if location.scheme == "file" and location.path else None

    # -- the bus side --

    async def _ask(self, message, doing: str):
        """A call's reply, or None (logged) when it failed or was refused."""
        try:
            reply = await asyncio.wait_for(self.router.send_and_get_reply(message), 10)
        except (OSError, TimeoutError, RuntimeError) as exc:
            logger.error("session bus: %s failed: %s", doing, exc)
            return None
        if reply.header.message_type.name == "error":
            from jeepney.low_level import HeaderFields
            logger.error("session bus: %s was refused: %s %s", doing,
                         reply.header.fields.get(HeaderFields.error_name), reply.body)
            return None
        return reply

    async def _register(self) -> None:
        from jeepney import DBusAddress, Properties, new_method_call
        name, path, interface = self.WATCHER
        if await self._owner(name) is None:
            self._shown(False)                      # no watcher: nothing to register with (stock GNOME)
            return
        watcher = DBusAddress(path, bus_name=name, interface=interface)
        registered = await self._ask(new_method_call(watcher, "RegisterStatusNotifierItem", "s", (APP_ID,)),
                                     "registering the tray icon")
        hosted = await self._ask(Properties(watcher).get("IsStatusNotifierHostRegistered"),
                                 "asking whether a tray host is shown") if registered else None
        self._shown(bool(registered and hosted and hosted.body[0][1]))

    def _shown(self, shown: bool) -> None:
        """The tray's availability: logged once per change (and once at
        first), and the shell's event when it changes."""
        changed = shown != self.tray_shown
        if changed or not self.availability_logged:
            self.availability_logged = True
            logger.info("tray icon %s", "shown" if shown else "not shown: no tray host to open the window from")
        if changed:
            self.tray_shown = shown
            self.events.put_nowait(("trayAvailability", {"shown": shown}))

    async def _owner(self, name: str) -> str | None:
        from jeepney import message_bus
        try:
            reply = await asyncio.wait_for(self.router.send_and_get_reply(message_bus.GetNameOwner(name)), 10)
        except (OSError, TimeoutError, RuntimeError):
            return None
        return reply.body[0] if reply.header.message_type.name != "error" else None

    async def _host(self, caller: str | None) -> bool:
        """Whether ``caller`` is the tray host (see the class's boundary)."""
        if not caller:
            return False
        from jeepney import message_bus
        listed = await self._ask(message_bus.ListNames(), "listing the tray hosts")
        names = [self.WATCHER[0], *(name for name in (listed.body[0] if listed else ())
                                    if name.startswith(self.HOSTS))]
        for name in names:
            if await self._owner(name) == caller:
                return True
        return False

    async def _check_portal(self) -> None:
        from jeepney import DBusAddress, Properties
        name, path, interface = self.PORTAL
        version = await self._ask(Properties(DBusAddress(path, bus_name=name, interface=interface)).get("version"),
                                  "finding the notification portal")
        if version is not None:
            logger.info("notification portal version %s", version.body[0][1])

    async def _signal(self, path: str, interface: str, member: str, signature: str, body: tuple) -> None:
        from jeepney import DBusAddress, new_signal
        try:
            await self.router.send(new_signal(DBusAddress(path, interface=interface), member,
                                              signature or None, body))
        except OSError as exc:
            logger.error("session bus: %s.%s not sent: %s", interface, member, exc)

    async def _watch(self, queue: asyncio.Queue) -> None:
        while True:
            message = await queue.get()
            watched, _old, new = message.body
            if watched != self.WATCHER[0]:
                continue
            if new:
                logger.info("a tray watcher appeared")
                await self._register()
            else:
                logger.warning("the tray watcher went away")
                self._shown(False)

    async def _hosts(self, queue: asyncio.Queue) -> None:
        """A host registered or gone (the watcher's own signals): asked anew."""
        from jeepney.low_level import HeaderFields
        while True:
            message = await queue.get()
            if message.header.fields.get(HeaderFields.member) in {"StatusNotifierHostRegistered",
                                                                   "StatusNotifierHostUnregistered"}:
                await self._register()

    async def _answer(self, queue: asyncio.Queue) -> None:
        from jeepney import new_error, new_method_return
        from jeepney.low_level import HeaderFields
        while True:
            message = await queue.get()
            fields = message.header.fields
            path, interface, member = (fields.get(HeaderFields.path), fields.get(HeaderFields.interface),
                                       fields.get(HeaderFields.member))
            caller = fields.get(HeaderFields.sender)
            try:
                answer = await self._method(caller, path, interface, member, message.body)
            except PermissionError as refusal:
                logger.warning("refused %s from %s: not the tray host", refusal, caller)
                reply = new_error(message, "org.freedesktop.DBus.Error.AccessDenied", "s",
                                  (f"{refusal} is the tray host's",))
            except LookupError:
                reply = new_error(message, "org.freedesktop.DBus.Error.UnknownMethod", "s",
                                  (f"{interface}.{member} on {path}",))
            else:
                reply = new_method_return(message, *answer) if answer else new_method_return(message)
            try:
                await self.router.send(reply)
            except OSError as exc:
                logger.error("session bus: no reply sent to %s.%s: %s", interface, member, exc)

    async def _method(self, caller, path, interface, member, body):
        """(signature, body) of the reply, () for none; LookupError: not ours;
        PermissionError: an action from someone other than the tray host."""
        if interface == "org.freedesktop.DBus.Introspectable" and member == "Introspect":
            return "s", (self._introspection(path),)
        if interface == "org.freedesktop.DBus.Peer" and member == "Ping":
            return ()
        if interface == "org.freedesktop.DBus.Properties" and member in {"Get", "GetAll"}:
            properties = self._properties(path, body[0])
            if member == "GetAll":
                return "a{sv}", (properties,)
            if body[1] not in properties:
                raise LookupError(body[1])
            return "v", (properties[body[1]],)
        if path == self.ITEM and interface == "org.kde.StatusNotifierItem":
            if member in {"Activate", "SecondaryActivate"}:
                await self.events.put(("trayMenuItemClicked", {"id": "open"}))
            elif member not in {"ContextMenu", "Scroll", "ProvideXdgActivationToken"}:
                raise LookupError(member)
            return ()
        if path == self.MENU and interface == "com.canonical.dbusmenu":
            return await self._menu(caller, member, body)
        if path == self.APPLICATION and interface == "org.freedesktop.Application" \
                and member in {"Activate", "Open", "ActivateAction"}:
            await self.events.put(("trayMenuItemClicked", {"id": "open"}))
            return ()
        raise LookupError(member)

    async def _menu(self, caller, member, body):
        entries = {index: item for index, item in enumerate(self.items, 1)}
        if member == "GetLayout":
            children = [("(ia{sv}av)", (index, self._entry(item), [])) for index, item in entries.items()]
            return "u(ia{sv}av)", (self.revision, (0, {"children-display": ("s", "submenu")}, children))
        if member == "GetGroupProperties":
            wanted = body[0] or list(entries)
            return "a(ia{sv})", ([(index, self._entry(entries[index])) for index in wanted if index in entries],)
        if member == "GetProperty":
            entry = self._entry(entries[body[0]]) if body[0] in entries else {}
            if body[1] not in entry:
                raise LookupError(body[1])
            return "v", (entry[body[1]],)
        if member in {"Event", "EventGroup"}:
            unknown, chosen = [], []
            for index, event, _data, _time in ([body] if member == "Event" else body[0]):
                item = entries.get(index)
                if item is None:
                    unknown.append(index)
                elif event == "clicked" and item.get("id") and not item.get("isDisabled"):
                    chosen.append(item["id"])
            # The boundary: an action is the tray host's, checked before any of
            # the group is taken.
            refused = [choice for choice in chosen if choice in self.ACTIONS]
            if refused and not await self._host(caller):
                raise PermissionError(", ".join(refused))
            for choice in chosen:
                await self.events.put(("trayMenuItemClicked", {"id": choice}))
            return ("ai", (unknown,)) if member == "EventGroup" else ()
        if member == "AboutToShow":
            return "b", (False,)
        if member == "AboutToShowGroup":
            return "aiai", ([], [])
        raise LookupError(member)

    @staticmethod
    def _entry(item: dict) -> dict:
        if item.get("text") == "-":
            return {"type": ("s", "separator")}
        return {"label": ("s", item.get("text", "")), "enabled": ("b", not item.get("isDisabled"))}

    def _properties(self, path: str, interface: str) -> dict:
        if path == self.ITEM and interface in {"org.kde.StatusNotifierItem", ""}:
            return {"Category": ("s", "ApplicationStatus"), "Id": ("s", APP_ID), "Title": ("s", "DownloadPulse"),
                    "Status": ("s", "Active"), "WindowId": ("i", 0), "IconName": ("s", ""),
                    "IconPixmap": ("a(iiay)", self.pixmaps), "IconThemePath": ("s", ""),
                    "OverlayIconName": ("s", ""), "OverlayIconPixmap": ("a(iiay)", []),
                    "AttentionIconName": ("s", ""), "AttentionIconPixmap": ("a(iiay)", []),
                    "AttentionMovieName": ("s", ""),
                    "ToolTip": ("(sa(iiay)ss)", ("", [], "DownloadPulse", self.status_line)),
                    "ItemIsMenu": ("b", False), "Menu": ("o", self.MENU)}
        if path == self.MENU and interface in {"com.canonical.dbusmenu", ""}:
            return {"Version": ("u", 3), "TextDirection": ("s", "ltr"), "Status": ("s", "normal"),
                    "IconThemePath": ("as", [])}
        raise LookupError(interface)

    def _introspection(self, path: str) -> str:
        interfaces = {self.ITEM: ["org.kde.StatusNotifierItem"], self.MENU: ["com.canonical.dbusmenu"],
                      self.APPLICATION: ["org.freedesktop.Application"]}.get(path, [])
        children = sorted({part.split("/")[1] for part in (self.ITEM, self.MENU, self.APPLICATION)}) \
            if path == "/" else []
        standard = ["org.freedesktop.DBus.Introspectable", "org.freedesktop.DBus.Peer",
                    "org.freedesktop.DBus.Properties"] if interfaces else ["org.freedesktop.DBus.Introspectable"]
        return ("<node>" + "".join(f'<interface name="{name}"/>' for name in standard + interfaces)
                + "".join(f'<node name="{child}"/>' for child in children) + "</node>")


# ── the run ────────────────────────────────────────────────────────────────

LIFECYCLE_NOTICES = {"transfer.accepted": "desktop.notice.accepted", "transfer.completed": "desktop.notice.completed",
                     "transfer.failed": "error.category.transfer_failed",
                     "transfer.cancelled": "file_selection.transfer_cancelled",
                     "transfer.paused": "transfer.action.paused_toast"}


def shell_binary_path(shell_dir: Path, platform: str = sys.platform) -> Path:
    """The native shell's executable. In the macOS application bundle it is in
    Contents/MacOS (packaging/desktop/macos/build-app.sh), so that macOS takes it
    for DownloadPulse.app's own code and its notices carry the application's name
    and icon; everywhere else, and outside a bundle, it is beside its resources."""
    if platform == "win32":
        return shell_dir / "downloadpulse-shell.exe"
    if platform == "darwin" and len(shell_dir.parents) > 3:
        # <bundle>/Contents/Resources/app/desktop/shell -> <bundle>/Contents/MacOS
        bundled = shell_dir.parents[3] / "MacOS" / "downloadpulse-shell"
        if bundled.is_file():
            return bundled
    return shell_dir / "downloadpulse-shell"


# The shell's resources: one Neutralino bundle (an asar archive, built by
# packaging/desktop/assemble.py ``shell_bundle``) holding its configuration and
# the icons it loads, and nothing else. Neutralino reads a bundle's files only
# by their names in its header, so its little HTTP server can serve nothing
# outside it; started on its folder instead (Neutralino's directory mode), it
# would serve any file the account can read.
SHELL_BUNDLE = "resources.neu"
SHELL_BUNDLE_FILES = ("neutralino.config.json", "resources/icons/app.png", "resources/icons/tray.png",
                      "resources/icons/trayTemplate.png")


def shell_bundle(shell_dir: Path) -> Path:
    """The shell's bundle, once proven whole: a regular file whose asar
    header names every file of SHELL_BUNDLE_FILES within the file. Neutralino
    falls back to directory mode, silently, when it cannot read its bundle;
    so a missing or unreadable one refuses the shell (``RuntimeError``)."""
    import stat
    import struct
    path = shell_dir / SHELL_BUNDLE
    try:
        with open(path, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("not a regular file")
            size = os.fstat(handle.fileno()).st_size
            four, pickle, _payload, length = struct.unpack("<4I", handle.read(16))
            if four != 4 or not 0 < length <= pickle - 8 <= size - 16:
                raise ValueError("not an asar header")
            tree = json.loads(handle.read(pickle - 8)[:length].decode("utf-8"))
        for name in SHELL_BUNDLE_FILES:
            node = tree
            for part in name.split("/"):
                node = node["files"][part]
            if 16 + (pickle - 8) + int(node["offset"]) + int(node["size"]) > size:
                raise ValueError(f"{name} lies outside the file")
    except (OSError, ValueError, KeyError, TypeError, struct.error) as exc:
        raise RuntimeError(f"the native shell's resource bundle {path} is missing or unreadable "
                           f"({type(exc).__name__}: {exc}); the shell is not started") from None
    return path


def shell_argv(binary: Path, shell_dir: Path, port: int) -> list[str]:
    """The native shell's command line, the same on every system: its
    resources from its bundle (``shell_bundle``, checked first), never its
    folder (``--load-dir-res``); its page, the backend's own interface."""
    shell_bundle(shell_dir)
    return [str(binary), f"--path={shell_dir}", "--res-mode=bundle", f"--url=http://127.0.0.1:{port}/"]


# What the window's view shows before its page paints, in the interface's mode
# (op ``appearance``): Linux through linux-background.patch, Windows through
# WebView2's documented default-background override (AARRGGBB). An unknown
# mode, or none, leaves each platform's own.
SHELL_BACKGROUNDS = {"dark": ("#000000", "FF000000"), "light": ("#ffffff", "FFFFFFFF")}


def shell_environment(root: Path, environ=None, platform: str = sys.platform, *,
                      mode: str | None = None) -> dict[str, str]:
    """The native shell's environment: this process's own, plus
      - Windows: its WebView2 profile inside DownloadPulse's data root
        (Microsoft's documented override, not Neutralino's default
        %APPDATA%\\<executable name>);
      - Linux: WebKitGTK's TextureMapper compositor rather than Skia, which
        WebKitGTK 2.54 made the default. Composited with Skia, the window
        drew stale and blank frames as a page change ended (measured in the
        GNOME 49 runtime's WebKitGTK 2.54.1 on KDE Wayland; compositing stays
        on). Where the person running DownloadPulse has set it, their value
        stands;
      - Linux and Windows: the view's background before its page paints, in
        the interface's ``mode`` (``SHELL_BACKGROUNDS``), never a white frame
        on a dark interface."""
    environ = os.environ if environ is None else environ
    env = {**environ, "PATH": environ.get("PATH", "")}
    if platform == "win32":
        env["WEBVIEW2_USER_DATA_FOLDER"] = str(root / "webview2")
    elif platform.startswith("linux"):
        env.setdefault("WEBKIT_USE_SKIA_FOR_COMPOSITION", "0")
    if mode in SHELL_BACKGROUNDS:
        linux, windows = SHELL_BACKGROUNDS[mode]
        if platform == "win32":
            env["WEBVIEW2_DEFAULT_BACKGROUND_COLOR"] = windows
        elif platform.startswith("linux"):
            env["DOWNLOADPULSE_BACKGROUND"] = linux
    return env


def notice_id() -> str:
    """A notice's portal id, unique across runs (SessionBus.notice): a random
    128-bit value, so no notice ever replaces an earlier one."""
    import uuid
    return f"notice-{uuid.uuid4().hex}"


def notices(events: list[dict], words: Words) -> list[tuple[str, str]]:
    """The notices for one batch of journal events: one per eligible
    transition, a bulk Pause or Resume coalesced into one. Resumed is what the
    journal itself says resumed (a transition out of Paused, ``outcome``
    ``resumed``), never inferred here."""
    shown, paused, resumed = [], [], []
    for event in events:
        kind, name = event.get("type"), str(event.get("name") or "")
        if kind == "transfer.paused":
            paused.append(name)
        elif event.get("outcome") == "resumed":
            resumed.append(name)
        elif kind in LIFECYCLE_NOTICES:
            shown.append((words(LIFECYCLE_NOTICES[kind]), name))
    for names, single, many in ((paused, "transfer.action.paused_toast", "desktop.notice.paused_many"),
                                (resumed, "transfer.action.resumed_toast", "desktop.notice.resumed_many")):
        if len(names) == 1:
            shown.append((words(single), names[0]))
        elif names:
            shown.append((words(many, count=len(names)), ""))
    return shown


def local_port_notice(status: dict | None, words: Words) -> tuple[str, str] | None:
    """The one notice of a run whose Local Port is not listening (another
    program holds it): external clients cannot reach DownloadPulse until it is
    free or changed in Settings. None while it listens."""
    local = (status or {}).get("local_port") or {}
    if local.get("listening") is not False:
        return None
    return words("desktop.local_port.unavailable_title"), words("desktop.local_port.unavailable", port=str(int(local.get("port") or 0)))


def tray_menu(status: dict | None, words: Words, platform: str = sys.platform) -> dict:
    """The tray: its icon (macOS: a template image the menu bar tints for its
    appearance), the status line, Open, the one of Pause All / Resume All
    that applies, Quit."""
    line = words("desktop.tray.status", active=int((status or {}).get("active", 0)),
                 queued=int((status or {}).get("queued", 0)))
    paused = bool((status or {}).get("paused"))
    icon = {"icon": "/resources/icons/trayTemplate.png", "useTemplateIcon": True} if platform == "darwin" \
        else {"icon": "/resources/icons/tray.png"}
    return {**icon, "menuItems": [
        {"id": "status", "text": line, "isDisabled": True},
        {"text": "-"},
        {"id": "open", "text": words("desktop.tray.open")},
        {"id": "resume" if paused else "pause", "text": words("processing.resume_all" if paused
                                                               else "processing.pause_all")},
        {"text": "-"},
        {"id": "quit", "text": words("desktop.tray.quit")},
    ]}


def main_menu(words: Words) -> list:
    """macOS: the application menu (Neutralino's ``window.setMainMenu`` takes
    the menu list itself), with Quit on Cmd+Q, chosen as the loop's own event
    (``mainMenuItemClicked``, an item without a native ``action``), never
    NSApp's termination."""
    return [{"id": "app", "text": "DownloadPulse", "menuItems": [
        {"id": "quit", "text": words("desktop.tray.quit"), "shortcut": "q"}]}]


def open_methods(*, hidden: bool, minimized: bool) -> tuple[str, ...]:
    """Open's native calls for the window as it is. Hidden (closed to the
    tray): shown again and raised. Minimized: unminimized and raised -- on
    Wayland a client cannot unminimize itself, so the compositor decides and
    the taskbar always restores it. Shown: raised."""
    if hidden:
        return ("window.show", "window.focus")
    if minimized:
        return ("window.unminimize", "window.focus")
    return ("window.focus",)


def backend_argv(paths: dict[str, Path], port: int) -> list[str]:
    return [sys.executable, "-I", "-m", "uvicorn", "main:app", "--app-dir", str(paths["backend"]),
            "--host", "127.0.0.1", "--port", str(port), "--workers", "1", "--no-access-log", "--no-proxy-headers",
            "--timeout-graceful-shutdown", str(GRACEFUL_SECONDS)]


class ChildLog:
    """One child's log file (``backend.log``, ``shell.log``): the one writer
    both of its output drains (stdout, stderr) append to, bounded at
    ``max_bytes`` -- beyond it the file becomes ``<name>.1`` (earlier ones move
    up to ``.<backups>``, the oldest is dropped) and a new one starts. Shared by
    both drains, so a rotation never leaves either writing to a renamed file."""

    def __init__(self, path: Path, max_bytes: int = LOG_BYTES, backups: int = LOG_BACKUPS):
        self.path, self.max_bytes, self.backups = path, max_bytes, backups
        self._rotation_failed = False
        self._limit = max_bytes      # the size that next triggers a rotation
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "ab")

    def write(self, chunk: bytes) -> None:
        if self._file.tell() and self._file.tell() + len(chunk) > self._limit:
            self._rotate()
        self._file.write(chunk)
        self._file.flush()

    def _rotate(self) -> None:
        """Move the full file aside. A rotation that fails (on Windows another
        process holding the file without delete sharing refuses the rename)
        never stops the drains: the file is reopened and written on, and the
        failure is logged once -- an unread child output pipe would stall the
        child."""
        self._file.close()
        try:
            for index in range(self.backups - 1, 0, -1):
                older = self.path.with_name(f"{self.path.name}.{index}")
                if older.exists():
                    os.replace(older, self.path.with_name(f"{self.path.name}.{index + 1}"))
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
            self._limit = self.max_bytes
        except OSError as exc:
            if not self._rotation_failed:
                self._rotation_failed = True
                logger.warning("rotating %s failed (%s); writing on to it", self.path.name, type(exc).__name__)
        self._file = open(self.path, "ab")
        # Not again until the file has grown by another full bound (a refused rename is not retried per write).
        if self._file.tell() >= self._limit:
            self._limit = self._file.tell() + self.max_bytes

    def close(self) -> None:
        self._file.close()


def diagnostics_handler(root: Path, *, rotating: bool = True) -> logging.Handler:
    """The supervisor's own log, ``logs/desktop.log``: size-rotated like the
    children's (LOG_BYTES, LOG_BACKUPS) by the instance that holds the
    single-instance lock. Another (``rotating=False``) only appends to it --
    a ``maxBytes`` of 0 never rolls over -- so it never moves the running
    instance's log aside."""
    (root / "logs").mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(root / "logs" / "desktop.log",
                                                   maxBytes=LOG_BYTES if rotating else 0,
                                                   backupCount=LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    return handler


# Windows' own termination request: the installer's (packaging/desktop/windows/
# installer.nsi) way to close a running DownloadPulse before it replaces its
# files. A named event in this session's namespace, set by the installer; this
# process waits on it from a thread of its own.
QUIT_EVENT = "Local\\DownloadPulse-Quit"


class _WindowsQuitEvent:
    """The named quit event (``QUIT_EVENT``): ``callback`` runs on the loop when
    it is set. ``close`` lets the waiting thread go and closes both handles."""

    def __init__(self, loop: asyncio.AbstractEventLoop, callback):
        import ctypes
        import threading
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.SetEvent.argtypes = [wintypes.HANDLE]
        k32.ResetEvent.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD]
        k32.WaitForMultipleObjects.restype = wintypes.DWORD
        self.k32 = k32
        # Manual reset: set once, it stays set until this run ends. A stale set
        # from an earlier run (the installer set it after that run ended) is cleared.
        self.quit = k32.CreateEventW(None, True, False, QUIT_EVENT)
        self.stop = k32.CreateEventW(None, True, False, None)
        if not self.quit or not self.stop:
            raise OSError(ctypes.get_last_error(), "CreateEventW")
        k32.ResetEvent(self.quit)
        handles = (wintypes.HANDLE * 2)(self.quit, self.stop)

        def wait() -> None:
            if k32.WaitForMultipleObjects(2, handles, False, 0xFFFFFFFF) == 0:
                loop.call_soon_threadsafe(callback, "the installer")
        self.thread = threading.Thread(target=wait, name="windows-quit-event", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.k32.SetEvent(self.stop)
        self.thread.join(timeout=2)
        for handle in (self.quit, self.stop):
            self.k32.CloseHandle(handle)


class Signals:
    """POSIX: SIGTERM, SIGINT and SIGHUP end the run as Quit does, in order,
    by the run's phase; Windows: the installer's named quit event
    (``QUIT_EVENT``) does the same:

    * ``starting`` -- the first signal cancels the run once, so its own
      ``finally`` ends whatever has started (the shell, then the backend);
    * ``serving`` -- ``quitRequested`` on the shell's events: the loop returns
      0 and the ordered Quit runs;
    * ``quitting`` -- a signal is logged only: nothing interrupts the Quit
      already under way (the shell's end, the backend's, ``forget``, the lock).
    """

    NAMES = ("SIGTERM", "SIGINT", "SIGHUP")

    def __init__(self, task: asyncio.Task):
        self.task, self.phase, self.events, self.received = task, "starting", None, None
        self.served = False                         # the run reached its loop: its start succeeded
        self.windows: _WindowsQuitEvent | None = None

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        if os.name == "nt":
            try:
                self.windows = _WindowsQuitEvent(loop, self.signalled)
            except OSError as exc:
                # The installer then asks the person to Quit from the tray instead.
                logger.warning("the installer's quit event is unavailable: %s", exc)
            return
        for name in self.NAMES:
            loop.add_signal_handler(getattr(signal, name), self.signalled, name)

    def remove(self, loop: asyncio.AbstractEventLoop) -> None:
        if os.name == "nt":
            if self.windows is not None:
                self.windows.close()
                self.windows = None
            return
        for name in self.NAMES:
            loop.remove_signal_handler(getattr(signal, name))

    def serving(self, events: asyncio.Queue) -> None:
        self.phase, self.events, self.served = "serving", events, True

    def quitting(self) -> None:
        self.phase = "quitting"

    def signalled(self, name: str) -> None:
        if self.phase == "quitting":
            logger.info("%s while quitting: the shutdown goes on", name)
            return
        self.received = name
        if self.phase == "starting":
            logger.info("%s during start: stopping what has started, in order", name)
            self.phase = "quitting"
            self.task.cancel()
        else:
            logger.info("%s: quitting", name)
            self.phase = "quitting"
            self.events.put_nowait(("quitRequested", {"signal": name}))


async def start_failure_notice(words: "Words | None", platform: str = sys.platform, *,
                               body: str = "desktop.error.start_failed") -> None:
    """A start that failed, said where the person sees it: Linux, a portal
    notice on a connection of its own (``SessionBus.notice_once``: the run's
    bus may never have opened); Windows, its toast (``windows_notice``). macOS
    logs only: its notices are the native shell's, which is what failed or
    never started. Without its words (they could not be read) only the log
    says it. ``body`` is the catalog key of what it says beneath the title."""
    if words is None:
        return
    title, body = words("desktop.error.start_failed_title"), words(body)
    if platform.startswith("linux"):
        await SessionBus.notice_once(title, body)
    elif platform == "win32":
        await windows_notice(title, body)


# A run with administrator or root rights is refused (``privileged``) unless
# this is set to 1: for a disposable machine or container whose only account
# is root (the Flatpak's launch check, packaging/desktop/flatpak/ci-build.sh).
PRIVILEGED_OVERRIDE = "DOWNLOADPULSE_DESKTOP_ALLOW_PRIVILEGED"
PRIVILEGED_EXIT = 4


def privileged(platform: str = sys.platform) -> str | None:
    """What makes this run privileged, or None. DownloadPulse runs with its
    account's own rights, never more: Windows, a token UAC elevated (Run as
    administrator, or an installer started that way that starts DownloadPulse
    again) -- the same account has an unelevated one, so an elevated run
    would only hand the backend and every helper administrator rights, and
    create files in the account's own folders that an ordinary run then
    refuses as not its own (``private_directory``, ``read_credential``);
    POSIX (Linux, macOS), root. A Windows token without a split (UAC off, or
    the built-in Administrator) has no lesser token to run with and is not
    refused; one whose elevation cannot be read is."""
    if platform == "win32":
        try:
            kind = _win32_api().elevation_type()
        except (OSError, AttributeError) as exc:
            return f"a token whose elevation cannot be read ({exc})"
        return "an elevated (administrator) token" if kind == 2 else None          # TokenElevationTypeFull
    return "root" if os.geteuid() == 0 else None


async def refuse_privileged(paths: dict[str, Path], what: str) -> int:
    """A privileged run, refused before it opens, creates or locks anything
    in the data root (a file it created there would be owned by
    Administrators or root, and refused by the next ordinary run; and root
    must not write through a path its user controls): the reason goes to
    standard error and, where one can be shown, a notice in the
    installation's language (``start_failure_notice``)."""
    logger.error("DownloadPulse does not run as %s: start it again as this account itself (on Windows, not with "
                 "Run as administrator; elsewhere, not with sudo or as root). Nothing was started or written. "
                 "(%s=1 allows it on a disposable test system.)", what, PRIVILEGED_OVERRIDE)
    words = None
    try:
        sys.dont_write_bytecode = True                 # not even a cache file beside the program
        sys.path.insert(0, str(paths["backend"]))
        os.environ["STATIC_DIR"] = str(paths["static"])
        words = Words(data_root() / "config" / "config.json")
    except Exception:
        logger.exception("the refusal's notice has no words")
    await start_failure_notice(words, body="desktop.error.privileged")
    return PRIVILEGED_EXIT


async def run() -> int:
    paths = layout()
    elevated = privileged()
    if elevated is not None and os.environ.get(PRIVILEGED_OVERRIDE) != "1":
        return await refuse_privileged(paths, elevated)
    root = data_root()
    lock = single_instance(root)
    # The log, before anything else is said: whatever happens from here on --
    # another instance running, a start that fails -- is in it. Only the
    # instance holding the lock rotates it; another appends its line alone.
    logger.addHandler(diagnostics_handler(root, rotating=lock is not None))
    if lock is None:
        if sys.platform.startswith("linux") and await SessionBus.activate_running():
            return 0      # the running instance opens its window
        logger.error("DownloadPulse is already running for this account")
        return 3
    if elevated is not None:
        logger.warning("DownloadPulse runs as %s: allowed by %s=1", elevated, PRIVILEGED_OVERRIDE)
    loop = asyncio.get_running_loop()
    signals = Signals(asyncio.current_task())
    signals.install(loop)
    words = None
    try:
        sys.path.insert(0, str(paths["backend"]))
        os.environ["STATIC_DIR"] = str(paths["static"])
        config_path = root / "config" / "config.json"
        words = Words(config_path)                  # the notices' words, a failed start's too
        return await _run(paths, root, config_path, words, signals)
    except asyncio.CancelledError:
        if signals.received is None:
            raise
        logger.info("stopped by %s during start", signals.received)
        return 0
    except Exception:
        if not signals.served:
            logger.exception("DownloadPulse could not start")
            await start_failure_notice(words)
        else:
            logger.exception("the DownloadPulse desktop run failed")
        return 1
    finally:
        signals.remove(loop)
        lock.close()


async def _run(paths: dict[str, Path], root: Path, config_path: Path, words: Words, signals: Signals) -> int:
    import aiohttp
    from executors.process_ownership import ProcessOwnership

    db_path = root / "data" / "downloadpulse.db"
    for directory in (db_path.parent, config_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
    processes = ProcessOwnership(root / "desktop-runtime")
    port = window_port(root / "desktop-runtime")
    channel = Channel()
    request_end, answer_end = channel.child_ends()
    env = processes.environment({
        "DB_PATH": str(db_path), "CONFIG_PATH": str(config_path), "STATIC_DIR": str(paths["static"]),
        "DOWNLOADPULSE_DESKTOP": "1", "DOWNLOADPULSE_DESKTOP_CONTROL": f"{request_end},{answer_end}",
        # The window's own port, which Local Port relays to (application.desktop_listener).
        "DOWNLOADPULSE_DESKTOP_PORT": str(port),
        # The bundled tools, and the packaged rsync helper's own directory
        # (its MSYS runtime layout on Windows), before the system's.
        "PATH": os.pathsep.join([*(str(path) for path in (paths["tools"], paths["tools"] / "rsync" / "usr" / "bin")
                                   if path.is_dir()), os.environ.get("PATH", "")]),
        "HOME" if os.name != "nt" else "USERPROFILE": str(Path.home()),
    })
    for key in ("APPDATA", "LOCALAPPDATA", "TEMP", "TMP") if os.name == "nt" \
            else ("TMPDIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    for stale in (BACKEND_ID, SHELL_ID):   # a previous run that crashed: its groups end before ours start
        if processes.alive(stale):
            await processes.terminate(None, stale, grace=10)
        processes.forget(stale)
    backend = await processes.spawn(BACKEND_ID, backend_argv(paths, port), env=env,
                                    pass_fds=(request_end, answer_end))
    channel.started()
    logs = [ChildLog(root / "logs" / "backend.log")]
    drains = [asyncio.ensure_future(_drain(stream, logs[0]))
              for stream in (backend.process.stdout, backend.process.stderr)]
    bus = None
    try:
        async with aiohttp.ClientSession() as session:
            await _ready(session, port, backend)
            # The shell runs under DownloadPulse's own name (its webview keys its
            # data on it), in the environment shell_environment() gives it; its
            # one owner launches it -- now, and again after a release (Window).
            clear_cookie_store(root / "desktop-runtime")   # a new run asks for sign-in, as ever
            logs.append(ChildLog(root / "logs" / "shell.log"))
            async def appearance() -> str | None:
                return (await channel.ask("appearance")).get("mode")
            window = Window(session=session, processes=processes, paths=paths, root=root, port=port, log=logs[1],
                            appearance=appearance)
            try:
                await window.spawn(time.monotonic() + READY_SECONDS)
                if sys.platform.startswith("linux"):
                    bus = await SessionBus.open(window.events, paths["shell"] / "resources" / "icons")
                signals.serving(window.events)
                # A hidden window may be released everywhere: on Linux, whose tray is the
                # supervisor's own, its shell ends; on Windows and macOS, whose tray is the
                # shell's, the shell stays and its page is parked instead.
                park = not sys.platform.startswith("linux")
                return await _serve(channel, words, backend, bus or window.shell, window,
                                    release_allowed=bus is not None or park, park=park)
            finally:
                # Quit, in order: the shell and its page first, so no request
                # of the page is left open to hold the backend's shutdown.
                signals.quitting()
                await window.end()
                drains += window.drains
                clear_cookie_store(root / "desktop-runtime")
    finally:
        signals.quitting()
        if bus is not None:
            await bus.close()
        channel.close()
        if not await _ended(backend, QUIT_SECONDS):
            logger.error("the DownloadPulse backend did not stop in %ss; ending it forcibly", QUIT_SECONDS)
            await processes.terminate(backend, BACKEND_ID, grace=5)
        logger.info("the DownloadPulse backend ended %s (%s)", "through its own shutdown"
                    if backend.process.returncode == 0 else "abnormally", backend.process.returncode)
        processes.forget(BACKEND_ID)
        if processes.alive(SHELL_ID):
            await processes.terminate(None, SHELL_ID, grace=5)
        processes.forget(SHELL_ID)
        for task in drains:
            task.cancel()
        for log in logs:
            log.close()


def clear_cookie_store(runtime: Path) -> None:
    """The run-scoped cookie store and its SQLite sidecars, gone (every start,
    Quit, a failed relaunch): a new run asks for sign-in as before."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        (runtime / f"{COOKIE_STORE}{suffix}").unlink(missing_ok=True)


def relaunch_arguments(geometry: dict | None) -> list[str]:
    """A relaunched shell's window (Neutralino 6.10.0 ``--window-*``, which
    override its configuration): hidden until its relauncher shows it, so it
    never appears at another size first; at the size the window had
    (``window.getSize`` and ``--window-width`` are the same, unscaled pixels)
    and maximized if it was. Neutralino's own saved window state is set aside
    (``useSavedState`` defaults to on, and a saved file would override the
    size given here)."""
    arguments = ["--window-hidden=true", "--window-use-saved-state=false"]
    if geometry:
        arguments += [f"--window-width={geometry['width']}", f"--window-height={geometry['height']}"]
        if geometry.get("maximized"):
            arguments.append("--window-maximize=true")
    return arguments


class Window:
    """The native window's one owner (1.0.5 WS8d): its shell -- the credential
    prepared before every spawn, the spawn, its log, the connection, its end --
    and what the window is: hidden (by this loop), minimized, hidden since,
    released (its shell ended to free its memory) and the geometry it is
    brought back to. Every shell it launches shares one event queue; only the
    current one's end can mean Quit."""

    def __init__(self, *, session=None, processes=None, paths=None, root: Path | None = None, port: int = 0,
                 events: asyncio.Queue | None = None, log: "ChildLog | None" = None, appearance=None):
        self.session, self.processes, self.paths, self.root, self.port = session, processes, paths, root, port
        # ``appearance() -> mode``: the interface's mode, asked at each spawn (the backend's op).
        self.appearance = appearance
        self.events: asyncio.Queue = events if events is not None else asyncio.Queue()
        self.log, self.drains = log, []
        self.shell: Shell | None = None
        self.owned = None
        self.generation = 0
        self.hidden = False
        self.minimized = False
        self.hidden_since: float | None = None
        self.released = False
        # Released by parking its page (Windows and macOS): the shell stays, so
        # its end is still Quit.
        self.parked = False
        self.geometry: dict | None = None

    async def spawn(self, deadline: float, *, relaunch: bool = False) -> None:
        """One shell, in order: a leftover group ended first, the credential's
        private directory made (or no shell), then the spawn and connection.
        A relaunch's shell starts hidden at the geometry the window had
        (``relaunch_arguments``); its relauncher shows it."""
        if self.processes.alive(SHELL_ID):
            await self.processes.terminate(None, SHELL_ID, grace=5)
        if self.processes.alive(SHELL_ID):
            raise RuntimeError("a previous native shell is not proven gone")
        self.processes.forget(SHELL_ID)
        auth_file = shell_data_home() / APP_ID / ".tmp" / "auth_info.json"
        private_directory(auth_file.parent)
        auth_file.unlink(missing_ok=True)
        binary = shell_binary_path(self.paths["shell"])
        shell = Shell(self.session, self.events, generation=self.generation + 1)
        shell.binary = str(binary)
        env = shell_environment(self.root, mode=await self._mode())
        if sys.platform.startswith("linux"):
            runtime = self.root / "desktop-runtime"
            private_directory(runtime)
            env["DOWNLOADPULSE_COOKIE_STORE"] = str(runtime / COOKIE_STORE)
        argv = shell_argv(binary, self.paths["shell"], self.port)
        if relaunch:
            argv += relaunch_arguments(self.geometry)
        owned = await self.processes.spawn(SHELL_ID, argv, env=env)
        if self.log is not None:
            self.drains += [asyncio.ensure_future(_drain(stream, self.log))
                            for stream in (owned.process.stdout, owned.process.stderr)]
        # Owned from its spawn: whatever happens next, Quit ends it (``end``).
        self.shell, self.owned, self.generation = shell, owned, shell.generation
        await shell.connect(auth_file, deadline, owned)

    async def _mode(self) -> str | None:
        """The interface's mode now, or None (unknown: each platform's own background).

        A slow answer is waited for at most APPEARANCE_SECONDS, but never
        cancelled: an ask in flight keeps the channel (its lock and its answer
        line) until its answer is read, so the next request never reads it."""
        if self.appearance is None:
            return None
        try:
            asked = asyncio.ensure_future(self.appearance())
            asked.add_done_callback(lambda task: task.cancelled() or task.exception())   # read, never logged
            return await asyncio.wait_for(asyncio.shield(asked), APPEARANCE_SECONDS)
        except Exception as exc:  # noqa: BLE001 -- a background is never worth a failed launch
            logger.warning("the interface's mode is unknown; the window opens on its default background: %s",
                           exc or type(exc).__name__)
            return None

    async def call(self, method: str, data: dict | list | None = None) -> None:
        if self.shell is not None:
            await _window(self.shell, method, data)

    async def release(self) -> bool:
        """End the hidden window's shell to free its memory, keeping its
        geometry; whether its process group is proven gone (if not, the next
        relaunch ends it first, or says why it cannot)."""
        try:
            size = await self.shell.call("window.getSize")
            maximized = await self.shell.call("window.isMaximized")
            self.geometry = {"width": int(size["width"]), "height": int(size["height"]),
                             "maximized": bool(maximized.get("returnValue") if isinstance(maximized, dict) else maximized)}
        except (NativeError, TimeoutError, KeyError, TypeError, ValueError, OSError) as exc:
            logger.warning("the window's size was not read before its release: %s", exc)
        self.released = True                     # before the shell ends: its close is no Quit
        shell, owned, self.shell, self.owned = self.shell, self.owned, None, None
        try:
            if shell.ws is not None:
                await shell.ws.close()
        except (OSError, RuntimeError):
            pass
        await _end_shell(shell, owned, self.processes)
        gone = not self.processes.alive(SHELL_ID)
        if gone:
            self.processes.forget(SHELL_ID)
        logger.info("the window was released after %ss hidden (its shell %s)", RELEASE_AFTER_SECONDS,
                    "ended" if gone else "is NOT proven gone")
        return gone

    async def relaunch(self, *, minimized: bool = False) -> bool:
        """Open after a release: a new shell, started hidden at the geometry
        the window had, then shown (or minimized), all within
        RELAUNCH_SECONDS. Any failure on the way -- the spawn, its
        connection, a shell that dies or does not answer its first calls --
        keeps the run: the shell that started is ended, the window stays
        released, the tray works, and the next Open tries again."""
        async def reopen() -> None:
            await self.spawn(time.monotonic() + RELAUNCH_SECONDS, relaunch=True)
            await self.shell.call("window.show")
            if minimized:
                await self.shell.call("window.minimize")
        try:
            await asyncio.wait_for(reopen(), RELAUNCH_SECONDS)
        except Exception as exc:  # noqa: BLE001 -- any failure is the same answer: not reopened
            logger.error("the window could not be opened again: %s", exc or type(exc).__name__)
            await self.end()                     # a shell that started and failed is ended, as at Quit
            self.shell, self.owned = None, None
            clear_cookie_store(self.root / "desktop-runtime")
            return False
        self.released, self.hidden, self.hidden_since, self.minimized = False, False, None, minimized
        logger.info("the window was opened again (shell generation %s)", self.generation)
        return True

    async def park(self, channel: "Channel") -> bool:
        """Release a hidden window whose shell must stay (Windows and macOS:
        the tray is the shell's own): its page is told to leave for the parked
        page, freeing what the application's page held; Open brings it back
        (``unpark``). Whether a page was told (if not, nothing changed)."""
        self.parked = bool((await channel.ask("park")).get("parked"))
        logger.info("the window was released after %ss hidden (%s)", RELEASE_AFTER_SECONDS,
                    "its page parked" if self.parked else "NOT parked: no window page to tell")
        return self.parked

    async def unpark(self, channel: "Channel") -> None:
        """Open on a parked window: its page goes back to the application,
        which takes the kept state; the window is then shown as a hidden one is."""
        arrived = (await channel.ask("unpark")).get("parked")
        self.parked = False
        logger.info("the parked window's page goes back to the application%s",
                    "" if arrived else " (it had not left: nothing to restore)")

    async def end(self) -> None:
        """Quit: the current shell, if there is one (a released window has none)."""
        if self.shell is not None and self.owned is not None:
            await _end_shell(self.shell, self.owned, self.processes)


async def _serve(channel: Channel, words: Words, backend, native, window: Window, *,
                 release_allowed: bool = False, park: bool = False) -> int:
    """One run's loop. ``native`` shows the tray and the notices (the shell,
    or Linux's ``SessionBus``); the window is ``window``'s (``Window``).
    ``release_allowed``: a window hidden for RELEASE_AFTER_SECONDS is released
    once its page answers that nothing would be lost (1.0.5 WS8d), and Open
    brings it back. ``park`` (the run's fact: Windows and macOS, whose tray is
    the shell's own): released by parking its page, the shell kept
    (``Window.park``); otherwise (Linux) its shell ends (``Window.release``)."""
    asked_release: float | None = None          # when the page was asked (release_prompt); its answer comes with show
    next_release = 0.0
    cursor = (await channel.ask("events"))["cursor"]
    status = (await channel.ask("status")).get("status")
    words.choose((await channel.ask("locale")).get("locale"))       # the interface's language from the first draw
    await native.tray(tray_menu(status, words))
    # Local Port taken at launch: said once, as the window opens.
    if (unavailable := local_port_notice(status, words)) is not None:
        await native.notice(*unavailable)
    if sys.platform == "darwin":
        # The application menu: its Quit (Cmd+Q) is this loop's Quit, never
        # the shell's own termination (_end_shell).
        await window.call("window.setMainMenu", main_menu(words))
    applied_frame = None                        # the frame last given to the window (apply_frame)
    next_poll = time.monotonic() + POLL_SECONDS
    next_show = time.monotonic() + SHOW_POLL_SECONDS
    # Work this loop starts and does not wait for (a Show in folder, a save's
    # dialog): each task is held here until it ends, so it cannot be collected
    # while it runs, and a failure is logged, never left unretrieved; Quit
    # cancels and awaits what is still running (``_end_background``).
    running: set[asyncio.Task] = set()

    def in_background(work, what: str) -> None:
        task = asyncio.ensure_future(work)
        running.add(task)

        def ended(done: asyncio.Task) -> None:
            running.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.error("%s failed", what, exc_info=done.exception())
        task.add_done_callback(ended)

    async def bring_forward(why: str) -> None:
        # What Open can do depends on what the window is: a released one is
        # launched again (it opens shown); a window this loop hid is shown
        # again (the shell maps it); a minimized one is asked to come back,
        # which on Wayland is the compositor's to grant (the taskbar always
        # can); a shown one is raised.
        nonlocal applied_frame, asked_release
        asked_release = None                    # shown again: a late answer to an earlier prompt is no answer
        if window.parked:
            # Its page goes back to the application; the window is shown as a hidden one is.
            await window.unpark(channel)
        if window.released:
            logger.info("%s: the window was released; opening it again", why)
            if await window.relaunch():
                applied_frame = None            # the new window takes the page's frame again
            else:
                await native.notice(words("desktop.error.reopen_failed_title"), words("desktop.error.reopen_failed_body"))
            return
        methods = open_methods(hidden=window.hidden, minimized=window.minimized)
        logger.info("%s: the window was %s; %s", why, "hidden" if window.hidden else "minimized" if window.minimized
                    else "shown", ", ".join(methods))
        window.hidden, window.hidden_since = False, None
        for method in methods:
            await window.call(method)

    try:
        while backend.process.returncode is None:
            try:
                event, data = await asyncio.wait_for(window.events.get(),
                                                     max(0.1, min(next_poll, next_show) - time.monotonic()))
            except TimeoutError:
                event, data = None, None
            if event in {"windowMinimize", "windowRestore"}:
                window.minimized = event == "windowMinimize"
            elif event == "windowClose":
                if native.tray_shown:
                    # Hidden where the tray can open it again.
                    window.hidden, window.hidden_since = True, time.monotonic()
                    await window.call("window.hide")
                elif sys.platform.startswith("linux") and (await channel.ask("quit_prompt")).get("prompted"):
                    # No tray to open it again (a desktop without one: stock
                    # GNOME): closing is Quit, once the person confirms it in the
                    # window's own page, which stays up to ask (the answer comes
                    # with ``show``: ``quit``).
                    logger.info("the window was closed with no tray: its page asks whether to quit")
                else:
                    # No page can ask (none loaded, signed out), or Windows and
                    # macOS: the window stays one click away, minimized, and
                    # DownloadPulse keeps running.
                    await window.call("window.minimize")
                    window.minimized = True
            elif event == "trayAvailability" and not (data or {}).get("shown") and window.released:
                # The tray went away while the released window had only it: the
                # window is launched again, minimized, where the desktop can open it.
                logger.warning("the tray went away while the window was released; opening it minimized")
                if await window.relaunch(minimized=True):
                    applied_frame = None
                else:
                    # No tray and no window: the notice says so; the tray's
                    # return (or a connected browser's Open) tries again.
                    await native.notice(words("desktop.error.reopen_failed_title"),
                                        words("desktop.error.reopen_failed_body"))
            elif event == "trayAvailability" and not (data or {}).get("shown") and window.hidden:
                # The tray went away while it was the only way back: the window
                # returns, minimized, where the desktop can still open it.
                logger.warning("the tray went away while the window was hidden; showing it minimized")
                window.hidden, window.hidden_since, window.minimized = False, None, True
                for method in ("window.show", "window.minimize"):
                    await window.call(method)
            elif event == "mainMenuItemClicked" and (data or {}).get("id") == "quit":
                logger.info("Quit (the application menu, Cmd+Q)")
                return 0
            elif event == "trayMenuItemClicked":
                chosen = (data or {}).get("id")
                if chosen == "open":
                    await bring_forward("Open")
                elif chosen in {"pause", "resume"}:
                    await channel.ask(chosen)
                    next_poll = 0
                elif chosen == "quit":
                    logger.info("Quit (the tray)")
                    return 0
            elif event == "quitRequested":
                logger.info("Quit (%s)", (data or {}).get("signal"))
                return 0
            elif event == "shellClosed":
                if window.released or (data or {}).get("generation", window.generation) != window.generation:
                    continue                    # a shell this loop retired (a release): no Quit
                # The shell ended by itself (macOS: its own termination, the Dock's
                # Quit); its exit status is recorded where it ends (_end_shell).
                logger.info("the native shell ended by itself")
                return 0
            if time.monotonic() >= next_show:
                asked = await channel.ask("show")
                if asked.get("quit"):
                    # The window's own page confirmed Quit (a close with no tray).
                    logger.info("Quit (confirmed in the window)")
                    return 0
                # A connected browser's Open DownloadPulse: the tray's Open.
                if asked.get("show"):
                    await bring_forward("Open (a connected browser)")
                # Show in folder, asked in the window: the file manager, on its own.
                if asked.get("reveal"):
                    in_background(show_in_folder(asked["reveal"]), "Show in folder")
                # The page's answer to a release prompt: released only if it says
                # nothing would be lost, the window is still hidden -- and has
                # been for RELEASE_AFTER_SECONDS -- and no desktop save waits;
                # ``released`` keeps the state it handed (sent before the shell
                # ends: a refused hand-over leaves the window as it is).
                answer = asked.get("release")
                if answer and asked_release is not None:
                    asked_release = None
                    if (answer.get("releasable") and window.hidden and not window.released and not window.parked
                            and window.hidden_since is not None
                            and time.monotonic() - window.hidden_since >= RELEASE_AFTER_SECONDS
                            and not (await channel.ask("saves")).get("saves")
                            and (await channel.ask("released", nonce=answer.get("nonce"))).get("ok")):
                        await (window.park(channel) if park else window.release())
                if window.parked and window.shell is not None and sys.platform == "darwin":
                    # Shown by the system itself while parked (macOS: the Dock): the application comes back.
                    try:
                        visible = await window.shell.call("window.isVisible")
                    except (NativeError, TimeoutError, OSError) as exc:
                        logger.warning("whether the parked window is shown was not read: %s", exc)
                    else:
                        if (visible.get("returnValue") if isinstance(visible, dict) else visible) is True:
                            await bring_forward("Shown by the system")
                next_show = time.monotonic() + SHOW_POLL_SECONDS
            now = time.monotonic()
            if asked_release is not None and now - asked_release >= RELEASE_RETRY_SECONDS:
                asked_release = None                # unanswered (its page did not run, or its answer failed): asked again
            if (release_allowed and window.hidden and not window.released and not window.parked
                    and asked_release is None
                    and window.hidden_since is not None and now - window.hidden_since >= RELEASE_AFTER_SECONDS
                    and now >= next_release):
                # Hidden long enough: its own page is asked whether it may be released.
                if (await channel.ask("release_prompt")).get("prompted"):
                    asked_release = now
                next_release = now + RELEASE_RETRY_SECONDS
            if time.monotonic() >= next_poll:
                batch = await channel.ask("events", after=cursor)
                cursor = batch.get("cursor", cursor)
                for title, body in notices(batch.get("events") or [], words):
                    await native.notice(title, body)
                current = (await channel.ask("status")).get("status")
                # The tray in the language the interface is shown in: a change there is redrawn now.
                relabel = words.choose((await channel.ask("locale")).get("locale"))
                if current != status or relabel or not native.tray_shown:
                    status = current
                    await native.tray(tray_menu(status, words))
                if relabel and sys.platform == "darwin":
                    # macOS: the application menu (its Quit) speaks the new language too.
                    await window.call("window.setMainMenu", main_menu(words))
                # The window frame in the page's theme, whenever the page says a new one.
                frame = (await channel.ask("frame")).get("frame")
                if frame and frame != applied_frame and window.shell is not None:
                    applied_frame = frame
                    await apply_frame(window.shell, frame)
                # Desktop saves waiting for a destination: each in its own task, so
                # the window and tray stay live while its dialog is open.
                for waiting in (await channel.ask("saves")).get("saves") or []:
                    in_background(_save(channel, native, words, waiting), "A desktop save")
                next_poll = time.monotonic() + POLL_SECONDS
        logger.error("the DownloadPulse backend ended on its own (%s)", backend.process.returncode)
        return 1
    finally:
        # Quit, first of all: the work this loop started ends with it, while
        # the channel and the shell it may still use are up.
        await _end_background(running)


async def _end_background(tasks: set[asyncio.Task]) -> None:
    """At Quit, the loop's own background work (a Show in folder, a save's
    dialog) is cancelled and awaited, at most BACKGROUND_END_SECONDS: a save
    still in its dialog answers the backend that it was cancelled, a file
    manager already started is left to itself. Nothing is killed; work that
    does not end in time is said in the log and left to the run's end."""
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if not pending:
        return
    _done, unfinished = await asyncio.wait(pending, timeout=BACKGROUND_END_SECONDS)
    if unfinished:
        logger.warning("%s background task(s) did not end in %ss at Quit", len(unfinished), BACKGROUND_END_SECONDS)


async def apply_frame(shell: "Shell", frame: dict) -> None:
    """The window's own frame in the page's theme (``frame``: caption and text
    ``#rrggbb``, dark). macOS: the shell's ``window.setFrameTheme``
    (packaging/desktop/neutralino/macos-frame-theme.patch). Windows: the title
    bar of every window of the shell (``windows_frame``). Linux keeps its
    desktop's frame. A frame that cannot be applied is logged, never fatal."""
    try:
        if sys.platform == "darwin":
            await shell.call("window.setFrameTheme", {"caption": frame["caption"], "dark": bool(frame["dark"])})
        elif os.name == "nt":
            coloured = await asyncio.to_thread(windows_frame, shell.binary, frame["caption"], frame["text"],
                                               bool(frame["dark"]))
            if not coloured:
                logger.warning("no window of the shell to give the frame")
    except (NativeError, OSError, TimeoutError, KeyError) as exc:
        logger.warning("the window frame could not be applied: %s", exc)


def colorref(value: str) -> int:
    """``#rrggbb`` as Windows' COLORREF (0x00bbggrr)."""
    red, green, blue = (int(value[index:index + 2], 16) for index in (1, 3, 5))
    return blue << 16 | green << 8 | red


# Windows notices: a toast sent under DownloadPulse's application ID, which the installer
# registers (HKCU\Software\Classes\AppUserModelId\net.downloadpulse.desktop: DisplayName,
# IconUri), so Windows names it DownloadPulse with its icon. The title and body reach the
# script only as environment values and become XML text nodes: nothing is parsed from them.
WINDOWS_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text/><text/></binding></visual></toast>')
$texts = $xml.GetElementsByTagName('text')
$null = $texts.Item(0).AppendChild($xml.CreateTextNode($env:DP_NOTICE_TITLE))
$null = $texts.Item(1).AppendChild($xml.CreateTextNode($env:DP_NOTICE_BODY))
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:DP_NOTICE_APP).Show(
    [Windows.UI.Notifications.ToastNotification]::new($xml))
"""
WINDOWS_NOTICE_SECONDS = 20


def windows_notice_command(title: str, body: str, environ=None) -> tuple[list[str], dict[str, str]]:
    """The Windows PowerShell 5.1 command (by its absolute path) and environment
    that show one notice."""
    environ = dict(os.environ if environ is None else environ)
    root = environ.get("SystemRoot") or environ.get("SYSTEMROOT") or r"C:\Windows"
    powershell = rf"{root}\System32\WindowsPowerShell\v1.0\powershell.exe"
    encoded = base64.b64encode(WINDOWS_TOAST_SCRIPT.encode("utf-16-le")).decode("ascii")
    argv = [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-EncodedCommand", encoded]
    environ.update(DP_NOTICE_APP=APP_ID, DP_NOTICE_TITLE=title, DP_NOTICE_BODY=body or "")
    return argv, environ


async def windows_notice(title: str, body: str) -> bool:
    """Show one Windows notice; whether Windows accepted it (False falls back
    to the shell's own)."""
    argv, environ = windows_notice_command(title, body)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, env=environ, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, creationflags=0x08000000)          # CREATE_NO_WINDOW
        _out, err = await asyncio.wait_for(process.communicate(), WINDOWS_NOTICE_SECONDS)
    except OSError as exc:
        logger.error("a Windows notice was not shown: %s", exc)
        return False
    except asyncio.TimeoutError:
        process.kill()
        logger.error("a Windows notice was not shown: no answer in %s seconds", WINDOWS_NOTICE_SECONDS)
        return False
    if process.returncode != 0:
        reason = " ".join(err.decode("utf-8", "replace").split())[:300]
        logger.error("a Windows notice was not shown (exit %s): %s", process.returncode, reason)
        return False
    return True


def windows_frame(binary: str, caption: str, text: str, dark: bool) -> int:
    """Windows: every top-level window of the process running ``binary`` (the
    shell, which ProcessOwnership starts behind its gate) gets the frame:
    dark mode for a dark theme (Windows 10 20H1+), and on Windows 11 the
    caption and its text in the theme's colours (DwmSetWindowAttribute
    DWMWA_USE_IMMERSIVE_DARK_MODE 20, DWMWA_CAPTION_COLOR 35,
    DWMWA_TEXT_COLOR 36; an attribute Windows lacks is simply refused). How
    many windows were coloured."""
    import ctypes
    from ctypes import wintypes as w
    user32, kernel32, dwm = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32"), ctypes.WinDLL("dwmapi")
    callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user32.EnumWindows.argtypes = [callback, w.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    kernel32.OpenProcess.argtypes, kernel32.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel32.CloseHandle.argtypes = [w.HANDLE]
    dwm.DwmSetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
    target = os.path.normcase(os.path.realpath(binary))
    found = []

    def visit(hwnd, _):
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = kernel32.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
        if process:
            image, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
            if kernel32.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)) \
                    and os.path.normcase(os.path.realpath(image.value)) == target:
                found.append(hwnd)
            kernel32.CloseHandle(process)
        return True

    user32.EnumWindows(callback(visit), 0)
    for hwnd in found:
        for attribute, value in ((20, int(dark)), (35, colorref(caption)), (36, colorref(text))):
            data = w.DWORD(value)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))
    return len(found)


# Show in folder: a completed download in the desktop's own file manager, as
# the backend resolved it (``application.desktop_control`` op show: reveal) --
# never a path from the page. Each is one process started directly, no shell:
# Windows ``explorer /select,"<file>"`` (a folder: ``explorer "<folder>"``), as
# one literal command line -- Explorer ignores a /select switch quoted whole,
# which is what an argument list becomes -- that CreateProcess starts (a
# Windows path cannot hold a ``"``); macOS ``open -R <path>``, file or folder
# alike (revealed in Finder, never opened: a folder may be an application
# bundle); Linux the desktop's OpenURI portal's OpenDirectory for a file (its
# folder, the file selected; inside the Flatpak the one way out), else -- and
# for a folder -- ``xdg-open <folder>`` (inside the Flatpak the portal's).
REVEAL_SECONDS = 10


def reveal_command(path: str, folder: bool, platform: str = sys.platform, environ=None) -> list[str] | str:
    """What shows ``path`` in the file manager: on Windows the literal command
    line, elsewhere the argument list."""
    if platform == "win32":
        environ = os.environ if environ is None else environ
        root = environ.get("SystemRoot") or environ.get("SYSTEMROOT") or r"C:\Windows"
        explorer = rf'"{root}\explorer.exe"'
        return f'{explorer} "{path}"' if folder else f'{explorer} /select,"{path}"'
    if platform == "darwin":
        return ["/usr/bin/open", "-R", path]
    return ["xdg-open", path if folder else os.path.dirname(path)]


def portal_open_directory(path: str) -> bool:
    """Linux: the OpenURI portal's OpenDirectory for one file -- its folder in
    the file manager, the file selected -- handed over as an open descriptor
    (a connection of its own: descriptors travel only on one that allows
    them). Whether the portal SAID it opened it: its request's Response is 0.

    The descriptor is opened ``O_RDONLY`` (never ``O_PATH``): the portal
    refuses an ``O_PATH`` one opened ``O_NOFOLLOW``, and judges an ``O_PATH``
    one by the sandbox mount's writability, refusing a sandboxed caller's
    writable one -- a refusal it says only in that Response (2), after
    accepting the call (1.0.5 WS2). ``O_NOFOLLOW`` still refuses a final
    symlink; ``O_NONBLOCK`` keeps a FIFO from holding the reveal (a regular
    file opens as ever)."""
    import secrets
    try:
        from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except ImportError:
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        logger.error("Show in folder: the file could not be opened for the portal: %s", exc)
        return False
    token = "dp" + secrets.token_hex(8)
    try:
        with open_dbus_connection("SESSION", enable_fds=True) as connection:
            sender = (connection.unique_name or "").lstrip(":").replace(".", "_")
            request = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
            # The answer is the request's Response signal: listened for before
            # asking, so it cannot come first.
            rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response",
                             path=request)
            with connection.filter(rule) as answers:
                connection.send_and_get_reply(message_bus.AddMatch(rule), timeout=REVEAL_SECONDS)
                reply = connection.send_and_get_reply(new_method_call(
                    DBusAddress("/org/freedesktop/portal/desktop", bus_name="org.freedesktop.portal.Desktop",
                                interface="org.freedesktop.portal.OpenURI"),
                    "OpenDirectory", "sha{sv}", ("", fd, {"handle_token": ("s", token)})), timeout=REVEAL_SECONDS)
                if reply.header.message_type.name == "error":
                    logger.warning("Show in folder: the portal refused: %s", reply.body)
                    return False
                answer = connection.recv_until_filtered(answers, timeout=REVEAL_SECONDS)
    except (OSError, KeyError, ValueError, TimeoutError) as exc:
        logger.warning("Show in folder: the portal could not be asked: %s", exc)
        return False
    finally:
        os.close(fd)
    response = answer.body[0] if answer.body else 2
    if response != 0:
        logger.warning("Show in folder: the portal did not open it (response %s)", response)
        return False
    return True


def _start_windows(command_line: str) -> None:
    """Windows: start ``command_line`` as it is (CreateProcess, no shell).
    Explorer's exit status says nothing (it answers 1 when it opened)."""
    import subprocess
    subprocess.Popen(command_line, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


async def show_in_folder(reveal, platform: str = sys.platform) -> bool:
    """Show one completed download in the file manager (``reveal``: the
    backend's ``{"path", "folder"}``); whether it was handed to the desktop.
    Anything else is refused and logged."""
    path = reveal.get("path") if isinstance(reveal, dict) else None
    folder = reveal.get("folder") if isinstance(reveal, dict) else None
    import ntpath
    import posixpath
    absolute = ntpath.isabs if platform == "win32" else posixpath.isabs
    if not isinstance(path, str) or not isinstance(folder, bool) or not absolute(path) or "\0" in path \
            or (platform == "win32" and '"' in path):
        logger.error("Show in folder: refused a request that is not an absolute path")
        return False
    command = reveal_command(path, folder, platform)
    if platform == "win32":
        try:
            await asyncio.to_thread(_start_windows, command)
        except OSError as exc:
            logger.error("Show in folder: Explorer could not be started: %s", exc)
            return False
        return True
    if platform.startswith("linux"):
        if not folder and await asyncio.to_thread(portal_open_directory, path):
            return True
        if not os.path.isdir(command[1]):
            logger.error("Show in folder: the folder is not there")
            return False
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
    except OSError as exc:
        logger.error("Show in folder: %s could not be started: %s", command[0], exc)
        return False
    try:
        code = await asyncio.wait_for(process.wait(), REVEAL_SECONDS)
    except TimeoutError:
        return True                                 # still opening: the file manager's own time
    if code != 0:
        logger.error("Show in folder: %s ended with %s", command[0], code)
        return False
    return True


async def _save(channel, native, words, waiting: dict) -> None:
    """One desktop save: the person's Save dialog, then its answer to the
    backend (a path, or None for cancelled), which writes the file."""
    path = None
    try:
        path = await native.save_destination(str(waiting.get("name") or "downloadpulse-file"), words("desktop.save.title"))
    finally:
        try:
            await channel.ask("saved", save=waiting["save"], path=path)
        except (ConnectionError, OSError, ValueError) as exc:
            logger.error("a desktop save's answer did not reach the backend: %s", exc)


async def _end_shell(shell: Shell, owned, processes) -> None:
    """The native shell's end at Quit, in order, recorded as what it is -- the
    shell's end, never the backend's.

    * macOS: its own ``app.exit``. The shell DownloadPulse builds
      (packaging/desktop/neutralino/macos-main-thread-shutdown.patch) tears its
      status item and window down on the main thread, and a termination
      signal is an event on its main queue that ends it the same way.
    * Linux: SIGTERM; its handler runs ``cleanupTray`` (a no-op: the tray is
      the supervisor's) and ``_Exit``.
    * Windows: its job, as the process owner ends one.

    Only a shell that does not end in time is stopped by the process owner
    (SIGTERM, then SIGKILL: the bounded fallback for a stuck process), and
    the log says so. The shell keeps nothing to save."""
    if owned.process.returncode is not None or await _ended(owned, 0.5):
        code = owned.process.returncode
        logger.info("the native shell had ended by itself (%s%s)", code,
                    f": signal {-code}, abnormal" if code is not None and code < 0 else "")
        return
    if sys.platform == "darwin":
        try:
            await shell.exit()
        except (OSError, RuntimeError) as exc:
            logger.warning("the native shell was not asked to exit: %s", exc)
        if await _ended(owned, SHELL_EXIT_SECONDS):
            logger.info("the native shell exited by its own app.exit (%s)", owned.process.returncode)
            return
        logger.error("the native shell did not exit in %ss; stopping it", SHELL_EXIT_SECONDS)
    ended = await processes.terminate(owned, SHELL_ID, grace=SHELL_EXIT_SECONDS)
    code = owned.process.returncode
    how = ("its job ended" if os.name == "nt"
           else "SIGTERM, through its own handler" if code == 128 + 15
           else "SIGTERM" if code == -15 else "forcibly, SIGKILL after an unanswered SIGTERM")
    logger.info("the native shell was ended by the supervisor: %s%s (%s)", how,
                "" if ended else " -- NOT proven gone", code)


async def _window(shell: Shell, method: str, data: dict | list | None = None) -> None:
    try:
        await shell.call(method, data)
    except NativeError as exc:
        logger.error("the window did not respond: %s", exc)


async def _ready(session, port: int, backend) -> None:
    deadline = time.monotonic() + READY_SECONDS
    while time.monotonic() < deadline:
        if backend.process.returncode is not None:
            raise RuntimeError(f"the DownloadPulse backend ended during start ({backend.process.returncode})")
        try:
            async with session.get(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, asyncio.TimeoutError, Exception):  # noqa: BLE001 -- not listening yet
            pass
        await asyncio.sleep(0.25)
    raise TimeoutError("the DownloadPulse backend did not become ready")


async def _ended(owned, seconds: float) -> bool:
    try:
        await asyncio.wait_for(owned.process.wait(), seconds)
        return True
    except TimeoutError:
        return False


async def _drain(stream, log: ChildLog) -> None:
    while chunk := await stream.read(65536):
        log.write(chunk)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
