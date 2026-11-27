"""WS6: the hosted desktop installer pipeline's contract, read from the
workflow, the installer script and the recipes as committed.

* The Windows installer requires the Microsoft Visual C++ x64 Redistributable
  14.51+ before it changes anything. It carries Microsoft's own installer of it,
  signature-checked and unmodified, and runs it only interactively, with its
  own interface (Microsoft's licence terms); a silent install refuses instead.
* The handover publishes each platform that passed every build and smoke
  check on its own, as soon as that platform's build is done (not after the
  whole matrix), and the complete set only when all three passed in the run,
  only for the one commit an explicit approval names.
* RARLAB's RAR package is never cached or handed over as source.
* The checksum file covers the manifest and every delivered file, not itself.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "desktop-artifacts.yml").read_text(encoding="utf-8"))
JOBS = WORKFLOW["jobs"]
NSIS = (ROOT / "packaging" / "desktop" / "windows" / "installer.nsi").read_text(encoding="utf-8")


def _steps(job: str) -> list[dict]:
    return JOBS[job]["steps"]


def _run(job: str) -> str:
    return "\n".join(step.get("run", "") for step in _steps(job))


def test_the_installer_requires_the_redistributable_first_and_runs_microsofts_installer_with_its_terms():
    assert re.search(r"(?m)^\s*!define VCREDIST_MIN_MAJOR 14$", NSIS)
    assert re.search(r"(?m)^\s*!define VCREDIST_MIN_MINOR 51\b", NSIS)
    on_init = NSIS[NSIS.index("Function .onInit"):NSIS.index("FunctionEnd", NSIS.index("Function .onInit"))]
    assert on_init.index("Call RequireVcRedist") < on_init.index("Call TakeDataRootLock")
    require = NSIS[NSIS.index("Function RequireVcRedist"):NSIS.index("FunctionEnd", NSIS.index("Function RequireVcRedist"))]
    silent = require[require.index("${If} ${Silent}"):require.index("MessageBox MB_ICONINFORMATION|MB_OKCANCEL")]
    assert silent.index("SetErrorLevel 3") < silent.index("Quit")             # silent: report, exit 3
    assert "vc_redist" not in silent                                          # never run without its terms shown
    interactive = require[require.index("vcInstall:"):]
    # Microsoft's own installer, carried; started through the shell (it needs
    # administrator rights), never quietly: its interface shows its terms.
    assert 'File /oname=$PLUGINSDIR\\vc_redist.x64.exe "${STAGE}\\prerequisites\\vc_redist.x64.exe"' in interactive
    assert 'ExecShellWait "open" "$PLUGINSDIR\\vc_redist.x64.exe" "/install /norestart"' in interactive
    assert "/quiet" not in require and "/passive" not in require
    assert interactive.index("ExecShellWait") < interactive.index("Call VcRedistSatisfied")   # checked again after
    assert "IDRETRY vcInstall" in interactive
    assert "MicrosoftEdgeWebview2Setup.exe" in NSIS                            # WebView2 path kept
    builder = (ROOT / "packaging" / "desktop" / "windows" / "build-installer.sh").read_text(encoding="utf-8")
    assert re.search(r'(?m)^cp "\$prereq/MicrosoftEdgeWebview2Setup\.exe" "\$prereq/vc_redist\.x64\.exe" "\$stage/prerequisites/"$', builder)


def test_the_build_carries_only_microsofts_signed_unmodified_redistributable():
    build = _windows_step("The Windows installer, on this runner")["run"]
    assert "curl -sSfL -o \"$RUNNER_TEMP/prerequisites/vc_redist.x64.exe\" https://aka.ms/vc14/vc_redist.x64.exe" in build
    assert 'Get-AuthenticodeSignature' in build and '$s.Status -ne "Valid"' in build
    assert '-notmatch "O=Microsoft Corporation"' in build and '[version]"14.51"' in build
    assert build.index("Get-AuthenticodeSignature") < build.index("bash packaging/desktop/windows/build-installer.sh")
    # The WebView2 bootstrapper is held to the same signature check before it is embedded (1.0.8 D-5).
    assert 'foreach ($name in "MicrosoftEdgeWebview2Setup.exe", "vc_redist.x64.exe")' in build
    assert build.index('MicrosoftEdgeWebview2Setup.exe" https://go.microsoft.com/') < build.index("Get-AuthenticodeSignature")
    licences = (ROOT / "docs" / "DEPENDENCY_LICENSES.md").read_text(encoding="utf-8")
    assert "**Bundled, unmodified, as Distributable Code.**" in licences and "not covered by DownloadPulse's GPL" in licences


def _windows_step(prefix: str) -> dict:
    return next(step for step in _steps("desktop") if step.get("name", "").startswith(prefix))


def test_windows_builds_its_installer_with_the_pinned_nsis_and_install_tests_that_exe_on_the_same_runner():
    pins = (ROOT / "packaging" / "desktop" / "windows" / "official.sha256").read_text(encoding="utf-8")
    assert re.search(r"(?m)^c7d27f780ddb6cffb4730138cd1591e841f4b7edb155856901cdf5f214394fa1  nsis-3\.11\.zip  "
                     r"https://downloads\.sourceforge\.net/project/nsis/NSIS%203/3\.11/nsis-3\.11\.zip$", pins)
    build = _windows_step("The Windows installer, on this runner")
    assert build["if"] == "runner.os == 'Windows'"
    assert "sha256sum -c -" in build["run"] and 'export MAKENSIS="$RUNNER_TEMP/nsis/nsis-3.11/makensis.exe"' in build["run"]
    assert 'packaging/desktop/windows/build-installer.sh "$RUNNER_TEMP/layout"' in build["run"]
    assert "TOOLCHAIN.txt" in build["run"]
    install = _windows_step("The same EXE installed silently")
    names = [step.get("name", "") for step in _steps("desktop")]
    assert names.index(build["name"]) < names.index(install["name"])           # one job, one runner
    run = install["run"]
    assert "desktop-build\\windows\\DownloadPulse-*-windows-x64.exe" in run       # the EXE built just above
    assert "https://aka.ms/vc14/vc_redist.x64.exe" in run and "$env:RUNNER_TEMP\\vc_redist.x64.exe" in run
    assert run.index("$refused.ExitCode -ne 3") < run.index("Invoke-WebRequest") < run.index(
        "$install = Start-Process -FilePath $exe")
    assert "$failed += 'uninstall'" in run and "$failed += 'Usenet check'" in run
    for job, body in JOBS.items():                                             # nowhere else, never kept
        assert "vc_redist" not in yaml.safe_dump(body) or job == "desktop", job
    builder = (ROOT / "packaging" / "desktop" / "windows" / "build-installer.sh").read_text(encoding="utf-8")
    assert 'if [ -n "${MAKENSIS:-}" ]; then' in builder


def test_caches_carry_only_shareable_inputs_and_no_finished_bytes():
    saves = [(job, step) for job, body in JOBS.items() for step in body["steps"]
             if "actions/cache/save" in step.get("uses", "")]
    assert [job for job, _ in saves] == ["inputs"]
    for job, body in JOBS.items():
        for step in body["steps"]:
            if "actions/cache" in step.get("uses", ""):
                assert "desktop-build" not in step["with"]["path"] and "desktop-build" not in step["with"]["key"], job


APPROVED = "vars.DESKTOP_INSTALLERS_UPLOAD_SHA == github.sha"


def test_the_handover_publishes_each_passed_platform_and_the_complete_set_only_for_an_approved_commit():
    handover = JOBS["handover"]
    # Each set waits for only its own builds: it needs the shared inputs, not the desktop matrix.
    assert handover["needs"] == "inputs"
    assert handover["if"] == f"always() && needs.inputs.result == 'success' && {APPROVED}"
    assert handover["strategy"] == {"fail-fast": False, "matrix": "${{ fromJSON(needs.inputs.outputs.sets) }}"}
    assert handover["permissions"] == {"contents": "read", "actions": "read"}
    wait = _steps("handover")[0]
    assert wait["env"]["BUILDS"] == "${{ matrix.builds }}"
    assert "actions/runs/$GITHUB_RUN_ID/jobs?filter=latest" in wait["run"]
    assert '*"|completed|success") ;;' in wait["run"] and "exit 1" in wait["run"]
    plan = next(step for step in _steps("inputs") if step.get("id") == "plan")["run"]
    for entry in ('{"set":"macos","builds":"macos-arm64"}', '{"set":"windows","builds":"windows-x64"}',
                  '{"set":"linux","builds":"linux-x64"}'):
        assert entry in plan
    assert ('[ "$platforms" != "macos windows linux" ] || '
            'sets+=(\'{"set":"complete","builds":"macos-arm64 windows-x64 linux-x64"}\')') in plan
    uploads = [(job, step) for job, body in JOBS.items() for step in body["steps"]
               if "actions/upload-artifact" in step.get("uses", "")]
    assert [(job, step["with"]["retention-days"]) for job, step in uploads] == [("desktop", 1)] + [("handover", 14)] * 4
    intermediate = uploads[0][1]
    assert intermediate["if"] == f"success() && {APPROVED}"         # only a platform that passed every step
    assert intermediate["with"]["name"] == "desktop-intermediate-${{ matrix.name }}-${{ github.sha }}"
    published = {step["with"]["name"]: step["if"] for _, step in uploads[1:]}
    assert published == {
        "desktop-installers-${{ github.sha }}-macos":
            f"matrix.set == 'macos' && steps.passed.outputs.macos == 'true' && {APPROVED}",
        "desktop-installers-${{ github.sha }}-windows":
            f"matrix.set == 'windows' && steps.passed.outputs.windows == 'true' && {APPROVED}",
        "desktop-installers-${{ github.sha }}-linux":
            f"matrix.set == 'linux' && steps.passed.outputs.linux == 'true' && {APPROVED}",
        "desktop-installers-${{ github.sha }}":
            f"matrix.set == 'complete' && steps.passed.outputs.complete == 'true' && {APPROVED}"}
    passed = next(step for step in _steps("handover") if step.get("id") == "passed")["run"]
    assert 'if [ -f "desktop-build/$os/COMMIT" ]' in passed
    assert '[ "$passed" != "macos windows linux" ] || echo "complete=true"' in passed
    download = next(step for step in _steps("handover") if "actions/download-artifact" in step.get("uses", ""))
    assert download["with"]["pattern"] == (
        "desktop-intermediate-${{ matrix.set == 'complete' && '*' || matrix.builds }}-${{ github.sha }}")
    cleanup = JOBS["intermediates"]
    assert set(cleanup["needs"]) == {"desktop", "handover"} and cleanup["if"] == f"always() && {APPROVED}"
    assert cleanup["permissions"] == {"actions": "write"} and WORKFLOW["permissions"] == {"contents": "read"}
    assert 'startswith("desktop-intermediate-")' in _run("intermediates")
    assert "DESKTOP_INSTALLERS_UPLOAD:" not in yaml.safe_dump(WORKFLOW)
    handover_run = _run("handover")
    assert '[ "$(cat "$dir/COMMIT")" = "$GITHUB_SHA" ]' in handover_run
    assert '[ "$(cat "$dir/RUN")" = "$GITHUB_RUN_ID" ]' in handover_run


def _plan(variable: str, sha: str, smoke_passed: dict[str, bool]) -> dict:
    """What runs, by GitHub's documented if-semantics, for the exact conditions asserted above: a
    step's success() holds only when every earlier step of its job passed; a job's success() only when
    every job it needs passed; always() regardless."""
    approved = variable == sha
    passed = sorted(name for name, ok in smoke_passed.items() if ok and approved)
    return {"intermediate artifacts": passed,
            "published": passed + (["complete set"] if approved and all(smoke_passed.values()) else []),
            "intermediates deleted at the end": approved}


def test_unset_mismatched_and_matching_approvals_with_and_without_a_failed_smoke_check():
    sha = "55d9fbceb5fc95341a5ce61c3425f76cab6d0845"
    passing = {"macos-arm64": True, "windows-x64": True, "linux-x64": True}
    failing = {**passing, "windows-x64": False}
    nothing = {"intermediate artifacts": [], "published": [], "intermediates deleted at the end": False}
    assert _plan("", sha, passing) == nothing                                  # unset
    assert _plan("0" * 40, sha, passing) == nothing                            # another commit
    assert _plan(sha[:12], sha, passing) == nothing                            # not the FULL sha
    assert _plan(sha, sha, passing) == {"intermediate artifacts": ["linux-x64", "macos-arm64", "windows-x64"],
                                        "published": ["linux-x64", "macos-arm64", "windows-x64", "complete set"],
                                        "intermediates deleted at the end": True}
    # One platform failing (or never given a runner) holds nothing else back: the others publish.
    assert _plan(sha, sha, failing) == {"intermediate artifacts": ["linux-x64", "macos-arm64"],
                                        "published": ["linux-x64", "macos-arm64"], "intermediates deleted at the end": True}


def test_the_rar_package_is_never_cached_or_handed_over_as_source():
    prepare = (ROOT / "packaging" / "macos-tools" / "prepare-sources.sh").read_text(encoding="utf-8")
    assert 'case "$name" in rar*) continue ;; esac' in prepare
    build = (ROOT / "packaging" / "macos-tools" / "build-macos.sh").read_text(encoding="utf-8")
    assert re.search(r'rar\*\) curl -sSfL -o "\$work/\$name" "\$url"', build)
    assert 'tar -xzf "$work/rarmacos-arm-723.tar.gz"' in build
    handover = _run("handover")
    assert "cp desktop-inputs/macos-sources/*" not in handover                  # a source-only selection
    assert 'compgen -G "$src/macos-and-flatpak-tools/rar*"' in handover
    assert "packaging/desktop/python-sources.py" in handover


def test_the_inputs_cache_and_the_checksums_cover_the_manifest_and_every_delivered_file():
    inputs = [step for step in _steps("inputs") if "actions/cache/save" in step.get("uses", "")]
    assert len(inputs) == 1
    assert inputs[0]["with"]["path"].split() == ["desktop-inputs/icons", "desktop-inputs/macos-sources",
                                                 "desktop-inputs/wheels", "desktop-inputs/windows-tools",
                                                 "desktop-inputs/sabctools-source"]
    step = next(s for s in _steps("handover") if s.get("name", "").startswith("The manifest, then SHA256SUMS"))
    run = step["run"]
    assert "! -name SHA256SUMS ! -name MANIFEST.md" in run
    assert run.index("> MANIFEST.md") < run.index("> SHA256SUMS")
    assert "sha256sum MANIFEST.md; } > SHA256SUMS" in run and "sha256sum -c SHA256SUMS" in run


def test_every_platform_job_checks_out_the_committed_bytes_unconverted():
    steps = _steps("desktop")
    names = [step.get("uses", step.get("name", "")) for step in steps]
    autocrlf = next(i for i, step in enumerate(steps) if step.get("run") == "git config --global core.autocrlf false")
    checkout = next(i for i, name in enumerate(names) if name.startswith("actions/checkout@"))
    assert autocrlf < checkout


def test_the_handover_checks_each_installer_against_its_builders_line_in_either_sha256sum_form(tmp_path):
    """The selection line exactly as committed, run with bash: macOS and Linux write "<hash>  name";
    Git Bash on the Windows runner writes "<hash> *name". Both are accepted, a wrong digest is not, and
    no other file's line can stand in for the installer's."""
    import hashlib
    import shutil
    import subprocess
    if not (shutil.which("bash") and shutil.which("sha256sum")):
        import pytest
        pytest.skip("needs bash and GNU sha256sum")
    step = next(s for s in _steps("handover") if s.get("name", "").startswith("The installers -- exactly one of each"))
    line = next(row.strip() for row in step["run"].splitlines() if "sha256sum -c -" in row)
    expected = "DownloadPulse-1.0.14-windows-x64.exe"
    payload = b"installer bytes"
    digest = hashlib.sha256(payload).hexdigest()
    other = hashlib.sha256(b"other").hexdigest()

    def selected(sums: str) -> int:
        directory = tmp_path / str(len(list(tmp_path.iterdir())))
        directory.mkdir()
        (directory / expected).write_bytes(payload)
        (directory / f"x{expected}").write_bytes(b"other")
        (directory / "SHA256SUMS").write_text(sums, encoding="utf-8")
        script = f'set -e; os=windows; expected={expected}; dir="{directory}"; {line}'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True).returncode

    assert selected(f"{digest}  {expected}\n") == 0                         # text form (macOS, Linux)
    assert selected(f"{other} *x{expected}\n{digest} *{expected}\n") == 0  # binary form (Git Bash)
    assert selected(f"{other} *{expected}\n") != 0                          # wrong digest
    assert selected(f"{other}  x{expected}\n") != 0                         # only another file's line


# ── macOS: the DMG's drag-to-install window ────────────────────────────────

def _dmg_settings() -> dict:
    """dmg-settings.py as dmgbuild executes it, with dmgbuild's ``defines``."""
    values = {"defines": {"app": "/build/DownloadPulse.app", "background": "/build/dmg-background.tiff"}}
    exec((ROOT / "packaging/desktop/macos/dmg-settings.py").read_text(encoding="utf-8"), values)
    return values


