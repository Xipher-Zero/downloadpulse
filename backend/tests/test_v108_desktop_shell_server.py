"""The desktop shell's own loopback server (1.0.8 WS-A1).

Neutralino's HTTP server, run on the shell's folder (``--load-dir-res``),
served any file the account could read (``/..%2f..``), to any local program
or, by DNS rebinding, to a web page; and ``/__neutralino_globals.js`` gave
the shell's access key (NL_TOKEN) to whoever asked first. Now, on every
system:

* the shell runs on its ``resources.neu`` bundle only, which the assembler
  builds and the supervisor proves whole before every start (a missing or
  unreadable one refuses the shell: Neutralino would silently fall back to
  its folder);
* the supervisor takes the one-time key itself, right after reading the
  exported credential and before its own native connection (so before a
  relaunched window is shown); a key someone else took, a shell not serving
  its bundle, or no answer at all ends the start;
* the shells DownloadPulse builds (Linux, macOS) carry
  shell-server-boundary.patch: only paths inside the document root, only a
  Host of 127.0.0.1 or localhost at its port, no WebSocket with an Origin.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
import yaml
from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop import supervisor  # noqa: E402
from test_v103_desktop_lifecycle import fake_shell_bundle  # noqa: E402

CONFIG = json.loads((ROOT / "desktop" / "shell" / "neutralino.config.json").read_text(encoding="utf-8"))
NEUTRALINO = ROOT / "packaging" / "desktop" / "neutralino"
PATCH = "shell-server-boundary.patch"


def _neutralino_lookup(bundle: bytes, path: str) -> bytes | None:
    """Neutralino 6.10.0's own bundle read (resources.cpp __makeBundleFileTree,
    __seekFilePos, __getFileFromBundle): the header's size at bytes 4..8 less
    8, the JSON tree at 16, each '/' segment looked up by name (an empty one
    skipped), the data at the header's end plus the file's offset."""
    size = struct.unpack_from("<I", bundle, 4)[0] - 8
    node = json.loads(bundle[16:16 + size].rstrip(b"\0").decode())
    for segment in path.split("/"):
        if not segment or node is None or node.get("files") is None:
            continue
        node = node["files"].get(segment)
    if node is None:
        return None
    start = size + 16 + int(node["offset"])
    return bundle[start:start + node["size"]]


# ── the bundle ─────────────────────────────────────────────────────────────

def test_the_shell_never_runs_on_its_folder_on_any_platform(tmp_path):
    fake_shell_bundle(tmp_path)
    for platform in ("win32", "darwin", "linux"):
        binary = supervisor.shell_binary_path(tmp_path, platform)
        argv = supervisor.shell_argv(binary, tmp_path, 8123)
        assert argv == [str(binary), f"--path={tmp_path}", "--res-mode=bundle", "--url=http://127.0.0.1:8123/"]
        assert not any(arg.startswith("--load-dir-res") or arg == "--res-mode=directory" for arg in argv)
    # No launcher of any installer starts the shell on its folder either.
    for recipe in ("desktop/supervisor.py", "packaging/desktop/macos/build-app.sh",
                   "packaging/desktop/windows/installer.nsi", "packaging/desktop/flatpak/net.downloadpulse.desktop.yml"):
        text = (ROOT / recipe).read_text(encoding="utf-8")
        assert '"--load-dir-res"' not in text and "--res-mode=directory" not in text, recipe


def test_the_bundle_the_assembler_builds_is_read_by_neutralino_and_holds_only_what_the_shell_loads(tmp_path):
    bundle = fake_shell_bundle(tmp_path).read_bytes()
    assert not (tmp_path / "neutralino.config.json").exists()        # nothing loose for a folder fallback
    config = _neutralino_lookup(bundle, "/neutralino.config.json")
    assert json.loads(config) == CONFIG
    for name in supervisor.SHELL_BUNDLE_FILES[1:]:                     # the icons, byte for byte
        assert _neutralino_lookup(bundle, "/" + name) == (tmp_path / name).read_bytes()
    # What the server is asked (its document root prepended, URL-decoded) finds
    # names in the bundle only: no segment leaves it, nothing on the disk is read.
    for request in ("/resources/../neutralino.config.json", "/resources/../../etc/passwd",
                    "/resources//../../../tmp/secret", "/resources/icons/../../neutralino.config.json"):
        assert _neutralino_lookup(bundle, request) is None, request
    header = json.loads(bundle[16:16 + struct.unpack_from("<I", bundle, 4)[0] - 8])
    assert set(header["files"]) == {"neutralino.config.json", "resources"}
    assert set(header["files"]["resources"]["files"]["icons"]["files"]) == {"app.png", "tray.png", "trayTemplate.png"}


@pytest.mark.parametrize("damage", ["missing", "empty", "truncated", "garbage", "folder", "no-icon"])
def test_a_missing_or_unreadable_bundle_refuses_the_shell_with_no_spawn(tmp_path, monkeypatch, damage):
    shell = tmp_path / "shell"
    shell.mkdir()
    path = shell / supervisor.SHELL_BUNDLE
    if damage != "missing":
        good = fake_shell_bundle(shell).read_bytes()
        path.unlink()
        if damage == "empty":
            path.write_bytes(b"")
        elif damage == "truncated":
            path.write_bytes(good[:len(good) - 3])
        elif damage == "garbage":
            path.write_bytes(b"\x04\0\0\0" + b"\xff" * 64)
        elif damage == "folder":
            path.mkdir()
        else:
            size = struct.unpack_from("<I", good, 4)[0] - 8
            tree = json.loads(good[16:16 + size])
            del tree["files"]["resources"]["files"]["icons"]["files"]["tray.png"]
            header = json.dumps(tree).encode()
            header += b" " * (-len(header) % 4)
            path.write_bytes(struct.pack("<4I", 4, len(header) + 8, len(header) + 4, len(header)) + header
                             + good[16 + size:])
    spawned = []

    class Owner:
        def alive(self, _identity):
            return False

        def forget(self, _identity):
            pass

        async def spawn(self, identity, argv, env=None, **_):
            spawned.append(argv)
            raise AssertionError("no shell may start without its bundle")

    monkeypatch.setattr(supervisor, "shell_data_home", lambda: tmp_path / "data-home")
    (tmp_path / "data-home" / supervisor.APP_ID).mkdir(parents=True)
    window = supervisor.Window(processes=Owner(), paths={"shell": shell}, root=tmp_path, port=1)
    with pytest.raises(RuntimeError, match="resource bundle .* is missing or unreadable.*the shell is not started"):
        asyncio.run(window.spawn(time.monotonic() + 5))
    assert spawned == [] and window.shell is None


# ── the one-time key ───────────────────────────────────────────────────────

class FakeNeutralino:
    """Neutralino's loopback server as the supervisor meets it: the globals
    (``token``: what the first request gets, then ''; ``mode``: NL_RESMODE;
    ``status``), and the native WebSocket. ``seen`` is the order of it all."""

    def __init__(self, *, token="secret.connect", mode="bundle", status=200):
        self.token, self.mode, self.status, self.seen, self.given = token, mode, status, [], False

    async def start(self):
        async def globals_(request):
            self.seen.append(("globals", request.headers.get("Host")))
            token, self.given = ("" if self.given else self.token), True
            return web.Response(status=self.status, content_type="application/javascript",
                                text=f"var NL_PORT={self.port};var NL_TOKEN='{token}';var NL_RESMODE='{self.mode}';")

        async def native(request):
            self.seen.append(("ws", request.query.get("connectToken"), request.headers.get("Origin")))
            sock = web.WebSocketResponse()
            await sock.prepare(request)
            async for message in sock:
                call = json.loads(message.data)
                self.seen.append(("call", call["method"], call["accessToken"]))
                await sock.send_str(json.dumps({"id": call["id"], "data": {"success": True}}))
            return sock

        app = web.Application()
        app.router.add_get("/__neutralino_globals.js", globals_)
        app.router.add_get("/", native)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    def credential(self, path: Path) -> None:
        path.write_text(json.dumps({"nlToken": self.token, "nlConnectToken": self.token.split(".")[1],
                                    "nlPort": self.port}))


async def _connect(tmp_path, neutralino):
    credential = tmp_path / "auth_info.json"
    neutralino.credential(credential)
    owned = SimpleNamespace(process=SimpleNamespace(returncode=None))
    async with aiohttp.ClientSession() as session:
        shell = supervisor.Shell(session)
        try:
            await shell.connect(credential, time.monotonic() + 5, owned)
            await shell.call("window.show")
        finally:
            if shell.ws is not None:
                await shell.ws.close()
    return credential


def test_the_supervisor_takes_the_key_first_then_connects_with_it(tmp_path):
    async def scenario():
        neutralino = await FakeNeutralino().start()
        try:
            credential = await _connect(tmp_path, neutralino)
        finally:
            await neutralino.runner.cleanup()
        return neutralino, credential
    neutralino, credential = asyncio.run(scenario())
    assert not credential.exists()                                       # read once, deleted (unchanged)
    port = neutralino.port
    # The globals first (to its own address), then the native connection (no
    # Origin) and the calls with the exported key; nobody else can now have it.
    assert neutralino.seen == [("globals", f"127.0.0.1:{port}"), ("ws", "connect", None),
                               ("call", "window.show", "secret.connect")]
    assert neutralino.given


@pytest.mark.parametrize("case", ["taken", "directory", "embedded", "refused", "unreachable"])
def test_a_key_not_taken_or_a_shell_off_its_bundle_fails_the_start_closed(tmp_path, case, caplog):
    async def scenario():
        neutralino = await FakeNeutralino(mode={"directory": "directory", "embedded": "embedded"}.get(case, "bundle"),
                                          status=404 if case == "refused" else 200).start()
        neutralino.given = case == "taken"                              # another program asked first
        try:
            if case == "unreachable":
                await neutralino.runner.cleanup()
            with pytest.raises(PermissionError):
                await _connect(tmp_path, neutralino)
        finally:
            await neutralino.runner.cleanup()
        return neutralino
    with caplog.at_level("ERROR", logger=supervisor.logger.name):
        neutralino = asyncio.run(scenario())
    assert not any(seen[0] in ("ws", "call") for seen in neutralino.seen)   # no connection with a leaked key
    expected = {"taken": "the native shell's access key was taken by another program first; the shell is ended",
                "directory": "the native shell serves its directory, not its resource bundle; the shell is ended",
                "embedded": "the native shell serves its embedded, not its resource bundle; the shell is ended",
                "refused": "the native shell's access key was not taken; the shell is ended",
                "unreachable": "the native shell's access key was not taken; the shell is ended"}[case]
    assert expected in caplog.text


def test_a_relaunched_window_is_shown_only_after_its_key_is_taken_and_a_leak_ends_its_shell(tmp_path, monkeypatch):
    """The relaunch path (shell started hidden, then shown): the globals are
    taken inside the spawn, before window.show; when they cannot be, the
    shell that started is ended and the window stays released."""
    ended = []

    async def end_shell(shell, owned, processes):
        ended.append(shell.generation)
    monkeypatch.setattr(supervisor, "_end_shell", end_shell)
    monkeypatch.setattr(supervisor, "clear_cookie_store", lambda runtime: None)
    monkeypatch.setattr(supervisor, "shell_data_home", lambda: tmp_path / "data-home")
    (tmp_path / "data-home" / supervisor.APP_ID).mkdir(parents=True)
    shell_dir = tmp_path / "shell"
    fake_shell_bundle(shell_dir)
    credential = tmp_path / "data-home" / supervisor.APP_ID / ".tmp" / "auth_info.json"

    async def scenario(leaked: bool):
        neutralino = await FakeNeutralino().start()
        neutralino.given = leaked

        class Owner:
            def alive(self, _identity):
                return False

            def forget(self, _identity):
                pass

            async def spawn(self, identity, argv, env=None, **_):
                neutralino.argv = argv
                neutralino.credential(credential)                       # the shell exports it
                return SimpleNamespace(process=SimpleNamespace(stdout=None, stderr=None, returncode=None))

        try:
            async with aiohttp.ClientSession() as session:
                window = supervisor.Window(session=session, processes=Owner(), paths={"shell": shell_dir},
                                           root=tmp_path, port=1)
                window.released, window.hidden = True, True
                reopened = await window.relaunch()
                if window.shell is not None and window.shell.ws is not None:
                    await window.shell.ws.close()
        finally:
            await neutralino.runner.cleanup()
        return neutralino, window, reopened

    neutralino, window, reopened = asyncio.run(scenario(leaked=False))
    assert reopened and "--res-mode=bundle" in neutralino.argv and "--window-hidden=true" in neutralino.argv
    assert [seen[0] for seen in neutralino.seen] == ["globals", "ws", "call"]
    assert neutralino.seen[-1][1] == "window.show" and ended == []

    neutralino, window, reopened = asyncio.run(scenario(leaked=True))
    assert not reopened and window.released and window.shell is None and ended == [1]
    assert [seen[0] for seen in neutralino.seen] == ["globals"]          # never connected, never shown


# ── the patch and its recipes ──────────────────────────────────────────────

def test_the_server_patch_is_pinned_and_applied_by_both_source_builds():
    digest = hashlib.sha256((NEUTRALINO / PATCH).read_bytes()).hexdigest()
    source = (NEUTRALINO / "source.sh").read_text(encoding="utf-8")
    assert f"{digest}  $here/{PATCH}" in source and f'"$here/{PATCH}"' in source    # checked, then handed over
    assert f"then \\`{PATCH}\\` applied" in source                                     # BUILD.md, both systems
    build = (NEUTRALINO / "build-macos.sh").read_text(encoding="utf-8")
    assert f"SERVER_PATCH={PATCH}\nSERVER_PATCH_SHA256={digest}\n" in build
    assert 'echo "$SERVER_PATCH_SHA256  $here/$SERVER_PATCH" | shasum -a 256 -c -' in build
    assert '"$SERVER_PATCH": "$SERVER_PATCH_SHA256"' in build                         # in its provenance
    manifest = yaml.safe_load((ROOT / "packaging/desktop/flatpak/net.downloadpulse.desktop.yml").read_text(encoding="utf-8"))
    neutralino = next(module for module in manifest["modules"] if module["name"] == "neutralino")
    assert {"type": "patch", "path": f"../neutralino/{PATCH}", "options": ["-p1"]} in neutralino["sources"]
    assert {"type": "file", "path": f"../neutralino/{PATCH}"} in neutralino["sources"]
    assert f'"{PATCH}": "%s"' in neutralino["build-commands"][-1]
    workflow = (ROOT / ".github/workflows/desktop-artifacts.yml").read_text(encoding="utf-8")
    assert f'"$src/neutralino/{PATCH}"' in workflow                                  # required in the handover


def test_the_server_patch_keeps_to_the_document_root_its_own_host_and_no_web_page():
    patch = (NEUTRALINO / PATCH).read_text(encoding="utf-8")
    assert patch.count("+++ b/") == 2 and "+++ b/server/router.cpp" in patch and "+++ b/server/neuserver.cpp" in patch
    added = "\n".join(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
    # (a) the decoded path, inside the document root, with no ".." segment or backslash
    assert 'string root = neuserver::getDocumentRoot() + "/";' in added
    assert 'inside = inside && segment != "..";' in added and "path.find('\\\\') == string::npos" in added
    assert "websocketpp::http::status_code::forbidden" in added
    # (b) plain HTTP: Host 127.0.0.1:<port> or localhost:<port> only
    assert 'host != "127.0.0.1:" + port && host != "localhost:" + port' in added
    # (c) the native WebSocket: refused when an Origin is sent (every web page sends one)
    assert 'if(!con->get_request_header("Origin").empty()) {\n        return false;' in added


# ── the configuration ──────────────────────────────────────────────────────

def test_the_shell_configuration_agrees_with_its_bundle_and_its_key_handling():
    files = {"/" + name for name in supervisor.SHELL_BUNDLE_FILES}
    assert CONFIG["documentRoot"] == "/resources/" and CONFIG["cli"]["resourcesPath"] == "/resources/"
    assert CONFIG["modes"]["window"]["icon"] in files                    # the window's icon, from the bundle
    for platform in ("win32", "darwin", "linux"):
        assert supervisor.tray_menu(None, lambda key, **_: key, platform)["icon"] in files
    assert "/neutralino.config.json" in files
    # The key: handed once, exported to the supervisor, never put into the page.
    window = CONFIG["modes"]["window"]
    assert CONFIG["tokenSecurity"] == "one-time" and CONFIG["exportAuthInfo"] is True
    assert window["injectGlobals"] is False and window["injectClientLibrary"] is False
    assert CONFIG["enableServer"] is True and CONFIG["port"] == 0 and "serverHeaders" not in CONFIG
    assert "singlePageServe" not in window and "mounts" not in CONFIG
    assert not any(method.startswith(("server.", "resources.", "filesystem.")) for method in CONFIG["nativeAllowList"])
    # The assembler builds the bundle on every platform's layout, after the icons.
    assemble = (ROOT / "packaging/desktop/assemble.py").read_text(encoding="utf-8")
    assert assemble.index('icons(out / "desktop" / "shell")') < assemble.index('shell_bundle(out / "desktop" / "shell")')
