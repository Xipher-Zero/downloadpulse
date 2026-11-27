; DownloadPulse for Windows x64 -- the per-user installer (NSIS 3.11, the pinned
; Debian package; built by packaging/desktop/windows/build-installer.sh).
;
; * Per-user: no elevation for DownloadPulse itself; the program goes to
;   %LOCALAPPDATA%\Programs\DownloadPulse (another folder may be chosen,
;   inside %LOCALAPPDATA% only: .onVerifyInstDir), its data stays in
;   %LOCALAPPDATA%\DownloadPulse (the supervisor's data root), whose content
;   neither an upgrade nor the uninstaller changes (they only hold its
;   desktop.lock, below). No configuration file is written, so the first run
;   is always the setup wizard. An installer started "as administrator"
;   starts DownloadPulse through the desktop's own Explorer, with the
;   account's ordinary rights (LaunchDownloadPulse); DownloadPulse itself
;   refuses an elevated run (desktop/supervisor.py privileged).
; * Silent: /S installs or uninstalls without a page.
; * Prerequisites, before any DownloadPulse file is written:
;     - Microsoft Visual C++ x64 Redistributable >= VCREDIST_MIN, read where
;       Microsoft documents it (HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\
;       Runtimes\X64, 64-bit view: Installed=1, Major/Minor/Bld), checked
;       first, before anything is touched. Microsoft's own installer of it is
;       carried, unmodified (Distributable Code under the maintainer's Visual
;       Studio licence): missing or older, an interactive install runs it with
;       its own interface -- Microsoft's licence terms, for the person to
;       accept -- and Windows' permission prompt, then checks again (Retry or
;       Cancel); a silent install (/S), which shows no terms to accept, writes
;       the requirement to the calling console and exits with code 3, leaving
;       any existing installation exactly as it was;
;     - Microsoft Edge WebView2 Runtime, read where Microsoft documents it
;       (EdgeUpdate Clients {F3017226-...} "pv", per machine or per user);
;       missing, Microsoft's Evergreen bootstrapper installs it per user.
;   A failed WebView2 installation ends the installation with an explanation
;   and nothing installed.
; * Uninstall removes the shortcut, the program files, the uninstall entry
;   and the Media Downloads helpers' AppContainer profile -- never the data
;   root or any download. Program files that cannot be removed or replaced
;   (in use) are reported, and the uninstall entry stays, so the user can
;   run the uninstaller or this installer again. A folder named app that is
;   not DownloadPulse's is never removed or replaced (ProgramFilesOurs).

Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"

!ifndef VERSION
  !error "VERSION is required (-DVERSION=...)"
!endif
!ifndef STAGE
  !error "STAGE (the assembled application directory) is required"
!endif
!ifndef OUTFILE
  !define OUTFILE "DownloadPulse-${VERSION}-windows-x64.exe"
!endif
!ifndef VCREDIST_MIN_MAJOR
  !define VCREDIST_MIN_MAJOR 14
!endif
!ifndef VCREDIST_MIN_MINOR
  !define VCREDIST_MIN_MINOR 51   ; the WS3 inventory's minimum (ujson, bencode2, cryptography: toolset 14.51)
!endif
!define VCREDIST_MIN_BLD 0
; Microsoft's official download of the latest supported x64 Redistributable
; (learn.microsoft.com/cpp/windows/latest-supported-vc-redist).
!define VCREDIST_DOWNLOAD "https://aka.ms/vc14/vc_redist.x64.exe"
!define WEBVIEW2_CLIENT "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\DownloadPulse"
!define AUMID_KEY "Software\Classes\AppUserModelId\net.downloadpulse.desktop"