def test_the_dmg_window_puts_the_application_beside_an_applications_link_on_its_background():
    import sys
    sys.path.insert(0, str(ROOT / "packaging" / "desktop"))
    import assemble
    settings = _dmg_settings()
    assert settings["files"] == ["/build/DownloadPulse.app"]
    assert settings["symlinks"] == {"Applications": "/Applications"}
    assert settings["background"] == "/build/dmg-background.tiff"
    assert settings["icon"] == "/build/DownloadPulse.app/Contents/Resources/DownloadPulse.icns"
    assert settings["format"] == "UDZO" and settings["volume_name"] == "DownloadPulse"
    # Each icon in the slot the derived background draws for it, in a window of its size.
    assert settings["icon_locations"] == {"DownloadPulse.app": assemble.DMG_APP, "Applications": assemble.DMG_APPLICATIONS}
    assert settings["window_rect"][1] == assemble.DMG_WINDOW
    assert settings["default_view"] == "icon-view" and settings["icon_size"] == 128
    assert not any(settings[key] for key in ("show_toolbar", "show_sidebar", "show_status_bar", "show_pathbar",
                                             "show_tab_view"))
    # No Finder flags on the application: it must still verify as signed, --strict.
    assert not any(key in settings for key in ("hide_extension", "hide_extensions", "hide"))
    svg = assemble.dmg_background_svg()
    width, height = assemble.DMG_WINDOW
    assert f'width="{width}" height="{height}"' in svg and svg.count("<rect") == 3   # the field and two slots
    assert 'stroke="url(#mark)"' in svg and "#b42cff" in svg and "<text" not in svg   # the favicon's gradient, no words


