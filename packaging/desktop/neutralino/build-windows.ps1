# The Windows native shell: Neutralino 6.10.0 built from its pinned source (the
# tag's commit, verified) with DownloadPulse's one Windows patch (its digest
# checked), for x64, with Microsoft's C/C++ compiler from the Visual Studio
# vswhere finds (its vcvars64.bat environment), CMake and Ninja -- the build
# Neutralino's own Windows release runs (cmake -G Ninja, Release; the static
# MSVC runtime and WebView2LoaderStatic.lib its CMakeLists.txt selects).
#   pwsh -File build-windows.ps1 <out-dir>
# Writes <out-dir>\downloadpulse-shell.exe and <out-dir>\PROVENANCE.json (source
# commit, patch digest, toolchain, binary SHA-256), which
# packaging/desktop/assemble.py --shell installs and records. Any step that
# fails stops the build: there is no fallback to Neutralino's released binary.
#
# shell-server-boundary.patch (Linux's and macOS's too): the shell's loopback
# server serves only paths inside its document root, answers only a Host of
# 127.0.0.1 or localhost at its own port, and refuses a WebSocket carrying an
# Origin. The Linux and macOS patches are not applied: each changes only its
# own system's code (GTK, AppKit), which a Windows build compiles out.
param([Parameter(Mandatory = $true)][string] $Out)
Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
$Tag = 'v6.10.0'
$Commit = '4361b63eb1157fc7dedb29b92815a08ec0002767'
$ServerPatch = 'shell-server-boundary.patch'
$ServerPatchSha256 = '73ea8318c48ddfaed2d3782ac9a69f7a6af571cdf8468bf0c7cd178b480e48be'

function Assert-Exit([string] $What) {
  if ($LASTEXITCODE -ne 0) { throw "build-windows.ps1: $What failed (exit $LASTEXITCODE)" }
}

New-Item -ItemType Directory -Force $Out | Out-Null
$outDir = (Resolve-Path $Out).Path
# A short working path (MSVC's tools keep to MAX_PATH): the runner's temporary directory when there is one.
$base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$work = Join-Path $base ('dp-neu-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory $work | Out-Null
try {
  # The source: the tag's tree, byte for byte (no CRLF conversion), at the commit it must be.
  $src = Join-Path $work 'src'
  git -c core.autocrlf=false clone -q --depth 1 --branch $Tag https://github.com/neutralinojs/neutralinojs.git $src
  Assert-Exit "git clone of Neutralino $Tag"
  $head = "$(git -C $src rev-parse HEAD)".Trim()
  Assert-Exit 'git rev-parse HEAD'
  if ($head -ne $Commit) { throw "build-windows.ps1: $Tag is $head, not $Commit" }

  # The patch: the one its digest names, applied to that tree.
  $patch = Join-Path $PSScriptRoot $ServerPatch
  $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $patch).Hash.ToLowerInvariant()
  if ($digest -ne $ServerPatchSha256) { throw "build-windows.ps1: $ServerPatch is $digest, not $ServerPatchSha256" }
  git -C $src apply $patch
  Assert-Exit "git apply $ServerPatch"

  # The x64 MSVC environment: the newest Visual Studio with the x64 C++ tools, its vcvars64.bat.
  $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
  if (-not (Test-Path -LiteralPath $vswhere)) { throw "build-windows.ps1: no vswhere.exe at $vswhere" }
  $vsArgs = @('-latest', '-products', '*', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64')
  $vs = "$(& $vswhere @vsArgs -property installationPath)".Trim()
  Assert-Exit 'vswhere (installationPath)'
  $vsVersion = "$(& $vswhere @vsArgs -property installationVersion)".Trim()
  Assert-Exit 'vswhere (installationVersion)'
  if (-not $vs) { throw 'build-windows.ps1: no Visual Studio with the x64 C++ tools (Microsoft.VisualStudio.Component.VC.Tools.x86.x64)' }
  $vcvars = Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat'
  if (-not (Test-Path -LiteralPath $vcvars)) { throw "build-windows.ps1: no vcvars64.bat at $vcvars" }
  $environment = & cmd.exe /d /s /c "`"$vcvars`" >nul && set"
  Assert-Exit 'vcvars64.bat'
  foreach ($line in $environment) {
    if ($line -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2]) }
  }
  if ($env:VSCMD_ARG_TGT_ARCH -ne 'x64') { throw "build-windows.ps1: vcvars64.bat set target '$env:VSCMD_ARG_TGT_ARCH', not x64" }
  if (-not $env:WindowsSDKVersion) { throw 'build-windows.ps1: vcvars64.bat named no Windows SDK' }
  $cl = (Get-Command cl.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)
  if (-not $cl) { throw 'build-windows.ps1: cl.exe is not on PATH after vcvars64.bat' }

  # The toolchain, as recorded in the provenance.
  $banner = & cmd.exe /d /c 'cl 2>&1' | Select-String -Pattern 'Compiler Version' | Select-Object -First 1
  if (-not $banner) { throw 'build-windows.ps1: cl.exe printed no compiler banner' }
  # Captured whole, then checked: a pipeline that stops the command early would leave
  # $LASTEXITCODE from the earlier, unchecked cl call.
  $cmakeOut = @(cmake --version)
  Assert-Exit 'cmake --version'
  $cmake = "$($cmakeOut[0])".Trim()
  $ninja = "$(ninja --version)".Trim()
  Assert-Exit 'ninja --version'

  # The build: Neutralino's own (CMakeLists.txt at the commit), Release, with Ninja.
  $build = Join-Path $work 'build'
  cmake -S $src -B $build -G Ninja '-DCMAKE_BUILD_TYPE=Release'
  Assert-Exit 'cmake (configure)'
  ninja -C $build
  Assert-Exit 'ninja (build)'
  $binary = Join-Path $src 'bin\neutralino-win_x64.exe'
  if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) { throw "build-windows.ps1: the build wrote no $binary" }
  # An x64 Windows executable (PE, machine 0x8664), nothing else.
  $bytes = [IO.File]::ReadAllBytes($binary)
  $pe = [BitConverter]::ToInt32($bytes, 0x3C)
  if ([BitConverter]::ToUInt32($bytes, $pe) -ne 0x00004550 -or [BitConverter]::ToUInt16($bytes, $pe + 4) -ne 0x8664) {
    throw "build-windows.ps1: $binary is not an x64 Windows executable"
  }

  $target = Join-Path $outDir 'downloadpulse-shell.exe'
  Copy-Item -Force -LiteralPath $binary -Destination $target
  $sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()
  $provenance = [ordered]@{
    neutralino    = $Tag
    commit        = $Commit
    patches       = [ordered]@{ $ServerPatch = $ServerPatchSha256 }
    architecture  = 'x64'
    cmake         = $cmake
    ninja         = $ninja
    compiler      = $banner.Line.Trim()
    msvc_toolset  = $env:VCToolsVersion
    visual_studio = $vsVersion
    sdk           = $env:WindowsSDKVersion.TrimEnd('\')
    binary_sha256 = $sha256
  }
  $json = $provenance | ConvertTo-Json -Depth 3
  # UTF-8 without a byte-order mark: assemble.py reads it with json.loads.
  [IO.File]::WriteAllText((Join-Path $outDir 'PROVENANCE.json'), $json + "`n", [Text.UTF8Encoding]::new($false))
  $json
}
finally {
  Remove-Item -Recurse -Force -LiteralPath $work -ErrorAction SilentlyContinue
}