Name "DownloadPulse"
OutFile "${OUTFILE}"
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\DownloadPulse"
InstallDirRegKey HKCU "${UNINSTALL_KEY}" "InstallLocation"
SetCompressor /SOLID lzma
ShowInstDetails show
; Drawn at the display's own scale (never blurred by Windows scaling), with the
; system's current controls.
ManifestDPIAware true
ManifestSupportedOS Win10
XPStyle on
BrandingText "DownloadPulse ${VERSION}"
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "DownloadPulse"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "FileDescription" "DownloadPulse installer"
VIAddVersionKey "LegalCopyright" "GPL-2.0-or-later"

; Modern UI 2: Welcome, License, Location, Progress, Finish (Launch DownloadPulse),
; and the uninstaller's Confirm, Progress and Finish -- in DownloadPulse's own
; pictures (packaging/desktop/assemble.py installer_bitmaps: the sign-in
; page's field, the mark and the page title's EKG; the mark on the header).
!define DP_ICONS "${STAGE}\desktop\shell\resources\icons"
!define MUI_ICON "${DP_ICONS}\app.ico"
!define MUI_UNICON "${DP_ICONS}\app.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${DP_ICONS}\installer-side.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "${DP_ICONS}\installer-side.bmp"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP "${DP_ICONS}\installer-header.bmp"
!define MUI_HEADERIMAGE_UNBITMAP "${DP_ICONS}\installer-header.bmp"
!define MUI_ABORTWARNING
!define MUI_UNABORTWARNING
; A wizard cancelled after an update closed DownloadPulse starts it again (DpUserAbort).
!define MUI_CUSTOMFUNCTION_ABORT DpUserAbort
; Launch from the Finish page: by then .onInstSuccess has let go of the data
; root's lock, so the DownloadPulse it starts can take it.
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Launch DownloadPulse"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchDownloadPulse

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${STAGE}\LICENSE"
; The folder: inside this account's own local application data, which only
; the account, SYSTEM and Administrators can change -- a folder elsewhere (say
; C:\X) inherits rights that let other accounts replace the program files
; DownloadPulse runs. Next stays disabled for any other (.onVerifyInstDir).
!define MUI_DIRECTORYPAGE_TEXT_TOP "Setup will install DownloadPulse in the following folder. It must be inside $LOCALAPPDATA, which only this account can change, outside DownloadPulse's data folder ($LOCALAPPDATA\DownloadPulse), and hold no app folder of another program. $_CLICK"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

Var dpElevated
Var dpOurs
Var dpInstDirOk

; Whether this installer runs with a token UAC elevated (Run as administrator):
; $dpElevated 1 for TokenElevationType 2 (TokenElevationTypeFull), the same
; test desktop/supervisor.py privileged refuses a run by.
Function DpElevated
  Push $1
  Push $2
  Push $3
  StrCpy $dpElevated 0
  System::Call 'kernel32::GetCurrentProcess() p .r1'
  System::Call 'advapi32::OpenProcessToken(p r1, i 0x8, *p 0 r1) i .r2'      ; TOKEN_QUERY
  ${If} $2 != 0
    System::Call 'advapi32::GetTokenInformation(p r1, i 18, *i 0 r2, i 4, *i 0) i .r3'   ; TokenElevationType
    System::Call 'kernel32::CloseHandle(p r1)'
    ${If} $3 != 0
    ${AndIf} $2 == 2
      StrCpy $dpElevated 1
    ${EndIf}
  ${EndIf}
  Pop $3
  Pop $2
  Pop $1
FunctionEnd

Function LaunchDownloadPulse
  Call DpElevated
  ${If} $dpElevated == 1
    ; Started "as administrator": never with this installer's elevated token.
    ; The desktop's own Explorer -- already running as this account, without
    ; elevation -- opens the Start-menu shortcut, the same command as below.
    ${If} ${FileExists} "$SMPROGRAMS\DownloadPulse.lnk"
      Exec '"$WINDIR\explorer.exe" "$SMPROGRAMS\DownloadPulse.lnk"'
    ${Else}
      MessageBox MB_ICONINFORMATION|MB_OK "Start DownloadPulse from the Start menu." /SD IDOK
    ${EndIf}
    Return
  ${EndIf}
  ; The Start-menu shortcut's own command.
  Exec '"$INSTDIR\app\runtime\python\pythonw.exe" -I "$INSTDIR\app\desktop\supervisor.py"'