def test_the_dmg_is_built_by_dmgbuild_from_a_hash_locked_build_only_lock():
    import re
    lock = (ROOT / "packaging/desktop/macos/dmg-requirements.txt").read_text(encoding="utf-8")
    pins = dict(re.findall(r"(?m)^([A-Za-z0-9_.-]+)==(\S+) \\", lock))
    assert pins == {"dmgbuild": "1.6.7", "ds-store": "1.3.3", "mac-alias": "2.2.3"}
    blocks = re.split(r"(?m)^(?=[A-Za-z0-9_.-]+==)", lock)[1:]
    assert all(block.count("--hash=sha256:") >= 1 for block in blocks)
    script = (ROOT / "packaging/desktop/macos/build-app.sh").read_text(encoding="utf-8")
    assert '"${DMGBUILD:-dmgbuild}" -s "$here/dmg-settings.py"' in script and "hdiutil create" not in script
    # The background is the DMG's: one 1x+2x TIFF, and not left inside the application.
    assert 'tiffutil -cathidpicheck "$icons/dmg-background.png" "$icons/dmg-background@2x.png"' in script
    assert 'rm -f "$icons/dmg-background.png" "$icons/dmg-background@2x.png"' in script
    workflow = (ROOT / ".github" / "workflows" / "desktop-artifacts.yml").read_text(encoding="utf-8")
    assert "--require-hashes --no-deps \\\n            -r packaging/desktop/macos/dmg-requirements.txt" in workflow
    assert 'DMGBUILD="$RUNNER_TEMP/dmg-tools/bin/dmgbuild" bash packaging/desktop/macos/build-app.sh' in workflow
    assert '[ "$(readlink "$mnt/Applications")" = /Applications ]' in workflow
    assert 'assert places == {"DownloadPulse.app": (170, 190), "Applications": (490, 190)}' in workflow


# ── The native shell: Neutralino 6.10.0 from its pinned source, patched ────

NEUTRALINO_COMMIT = "4361b63eb1157fc7dedb29b92815a08ec0002767"


def test_the_flatpak_builds_the_shell_from_the_pinned_commit_with_the_linux_patch():
    manifest = yaml.safe_load((ROOT / "packaging/desktop/flatpak/net.downloadpulse.desktop.yml").read_text(encoding="utf-8"))
    modules = {module["name"]: module for module in manifest["modules"]}
    assert next(iter(modules)) == "neutralino"                 # built first, before the tools and the application
    sources = modules["neutralino"]["sources"]
    assert sources[0] == {"type": "git", "url": "https://github.com/neutralinojs/neutralinojs.git",
                          "tag": "v6.10.0", "commit": NEUTRALINO_COMMIT}
    # 1.0.5: a second Linux patch (the run-scoped cookie store), then a third (the view's
    # background in the interface's mode), each applied after the one before.
    assert sources[1] == {"type": "patch", "path": "../neutralino/linux-gtk-main-thread.patch", "options": ["-p1"]}
    assert sources[2] == {"type": "patch", "path": "../neutralino/linux-cookie-store.patch", "options": ["-p1"]}
    assert sources[3] == {"type": "patch", "path": "../neutralino/linux-background.patch", "options": ["-p1"]}
    # 1.0.8: the loopback server's boundary, shared with macOS, applied last.
    assert sources[4] == {"type": "patch", "path": "../neutralino/shell-server-boundary.patch", "options": ["-p1"]}
    assert sources[5] == {"type": "file", "path": "../neutralino/linux-gtk-main-thread.patch"}
    assert sources[6] == {"type": "file", "path": "../neutralino/linux-cookie-store.patch"}
    assert sources[7] == {"type": "file", "path": "../neutralino/linux-background.patch"}
    assert sources[8] == {"type": "file", "path": "../neutralino/shell-server-boundary.patch"}
    # Its provenance (build-macos.sh's schema), recorded in the installed BUILD-INFO.json
    # only once the installed executable is the binary it names.
    provenance = modules["neutralino"]["build-commands"][-1]
    assert f'"commit": "{NEUTRALINO_COMMIT}"' in provenance and "sha256sum linux-gtk-main-thread.patch" in provenance
    assert "sha256sum linux-cookie-store.patch" in provenance and "sha256sum linux-background.patch" in provenance
    assert "sha256sum shell-server-boundary.patch" in provenance
    assert "sha256sum /app/lib/neutralino/downloadpulse-shell" in provenance
    record = next(c for c in modules["downloadpulse"]["build-commands"] if "BUILD-INFO.json" in c)
    commands = modules["downloadpulse"]["build-commands"]
    assert commands.index(record) == commands.index(
        "install -m755 /app/lib/neutralino/downloadpulse-shell /app/lib/downloadpulse/desktop/shell/downloadpulse-shell") + 1
    assert 'installed == shell["binary_sha256"] or sys.exit(' in record and 'info["shell"] = shell' in record
    script = (ROOT / "packaging/desktop/flatpak/ci-build.sh").read_text(encoding="utf-8")
    assert "zstd git appstream >/dev/null" in script           # flatpak-builder's git source; appstreamcli
    assert 'echo "shell: $shell (/app/lib/downloadpulse/desktop/shell/downloadpulse-shell)"' in script
    assert '[ "${recorded%% *}" = "$shell" ] || { echo "BUILD-INFO.json names another shell' in script