FunctionEnd

Var vcMajor
Var vcMinor
Var vcBld

; One owner of the data root at a time: the supervisor's lock -- an exclusive
; lock on the first byte of %LOCALAPPDATA%\DownloadPulse\desktop.lock, held for as
; long as DownloadPulse runs (desktop/supervisor.py single_instance). The
; installer and the uninstaller take that same byte before they change
; anything and hold it until they end, so DownloadPulse cannot be running while
; its program files, shortcut or uninstall entry change (a running program
; keeps files open: a half-installed or half-removed DownloadPulse), and a
; DownloadPulse started meanwhile finds the lock taken and ends before it starts.
; A first installation creates the data root only to hold the lock, and removes
; what it created when it lets go (DownloadPulse creates it on its first run).
Var dpLock
Var dpRootCreated

Var dpRelaunch

; Close the running DownloadPulse through its quit event, then wait (up to
; 60 seconds) for its lock: $1 is the open lock file, $2 becomes nonzero
; once the lock is ours. A DownloadPulse that does not close -- one older than
; this event (asked at once: it has no event to set), or one that is stuck --
; is the person's to Quit: Retry waits for its lock again, Cancel ends setup
; with nothing changed ($2 stays 0).
Function CloseRunningDownloadPulse
  StrCpy $5 1                     ; the first pass
  dpSignal:
  ; EVENT_MODIFY_STATE, not inheritable
  System::Call 'kernel32::OpenEventW(i 0x0002, i 0, w "Local\DownloadPulse-Quit") p .r3'
  ${If} $3 != 0
    System::Call 'kernel32::SetEvent(p r3)'
    System::Call 'kernel32::CloseHandle(p r3)'
  ${ElseIf} $5 == 1
    ; No quit event: a DownloadPulse older than 1.0.7, which only its own
    ; Quit closes. Asked at once, not after a wait with nothing on screen.
    StrCpy $5 0
    Goto dpAsk
  ${EndIf}
  StrCpy $5 0
  StrCpy $4 0
  ${Do}
    System::Call 'kernel32::LockFile(p r1, i 0, i 0, i 1, i 0) i .r2'
    ${If} $2 != 0
      Return
    ${EndIf}
    Sleep 500
    IntOp $4 $4 + 1
  ${LoopUntil} $4 >= 120          ; 120 x 0.5 s
  dpAsk:
  MessageBox MB_ICONEXCLAMATION|MB_RETRYCANCEL "Quit DownloadPulse from its tray menu (Quit), then choose Retry.$\n$\nCancel: quit setup (nothing has been changed)" /SD IDCANCEL IDRETRY dpSignal
  StrCpy $2 0
FunctionEnd