def test_each_patch_is_the_one_its_recipes_pin_and_each_recipe_verifies_the_commit():
    import hashlib
    here = ROOT / "packaging/desktop/neutralino"
    digests = {name: hashlib.sha256((here / name).read_bytes()).hexdigest()
               for name in ("linux-gtk-main-thread.patch", "linux-cookie-store.patch", "linux-background.patch",
                            "macos-main-thread-shutdown.patch",
                            "macos-frame-theme.patch", "shell-server-boundary.patch")}
    build, source = (here / "build-macos.sh").read_text(encoding="utf-8"), (here / "source.sh").read_text(encoding="utf-8")
    assert f"PATCH_SHA256={digests['macos-main-thread-shutdown.patch']}" in build
    assert f"FRAME_PATCH_SHA256={digests['macos-frame-theme.patch']}" in build
    assert f"SERVER_PATCH_SHA256={digests['shell-server-boundary.patch']}" in build
    assert build.index('git -C "$work/src" apply "$here/$PATCH"') < build.index('git -C "$work/src" apply "$here/$FRAME_PATCH"') \
        < build.index('git -C "$work/src" apply "$here/$SERVER_PATCH"')
    for name, digest in digests.items():
        assert f"{digest}  $here/{name}" in source
    for script in (build, source):
        assert f"COMMIT={NEUTRALINO_COMMIT}" in script and '[ "$(git -C "$work/src" rev-parse HEAD)" = "$COMMIT" ]' in script
    # Each patch keeps to its own system: reviewable apart.
    linux, cookies, background, macos, frame, _server = ((here / name).read_text(encoding="utf-8") for name in digests)
    assert "dispatch_sync" not in linux and "pthread_main_np" in macos and "g_main_context_is_owner" not in macos
    # The cookie store: Linux-only (webview.h's GTK path), named by the supervisor, nothing persistent unless named.
    assert 'std::getenv("DOWNLOADPULSE_COOKIE_STORE")' in cookies and "webkit_cookie_manager_set_persistent_storage" in cookies
    assert "dispatch_sync" not in cookies and "+++ b/lib/webview/webview.h" in cookies
    # The background (1.0.5): Linux-only, named by the supervisor, the GTK theme's when unset.
    assert 'std::getenv("DOWNLOADPULSE_BACKGROUND")' in background and "gdk_rgba_parse" in background
    assert "+++ b/lib/webview/webview.h" in background and "dispatch_sync" not in background
    # The frame patch: one window method, macOS-only behaviour, registered with the router.
    assert '{"window.setFrameTheme", window::controllers::setFrameTheme},' in frame
    assert "setTitlebarAppearsTransparent:" in frame and "#if defined(__APPLE__)" in frame