!macro DP_DATA_ROOT_LOCK UN
Function ${UN}TakeDataRootLock
  StrCpy $dpRootCreated 0
  ${IfNot} ${FileExists} "$LOCALAPPDATA\DownloadPulse\*.*"
    CreateDirectory "$LOCALAPPDATA\DownloadPulse"
    StrCpy $dpRootCreated 1
  ${EndIf}
  ; GENERIC_READ|GENERIC_WRITE, FILE_SHARE_READ|FILE_SHARE_WRITE (the
  ; supervisor's own open), OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL
  System::Call 'kernel32::CreateFileW(w "$LOCALAPPDATA\DownloadPulse\desktop.lock", i 0xC0000000, i 3, p 0, i 4, i 0x80, p 0) p .r1'
  ${If} $1 == -1
    ${If} $dpRootCreated == 1
      RMDir "$LOCALAPPDATA\DownloadPulse"
    ${EndIf}
    MessageBox MB_ICONSTOP|MB_OK "$LOCALAPPDATA\DownloadPulse\desktop.lock cannot be opened, so it is not known whether DownloadPulse is running. Nothing was changed." /SD IDOK
    Abort
  ${EndIf}
  System::Call 'kernel32::LockFile(p r1, i 0, i 0, i 1, i 0) i .r2'
  ${If} $2 == 0
!if "${UN}" == ""
    ; DownloadPulse is running: an update closes it (asked first), as its own
    ; Quit does -- the named event desktop/supervisor.py QUIT_EVENT waits on --
    ; and starts it again once it is installed (dpRelaunch).
    MessageBox MB_ICONINFORMATION|MB_OKCANCEL "DownloadPulse is running. Setup will close it, update it and start it again.$\n$\nDownloads in progress continue where they left off." /SD IDOK IDOK dpClose
    System::Call 'kernel32::CloseHandle(p r1)'
    Abort
    dpClose:
    StrCpy $dpRelaunch 1
    Call CloseRunningDownloadPulse
    ${If} $2 == 0
      System::Call 'kernel32::CloseHandle(p r1)'
      Abort
    ${EndIf}
!else
    System::Call 'kernel32::CloseHandle(p r1)'
    MessageBox MB_ICONSTOP|MB_OK "DownloadPulse is running. Quit it from its tray menu (Quit), then run this again." /SD IDOK
    Abort
!endif
  ${EndIf}
  StrCpy $dpLock $1
FunctionEnd

Function ${UN}ReleaseDataRootLock
  ${If} $dpLock != ""
    System::Call 'kernel32::UnlockFile(p $dpLock, i 0, i 0, i 1, i 0)'
    System::Call 'kernel32::CloseHandle(p $dpLock)'
    StrCpy $dpLock ""
    ${If} $dpRootCreated == 1
      Delete "$LOCALAPPDATA\DownloadPulse\desktop.lock"
      RMDir "$LOCALAPPDATA\DownloadPulse"
    ${EndIf}
  ${EndIf}
FunctionEnd
!macroend
!insertmacro DP_DATA_ROOT_LOCK ""
!insertmacro DP_DATA_ROOT_LOCK "un."

; Taken first, before any page or section; let go once the run has ended,
; whichever way it ended (the system also lets go when the process ends).
; Removing the program files. The lock keeps DownloadPulse from starting, but a
; launch from the Start-menu shortcut meanwhile still loads the bundled
; interpreter from these files before its supervisor finds the lock taken and
; ends -- holding them open for that moment. Removal is retried while that
; lasts (10 s); files still present after that are in use by something else,
; and the caller reports it.
; Whether $INSTDIR\app is DownloadPulse's own ($dpOurs 1), so that removing it
; removes nothing else: absent (nothing to remove); named by the uninstall
; entry, which an installation writes before its first program file; or
; holding DownloadPulse's own interpreter and supervisor. Any other app
; folder is someone else's, and is neither removed nor replaced.
!macro DP_PROGRAM_FILES_OURS UN
Function ${UN}ProgramFilesOurs
  Push $0
  StrCpy $dpOurs 1
  ${If} ${FileExists} "$INSTDIR\app\*.*"
    StrCpy $dpOurs 0
    ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "InstallLocation"
    ${If} $0 != ""
    ${AndIf} $0 == $INSTDIR
      StrCpy $dpOurs 1
    ${ElseIf} ${FileExists} "$INSTDIR\app\desktop\supervisor.py"
    ${AndIf} ${FileExists} "$INSTDIR\app\runtime\python\pythonw.exe"
      StrCpy $dpOurs 1
    ${EndIf}
  ${EndIf}
  Pop $0
FunctionEnd
!macroend
!insertmacro DP_PROGRAM_FILES_OURS ""
!insertmacro DP_PROGRAM_FILES_OURS "un."

!macro DP_REMOVE_PROGRAM_FILES UN
Function ${UN}RemoveProgramFiles
  Call ${UN}ProgramFilesOurs
  ${If} $dpOurs != 1
    Return                          ; the caller refused it already: never removed
  ${EndIf}
  StrCpy $R9 0
  dp_remove:
  RMDir /r "$INSTDIR\app"
  ${IfNot} ${FileExists} "$INSTDIR\app\*.*"
    Return
  ${EndIf}
  IntOp $R9 $R9 + 1
  ${If} $R9 < 20
    DetailPrint "Some program files are in use; trying again..."
    Sleep 500
    Goto dp_remove
  ${EndIf}
FunctionEnd
!macroend
!insertmacro DP_REMOVE_PROGRAM_FILES ""
!insertmacro DP_REMOVE_PROGRAM_FILES "un."

; Whether $INSTDIR is a folder DownloadPulse installs into ($dpInstDirOk): 1
; inside this account's own local application data ($LOCALAPPDATA) -- never
; that folder itself, nor the data root (%LOCALAPPDATA%\DownloadPulse) or
; anything in it -- with no app folder that is not DownloadPulse's; 0 outside
; it; 2 with someone else's app folder. Compared once made absolute
; (GetFullPathNameW: "..", "." and "/" resolved), ignoring case.
Function DpInstDirAllowed
  Push $0
  Push $1
  Push $2
  StrCpy $dpInstDirOk 0
  System::Call 'kernel32::GetFullPathNameW(w "$INSTDIR", i ${NSIS_MAX_STRLEN}, w .r0, p 0) i .r1'
  ${If} $LOCALAPPDATA != ""
  ${AndIf} $1 > 0
  ${AndIf} $1 < ${NSIS_MAX_STRLEN}
    StrLen $1 "$LOCALAPPDATA\"
    StrCpy $2 $0 $1
    ${If} $2 == "$LOCALAPPDATA\"
      StrCpy $2 $0 "" $1
      ${If} $2 != ""
        StrCpy $dpInstDirOk 1
        StrLen $1 "$LOCALAPPDATA\DownloadPulse\"
        StrCpy $2 "$0\" $1
        ${If} $2 == "$LOCALAPPDATA\DownloadPulse\"
          StrCpy $dpInstDirOk 0
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${EndIf}
  ${If} $dpInstDirOk == 1
    Call ProgramFilesOurs
    ${If} $dpOurs != 1
      StrCpy $dpInstDirOk 2
    ${EndIf}
  ${EndIf}
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

Function .onVerifyInstDir
  ; The directory page: Next stays disabled for a folder DownloadPulse does not install into.
  Call DpInstDirAllowed
  ${If} $dpInstDirOk != 1
    Abort
  ${EndIf}
FunctionEnd

; The folder refused, said to the person or, silent, to the caller (exit 4); nothing is changed.
Function RefuseInstDir
  ${If} $dpInstDirOk == 2
    StrCpy $4 "$INSTDIR\app is not DownloadPulse's: setup never removes or replaces a folder it did not install. Choose another folder, then run setup again. Nothing was changed."
  ${Else}
    StrCpy $4 "DownloadPulse installs only inside $LOCALAPPDATA, which only this account can change, and outside its data folder ($LOCALAPPDATA\DownloadPulse); $INSTDIR is not. Choose a folder there, then run setup again. Nothing was changed."
  ${EndIf}
  ${If} ${Silent}
    StrCpy $4 "DownloadPulse setup: $4$\r$\n"
    Call TellCaller
  ${Else}
    MessageBox MB_ICONSTOP|MB_OK "$4"
  ${EndIf}
  SetErrorLevel 4
  Quit
FunctionEnd

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP|MB_OK "DownloadPulse for Windows needs 64-bit Windows." /SD IDOK
    Abort
  ${EndIf}
  Call RequireVcRedist      ; before the data-root lock and any file: nothing is changed without it
  ${If} ${Silent}           ; no directory page: the folder (/D, or the installed one) is checked here
    Call DpInstDirAllowed
    ${If} $dpInstDirOk != 1
      Call RefuseInstDir
    ${EndIf}
  ${EndIf}
  Call TakeDataRootLock
FunctionEnd
Function DpUserAbort
  ; Cancelled on any page: nothing was changed, and a DownloadPulse this setup
  ; closed is started again, as the dialog that closed it said.
  Call ReleaseDataRootLock
  ${If} $dpRelaunch == 1
    Call LaunchDownloadPulse
  ${EndIf}
FunctionEnd

Function .onInstSuccess
  Call ReleaseDataRootLock
  ; An update that closed DownloadPulse starts it again: a silent one here, an
  ; interactive one from the Finish page (its Launch box, checked by default).
  ${If} ${Silent}
  ${AndIf} $dpRelaunch == 1
    Call LaunchDownloadPulse
  ${EndIf}
FunctionEnd
Function .onInstFailed
  Call ReleaseDataRootLock
FunctionEnd
Function .onGUIEnd
  Call ReleaseDataRootLock
FunctionEnd

Function un.onInit
  Call un.TakeDataRootLock
FunctionEnd
Function un.onUninstSuccess
  Call un.ReleaseDataRootLock
FunctionEnd
Function un.onUninstFailed
  Call un.ReleaseDataRootLock
FunctionEnd
Function un.onGUIEnd
  Call un.ReleaseDataRootLock
FunctionEnd

Function VcRedistSatisfied
  ; Microsoft's documented detection: the 64-bit registry view.
  SetRegView 64
  ReadRegDWORD $0 HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" "Installed"
  ReadRegDWORD $vcMajor HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" "Major"
  ReadRegDWORD $vcMinor HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" "Minor"
  ReadRegDWORD $vcBld HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" "Bld"
  SetRegView lastused
  StrCpy $1 0
  ${If} $0 == 1
    ${If} $vcMajor > ${VCREDIST_MIN_MAJOR}
      StrCpy $1 1
    ${ElseIf} $vcMajor == ${VCREDIST_MIN_MAJOR}
      ${If} $vcMinor > ${VCREDIST_MIN_MINOR}
        StrCpy $1 1
      ${ElseIf} $vcMinor == ${VCREDIST_MIN_MINOR}
      ${AndIf} $vcBld >= ${VCREDIST_MIN_BLD}
        StrCpy $1 1
      ${EndIf}
    ${EndIf}
  ${EndIf}
  Push $1
FunctionEnd

; A silent run's refusal ($4), said where the caller can read it: the standard
; output it gave the installer (a redirect or a pipe), else the console that
; started it.
Function TellCaller
  StrLen $5 $4
  System::Call 'kernel32::GetStdHandle(i -11) p .r6'
  System::Call 'kernel32::GetFileType(p r6) i .r7'
  ${If} $7 != 0     ; FILE_TYPE_UNKNOWN: no usable standard output
    System::Call 'kernel32::WriteFile(p r6, m r4, i r5, *i .r8, p 0)'
  ${Else}
    System::Call 'kernel32::AttachConsole(i -1) i .r3'
    ${If} $3 != 0
      FileOpen $3 "CONOUT$$" w
      FileWrite $3 $4
      FileClose $3
    ${EndIf}
  ${EndIf}
FunctionEnd

Function RequireVcRedist
  Call VcRedistSatisfied
  Pop $0
  ${If} $0 == 1
    Return
  ${EndIf}
  ${If} $vcMajor == ""
    StrCpy $2 "none"
  ${Else}
    StrCpy $2 "$vcMajor.$vcMinor.$vcBld"
  ${EndIf}
  ${If} ${Silent}
    ; No page: say so where the caller can read it -- the standard output it
    ; gave the installer (a redirect or a pipe), else the console that started
    ; it -- and fail with a distinct code before anything is changed.
    StrCpy $4 "DownloadPulse setup: the Microsoft Visual C++ x64 Redistributable ${VCREDIST_MIN_MAJOR}.${VCREDIST_MIN_MINOR} or newer is required (found: $2). Install it from Microsoft (${VCREDIST_DOWNLOAD}) and run setup again. Nothing was changed.$\r$\n"
    Call TellCaller
    SetErrorLevel 3
    Quit
  ${EndIf}
  MessageBox MB_ICONINFORMATION|MB_OKCANCEL "DownloadPulse needs the Microsoft Visual C++ x64 Redistributable ${VCREDIST_MIN_MAJOR}.${VCREDIST_MIN_MINOR} or newer (found on this computer: $2).$\n$\nSetup will now open Microsoft's installer for it. Read and accept Microsoft's license terms there; Windows will ask for permission to install it.$\n$\nCancel: quit setup (nothing has been changed)" IDOK vcInstall
  SetErrorLevel 3
  Quit
  vcInstall:
  ; Microsoft's installer needs administrator rights: started through the
  ; shell, so Windows asks for them. Its own interface shows its licence terms.
  InitPluginsDir
  File /oname=$PLUGINSDIR\vc_redist.x64.exe "${STAGE}\prerequisites\vc_redist.x64.exe"
  ExecShellWait "open" "$PLUGINSDIR\vc_redist.x64.exe" "/install /norestart"
  Call VcRedistSatisfied
  Pop $0
  ${If} $0 == 1
    Return
  ${EndIf}
  MessageBox MB_ICONEXCLAMATION|MB_RETRYCANCEL "The Microsoft Visual C++ x64 Redistributable was not installed, so DownloadPulse cannot be installed yet.$\n$\nRetry: open Microsoft's installer again$\nCancel: quit setup (nothing has been changed)" IDRETRY vcInstall
  SetErrorLevel 3
  Quit
FunctionEnd

Function WebView2Satisfied
  ; Microsoft's documented detection: a non-empty, non-"0.0.0.0" pv, per
  ; machine (WOW6432Node) or per user.
  StrCpy $1 0
  ReadRegStr $0 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\${WEBVIEW2_CLIENT}" "pv"
  ${If} $0 != ""
  ${AndIf} $0 != "0.0.0.0"
    StrCpy $1 1
  ${EndIf}
  ReadRegStr $0 HKCU "Software\Microsoft\EdgeUpdate\Clients\${WEBVIEW2_CLIENT}" "pv"
  ${If} $0 != ""
  ${AndIf} $0 != "0.0.0.0"
    StrCpy $1 1
  ${EndIf}
  Push $1
FunctionEnd

Section "Prerequisites" SecPrerequisites
  SectionIn RO
  ; The folder once more, before anything is written (the page and .onInit checked it).
  Call DpInstDirAllowed
  ${If} $dpInstDirOk != 1
    Call RefuseInstDir
  ${EndIf}
  InitPluginsDir
  ; The Visual C++ Redistributable was required in .onInit (RequireVcRedist).
  Call WebView2Satisfied
  Pop $0
  ${If} $0 != 1
    DetailPrint "Installing the Microsoft Edge WebView2 Runtime for this account..."
    File /oname=$PLUGINSDIR\MicrosoftEdgeWebview2Setup.exe "${STAGE}\prerequisites\MicrosoftEdgeWebview2Setup.exe"
    ExecWait '"$PLUGINSDIR\MicrosoftEdgeWebview2Setup.exe" /silent /install' $2
    Call WebView2Satisfied
    Pop $0
    ${If} $0 != 1
      MessageBox MB_ICONSTOP|MB_OK "The Microsoft Edge WebView2 Runtime could not be installed (exit $2). Nothing of DownloadPulse was installed." /SD IDOK
      Abort "The Microsoft Edge WebView2 Runtime is missing."
    ${EndIf}
  ${EndIf}
SectionEnd

Section "DownloadPulse" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  ; An upgrade replaces the program files in place; the data root is elsewhere.
  ; Only DownloadPulse's own app folder is ever removed (ProgramFilesOurs).
  Call ProgramFilesOurs
  ${If} $dpOurs != 1
    MessageBox MB_ICONSTOP|MB_OK "$INSTDIR\app is not DownloadPulse's: setup never removes or replaces a folder it did not install. Choose another folder, then run setup again." /SD IDOK
    Abort "$INSTDIR\app is not DownloadPulse's: nothing was replaced."
  ${EndIf}
  Call RemoveProgramFiles
  ${If} ${FileExists} "$INSTDIR\app\*.*"
    MessageBox MB_ICONSTOP|MB_OK "The installed DownloadPulse program files could not be replaced: some of them are in use ($INSTDIR\app). Close what is using them, then run this installer again. The existing uninstall entry is kept." /SD IDOK
    Abort "Program files in use: nothing was replaced."
  ${EndIf}
  ; The uninstall entry first: whatever happens next, it can remove or repair.
  WriteUninstaller "$INSTDIR\uninstall.exe"
  Call WriteUninstallEntry
  SetOutPath "$INSTDIR\app"
  ClearErrors
  File /r "${STAGE}\*.*"
  ${If} ${Errors}
    MessageBox MB_ICONSTOP|MB_OK "Some DownloadPulse program files could not be written ($INSTDIR\app). Close what is using them, then run this installer again; the uninstall entry is kept." /SD IDOK
    Abort "Program files could not be written."
  ${EndIf}
  CreateShortcut "$SMPROGRAMS\DownloadPulse.lnk" "$INSTDIR\app\runtime\python\pythonw.exe" '-I "$INSTDIR\app\desktop\supervisor.py"' "$INSTDIR\app\desktop\shell\resources\icons\app.ico"
  ; DownloadPulse's notification identity (desktop/supervisor.py windows_notice): the name and
  ; icon Windows shows on its notices, for the application ID they are sent under.
  WriteRegStr HKCU "${AUMID_KEY}" "DisplayName" "DownloadPulse"
  WriteRegStr HKCU "${AUMID_KEY}" "IconUri" "$INSTDIR\app\desktop\shell\resources\icons\app.png"
SectionEnd

Function WriteUninstallEntry
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "DownloadPulse"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "DownloadPulse"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\app\desktop\shell\resources\icons\app.ico"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
FunctionEnd

Section "Uninstall"
  ; The shortcut first: no new launch from it while the files go.
  Call un.ProgramFilesOurs
  ${If} $dpOurs != 1
    MessageBox MB_ICONSTOP|MB_OK "$INSTDIR\app is not DownloadPulse's program folder, so the uninstaller removes nothing. Your settings, database and downloads were not touched." /SD IDOK
    Abort "$INSTDIR\app is not DownloadPulse's: nothing was removed."
  ${EndIf}
  Delete "$SMPROGRAMS\DownloadPulse.lnk"
  Call un.RemoveProgramFiles
  ${If} ${FileExists} "$INSTDIR\app\*.*"
    MessageBox MB_ICONSTOP|MB_OK "Some DownloadPulse program files could not be removed: they are in use ($INSTDIR\app). Close what is using them, then uninstall DownloadPulse again (Settings > Apps, or $INSTDIR\uninstall.exe). Your settings, database and downloads were not touched." /SD IDOK
    Abort "Program files in use: the uninstall entry is kept."
  ${EndIf}
  ; The Media Downloads helpers' AppContainer profile (executors.media.netless).
  System::Call 'userenv::DeleteAppContainerProfile(w "DownloadPulse.MediaHelpers") i .r0'
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "${AUMID_KEY}"
  ; %LOCALAPPDATA%\DownloadPulse (settings, database, logs) and every download stay.
SectionEnd