def test_the_macos_shell_is_built_and_installed_with_its_provenance_and_the_handover_carries_its_source():
    workflow = (ROOT / ".github/workflows/desktop-artifacts.yml").read_text(encoding="utf-8")
    assert 'run: bash packaging/desktop/neutralino/build-macos.sh "$RUNNER_TEMP/shell"' in workflow
    assert 'if [ "$RUNNER_OS" != "Linux" ]; then extra+=(--shell "$RUNNER_TEMP/shell"); fi' in workflow
    assert 'bash packaging/desktop/neutralino/source.sh "$src/neutralino"' in workflow
    assemble = (ROOT / "packaging/desktop/assemble.py").read_text(encoding="utf-8")
    assert 'if hashlib.sha256(target.read_bytes()).hexdigest() != shell.get("binary_sha256"):' in assemble
    assert '"shell": shell,' in assemble


# ── 1.0.8 WS-A1b: Windows builds its shell from the same source, with the server patch ──

WINDOWS_SHELL = ROOT / "packaging/desktop/neutralino/build-windows.ps1"


def _assemble():
    import importlib.util
    spec = importlib.util.spec_from_file_location("assemble_ws_a1b", ROOT / "packaging/desktop/assemble.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _desktop_step(name_start: str) -> dict:
    return next(step for step in _steps("desktop") if step.get("name", "").startswith(name_start))


def test_the_windows_job_builds_the_shell_from_the_verified_commit_with_the_checked_server_patch():
    import hashlib
    script = WINDOWS_SHELL.read_text(encoding="utf-8")
    patch = ROOT / "packaging/desktop/neutralino/shell-server-boundary.patch"
    # The tag's tree, verified to be the pinned commit, before anything is applied.
    assert f"$Commit = '{NEUTRALINO_COMMIT}'" in script and "$Tag = 'v6.10.0'" in script
    assert ("git -c core.autocrlf=false clone -q --depth 1 --branch $Tag "
            "https://github.com/neutralinojs/neutralinojs.git $src") in script
    assert 'if ($head -ne $Commit) { throw' in script
    # The patch's digest checked, then that one patch applied: the Linux and macOS ones never.
    assert f"$ServerPatchSha256 = '{hashlib.sha256(patch.read_bytes()).hexdigest()}'" in script
    assert script.index("if ($digest -ne $ServerPatchSha256) { throw") < script.index("git -C $src apply $patch")
    assert script.count("git -C $src apply") == 1
    assert not re.search(r"(linux|macos)-[a-z-]+\.patch", script)
    # MSVC from the runner's own Visual Studio (vswhere, vcvars64.bat), Neutralino's own Release build.
    assert "Microsoft Visual Studio\\Installer\\vswhere.exe" in script and "VC\\Auxiliary\\Build\\vcvars64.bat" in script
    assert "Microsoft.VisualStudio.Component.VC.Tools.x86.x64" in script
    assert "cmake -S $src -B $build -G Ninja '-DCMAKE_BUILD_TYPE=Release'" in script and "ninja -C $build" in script
    assert "$binary = Join-Path $src 'bin\\neutralino-win_x64.exe'" in script
    # Every native step's exit is checked; a failure stops the build (no fallback).
    assert "$ErrorActionPreference = 'Stop'" in script
    for what in ("git clone of Neutralino $Tag", "git apply $ServerPatch", "vcvars64.bat", "cmake (configure)",
                 "ninja (build)"):
        assert f"Assert-Exit \"{what}\"" in script or f"Assert-Exit '{what}'" in script, what
    # The workflow: that script on the Windows runner, before the layout is assembled, which takes it.
    step = _desktop_step("The Windows native shell")
    assert step["if"] == "runner.os == 'Windows'" and step["shell"] == "pwsh"
    assert ('& "$env:GITHUB_WORKSPACE\\packaging\\desktop\\neutralino\\build-windows.ps1" "$env:RUNNER_TEMP\\shell"'
            in step["run"])
    names = [s.get("name", "") for s in _steps("desktop")]
    assert names.index(step["name"]) < next(i for i, n in enumerate(names) if n.startswith("Assemble the desktop layout"))
    assert 'if [ "$RUNNER_OS" != "Linux" ]; then extra+=(--shell "$RUNNER_TEMP/shell"); fi' in \
        _desktop_step("Assemble the desktop layout")["run"]
    # No third-party action sets the compiler up: every action stays SHA-pinned, none is an MSVC one.
    workflow = (ROOT / ".github/workflows/desktop-artifacts.yml").read_text(encoding="utf-8")
    assert "msvc-dev-cmd" not in workflow
    for uses in re.findall(r"(?m)^\s*-?\s*uses:\s*(\S+)", workflow):
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", uses), uses


def test_no_stock_windows_shell_is_downloaded_and_the_assembler_refuses_windows_without_the_built_one(
        tmp_path, monkeypatch):
    assemble = _assemble()
    source = (ROOT / "packaging/desktop/assemble.py").read_text(encoding="utf-8")
    # The release's binary is Linux's alone (the Flatpak replaces it); nothing names a Windows or macOS one.
    assert assemble.RELEASED_SHELLS == {("Linux", "x86_64"): "neutralino-linux_x64"}
    assert "neutralino-win_x64" not in source and "neutralino-mac_" not in source
    workflow = (ROOT / ".github/workflows/desktop-artifacts.yml").read_text(encoding="utf-8")
    assert "neutralinojs/neutralinojs/releases" not in workflow
    assert "neutralinojs/releases" not in WINDOWS_SHELL.read_text(encoding="utf-8")

    def no_download(*_args, **_kwargs):
        raise AssertionError("nothing may be downloaded")

    monkeypatch.setattr(assemble, "fetch", no_download)
    for system, machine in (("Windows", "AMD64"), ("Darwin", "arm64")):
        # Without --shell: refused at once, before any download or install.
        monkeypatch.setattr(assemble.platform, "system", lambda system=system: system)
        monkeypatch.setattr(assemble.platform, "machine", lambda machine=machine: machine)
        monkeypatch.setattr(sys, "argv", ["assemble.py", "--out", str(tmp_path / "layout")])
        with pytest.raises(SystemExit, match="needs --shell"):
            assemble.main()
        with pytest.raises(SystemExit, match="ships only the shell DownloadPulse builds"):
            assemble.install_shell(tmp_path / "shell.exe", None, (system, machine), tmp_path)


def _built_shell(directory: Path, name: str, **provenance) -> dict:
    import hashlib
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(b"MZ built from source")
    record = {"neutralino": "v6.10.0", "commit": NEUTRALINO_COMMIT,
              "patches": {"shell-server-boundary.patch": hashlib.sha256(
                  (ROOT / "packaging/desktop/neutralino/shell-server-boundary.patch").read_bytes()).hexdigest()},
              "architecture": "x64", "cmake": "cmake version 3.31.6", "ninja": "1.12.1",
              "compiler": "Microsoft (R) C/C++ Optimizing Compiler Version 19.44.35217 for x64",
              "sdk": "10.0.26100.0", "binary_sha256": hashlib.sha256(b"MZ built from source").hexdigest()}
    record.update(provenance)
    import json
    (directory / "PROVENANCE.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def test_the_assembler_installs_the_built_windows_shell_only_as_its_provenance_names_it(tmp_path):
    assemble = _assemble()
    key = ("Windows", "AMD64")
    target = tmp_path / "layout" / "downloadpulse-shell.exe"
    target.parent.mkdir()
    record = _built_shell(tmp_path / "built", "downloadpulse-shell.exe")
    # The .exe beside its provenance, recorded whole (the toolchain included) for BUILD-INFO.json.
    assert assemble.install_shell(target, tmp_path / "built", key, tmp_path) == record
    assert target.read_bytes() == b"MZ built from source"
    # A directory without the .exe, another binary, another commit, or no server patch: refused.
    _built_shell(tmp_path / "unix-name", "downloadpulse-shell")
    with pytest.raises(SystemExit, match="no downloadpulse-shell.exe"):
        assemble.install_shell(target, tmp_path / "unix-name", key, tmp_path)
    _built_shell(tmp_path / "other", "downloadpulse-shell.exe", binary_sha256="0" * 64)
    with pytest.raises(SystemExit, match="not the one its provenance names"):
        assemble.install_shell(target, tmp_path / "other", key, tmp_path)
    _built_shell(tmp_path / "commit", "downloadpulse-shell.exe", commit="0" * 40)
    with pytest.raises(SystemExit, match="names commit"):
        assemble.install_shell(target, tmp_path / "commit", key, tmp_path)
    _built_shell(tmp_path / "unpatched", "downloadpulse-shell.exe", patches={})
    with pytest.raises(SystemExit, match="does not name shell-server-boundary.patch"):
        assemble.install_shell(target, tmp_path / "unpatched", key, tmp_path)
    _built_shell(tmp_path / "stale", "downloadpulse-shell.exe", patches={"shell-server-boundary.patch": "0" * 64})
    with pytest.raises(SystemExit, match="does not name shell-server-boundary.patch"):
        assemble.install_shell(target, tmp_path / "stale", key, tmp_path)


def test_the_windows_shell_records_its_provenance_and_the_handover_carries_its_recipe():
    script = WINDOWS_SHELL.read_text(encoding="utf-8")
    provenance = script[script.index("$provenance = [ordered]@{"):script.index("$json = $provenance")]
    for field in ("neutralino", "commit", "patches", "architecture", "cmake", "ninja", "compiler", "msvc_toolset",
                  "visual_studio", "sdk", "binary_sha256"):
        assert re.search(rf"(?m)^\s+{field}\s+= ", provenance), field
    assert "patches       = [ordered]@{ $ServerPatch = $ServerPatchSha256 }" in provenance
    assert "compiler      = $banner.Line.Trim()" in provenance and "sdk           = $env:WindowsSDKVersion" in provenance
    assert "$target = Join-Path $outDir 'downloadpulse-shell.exe'" in script
    # Written without a byte-order mark (assemble.py json.loads it as UTF-8).
    assert "[Text.UTF8Encoding]::new($false)" in script and "Join-Path $outDir 'PROVENANCE.json'" in script
    # The provenance reaches the installer's BUILD-INFO.json, which the handover carries.
    assemble = (ROOT / "packaging/desktop/assemble.py").read_text(encoding="utf-8")
    assert 'shell = install_shell(target, args.shell, key, args.cache)' in assemble and '"shell": shell,' in assemble
    workflow = (ROOT / ".github/workflows/desktop-artifacts.yml").read_text(encoding="utf-8")
    assert 'cp "$RUNNER_TEMP/layout/BUILD-INFO.json" desktop-build/windows/' in workflow
    assert 'cp "$dir/BUILD-INFO.json" "$h/BUILD-INFO-$os.json"' in workflow
    # The corresponding source names how Windows builds it, and carries the recipe.
    source = (ROOT / "packaging/desktop/neutralino/source.sh").read_text(encoding="utf-8")
    assert '"$here/build-windows.ps1" "$out/"' in source
    assert "- **Windows (the installer):** \\`build-windows.ps1\\` -- \\`shell-server-boundary.patch\\` applied" in source
    assert "released" not in source and "unmodified" not in source
    assert '"$src/neutralino/build-windows.ps1"' in workflow


def test_any_change_to_what_the_installers_ship_builds_them():
    """The desktop jobs build from the whole backend and interface, so a change
    anywhere in them starts a build -- not only the few files once listed (a
    media executor fix once reached no installer, 2026-11-16)."""
    paths = WORKFLOW[True]["push"]["paths"] if True in WORKFLOW else WORKFLOW["on"]["push"]["paths"]
    for required in ("backend/**", "frontend/**", "desktop/**", "packaging/desktop/**"):
        assert required in paths, required


def test_a_push_builds_all_three_and_a_dispatch_may_build_one_platform():
    """A push always builds all three; a dispatch may build one platform alone (macOS arm64's runners
    have been held for capacity), published on its own like every platform."""
    dispatch = WORKFLOW[True]["workflow_dispatch"]["inputs"]["platforms"]
    assert dispatch["default"] == "all" and dispatch["options"] == ["all", "macos", "windows", "linux"]
    plan = next(step for step in _steps("inputs") if step.get("id") == "plan")
    assert plan["env"]["CHOICE"] == "${{ github.event_name == 'workflow_dispatch' && inputs.platforms || 'all' }}"
    for line in ('all) platforms="macos windows linux" ;;', 'macos|windows|linux) platforms="$CHOICE" ;;'):
        assert line in plan["run"]
    assert JOBS["desktop"]["strategy"]["matrix"] == "${{ fromJSON(needs.inputs.outputs.matrix) }}"
    assert '[[ "$platforms" == *" $os "* ]] || continue' in _run("handover")
