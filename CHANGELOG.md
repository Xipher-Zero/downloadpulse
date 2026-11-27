# Changelog

## [1.0.8] — Development

### Security

- **The desktop window's local helper no longer serves files from your disk or hands out its
  access key.** The small server the desktop window runs on your own computer could be asked,
  by any program on it, for any file your account can read; and the key that controls the
  window went to the first program that asked for it. On Windows, macOS and Linux, that server
  now holds only the window's own settings and icons, and DownloadPulse takes the key itself
  as soon as the window starts. On macOS and Linux, the server also answers only requests made
  to it by its own address, so a web page cannot reach it.

### Fixed

- **Windows: downloads whose names contain capital letters are no longer lost when they finish.**
  Since 1.0.0, on Windows, a download made through aria2 (HTTP and HTTPS links, debrid links and
  the update installer) was rejected by DownloadPulse's final check when its file name, or a
  folder below the download folder, had a capital letter, and the finished file was deleted.
  The check now compares the file DownloadPulse planned with the file aria2 wrote by their real
  names. This also stopped the in-app update on Windows: on 1.0.7 or older, install 1.0.8 once
  from downloadpulse.net; later updates work from inside the app.

## [1.0.7] — Updates that explain themselves and install over a running app

### Changed

- **The update notice shows what is new, inside DownloadPulse.** Its **Release notes** title
  opens, beneath it, the notes of each release newer than yours (up to the last ten), newest
  first, in their own scrolling area; nothing opens a web page. **Not now** sits at the left of the footer, the
  download at the right.
- **The Windows installer updates a running DownloadPulse.** It asks, closes DownloadPulse as its
  own Quit does, installs the update and starts it again; downloads in progress continue where
  they left off. A DownloadPulse that does not close is yours to Quit from the tray, and setup
  offers Retry instead of stopping.

### Known limitations

- **The first update to 1.0.7 on Windows still asks you to Quit.** A DownloadPulse older than
  1.0.7 cannot be closed by the installer, which asks at once: Quit it from the tray, then
  choose Retry. Updates after 1.0.7 close it by themselves.
- **The new notice appears from the next update.** The notice is part of the version you run, so
  the release notes inside it are shown when you are offered an update after 1.0.7.

## [1.0.6] — A lighter tray on every desktop

The desktop applications were not published at 1.0.5: 1.0.6 is their first release since 1.0.4,
and it includes everything in the [1.0.5 release notes](https://github.com/Xipher-Zero/downloadpulse/releases/tag/v1.0.5).

### Changed

- **The Windows and macOS desktop lets go of the window's page while it sits in the tray.** As on
  Linux, a window hidden for 10 minutes gives up what the application's page holds, and Open
  brings it back as it was: the same page, filters, search, place in the list, open Details,
  Settings or Help section, Quick Add draft and dashboard pulse, opening as soon as it is ready,
  with a note of what happened while it was away. There the tray belongs to the window itself, so the window stays and only its page goes;
  the tray, the Dock icon and Cmd+Q are unchanged. A window that would lose something is kept as
  it is.

## [1.0.5] — A lighter desktop that comes back as you left it, and Usenet only when it is needed

### Added

- **Choose whether a provider imports downloads started elsewhere.** Each premium service
  (AllDebrid, Debrid-Link, Premiumize, Real-Debrid, TorBox) has **Import Downloads Started
  Elsewhere** in its Additional Settings. It is on by default, as before: a download on your
  account that another installation (or the service's own website) started is added here too.
  Turned off, only this installation's own downloads are followed, so a torrent your server
  started no longer appears in your desktop application.
- **A desktop window reopened from the tray says what happened while it was away (Linux).** One notice names what
  completed, what failed and what needs attention, and points to the Activity Log for the rest.
  When nothing happened, nothing is said.

### Changed

- **The Linux desktop frees its window's memory while it sits in the tray.** A window hidden
  for 10 minutes is closed behind the scenes and opened again from the tray as it was: the
  same page, filters, search, place in the list, open Details, Settings or Help section, Quick
  Add draft and dashboard pulse. A window that would lose something (a setting not yet saved, an
  open dialog, a sign-in under way) is kept as it is. In our testing an idle desktop dropped from
  600–1000 MB to about 150–180 MB.
- **A window reopened from the tray opens as soon as it is ready (Linux).** It shows no
  three-second loading screen; it draws one only if loading takes longer than half a second. A
  cold start keeps its loading screen.
- **The window's first frame follows the theme.** It is black in dark mode and white in light
  mode when the application starts (Linux and Windows), instead of a white flash.
- **SABnzbd runs only while it is needed.** With Usenet enabled, the bundled SABnzbd starts for
  a server test or an NZB and stops one minute after its last work. Provider Status shows it as
  **Idle** (blue): a new NZB starts it again.
- **Less memory and faster page loads.** On Linux the server and the desktop keep fewer malloc
  arenas; the desktop supervisor no longer loads a web framework and every language to show its
  tray words; a dashboard sparkline repaints in its own layer; and scripts and stylesheets are
  cached until they change, so a page opened again loads most of them from the browser's cache.
  The desktop window keeps its port between launches so that cache survives.
- **The update badge floats in the page's bottom-right corner.** Its x hides it in this browser
  until the next update check. It is not shown on screens narrower than 780px.

### Fixed

- **Show in folder works in the Linux desktop.** It opens your file manager with the file
  selected; before, the Flatpak's sandbox refused the request.
- **A video offered only with DRM says so.** Details reported "Requested format is not
  available"; it now says the media is DRM protected, which DownloadPulse cannot download.

### Known limitations

- **A window reopened from the tray can flash white for an instant.** On Linux, after the window
  was released, it can show white before the page draws, whatever the theme.

## [1.0.4] — A server secured from its first run, and signed-in YouTube downloads again

### Security

- **A new server is secured before it is set up.** Until its Setup Wizard finished, a new
  installation had no sign-in, and a Standalone server finished without a password and stayed
  open. Now a Standalone server sets the password you sign in with during setup, and setup is
  refused to anyone reaching DownloadPulse from the internet until it is finished; your home
  network, a VPN and the server itself set up as before. The desktop application is unchanged.
  Afterwards you can still run DownloadPulse without a password on a network you trust: Clear
  Password in My Account.
- **A Content-Security-Policy on the whole application.** Scripts run only from DownloadPulse
  itself, so a hostile file or download name cannot run as script in your browser.
- **IPv6 addresses that carry an IPv4 address are judged by it.** On an IPv6-only network with
  NAT64, a download link could be pointed at a device on your home network; such addresses are
  now refused like the home-network address they reach.

### Fixed

- **Signed-in YouTube downloads get their formats again.** With a browser's YouTube sign-in,
  downloads failed with "Requested format is not available"; yt-dlp 2026.11.23 asks YouTube in
  the order it now answers.
- **A row's reason stays on one line.** The reason under a download's status no longer adds a
  line to its row; one that does not fit ends in an ellipsis and shows whole on hover.
- **The Linux desktop shows its screenshots in software centres.** The Flatpak is built only
  when its store screenshots are served and in its catalog, so this release's has them.

### Changed

- **The example `docker-compose.yml` is generic.** It keeps DownloadPulse's files and your
  downloads in folders beside it (`./downloadpulse/`, `./downloads/`), runs as 1000:1000 and
  takes your timezone, user, group and public address from `.env` (copy `.env.example`). It has
  no `build:` and no healthcheck of its own: the image's applies.
- **The README is in plain words** and introduces the desktop application; internal design notes moved to
  `docs/internal/`; the upstream MIT notice is now `licenses/alldebrid-client-MIT.txt`.

### Upgrading

- **Compose from a clone: its folders have moved.** Point its `volumes:` at your existing
  folders, and set `PUID`/`PGID` (and `TZ`, now `Etc/UTC` by default) in `.env` if they are not
  1000:1000 (Unraid: 99:100), before
  `docker compose up -d`.
- **An installation already set up stays as it is.** With or without a password, nothing about
  its sign-in changes.

## [1.0.3] — Update notices, faster recovery, and a desktop that keeps your files

### Fixed

- **A source that says "not found" is not asked again.** A direct link whose server answered 404
  (or 410) waited for a timed retry of the same address before needing attention, even with
  another source available. The route now ends at once: another source is tried now, and with
  none the download shows Requires attention right away. A provider's link that may only have
  expired is still refreshed first.
- **Cancel removes the partial files it was writing.** Cancel left the full-size pre-allocated
  file and its control file in the download folder; only Delete removed them. A cancelled
  download now removes what it was writing, as a deleted one does; files that finished
  downloading stay. A cancelled download is final: Retry does not reopen it.
- **After a restart, downloads resume at once.** A download that was running when DownloadPulse
  stopped waited a full retry delay in Waiting for retry after the restart, and counted a
  failure. It now continues as soon as DownloadPulse is back.
- **Less work while idle.** With nothing downloading, DownloadPulse no longer re-reads its
  download list several times a second.
- **The Linux desktop keeps downloads only where they survive.** The Flatpak's setup wizard
  offered the home folder, which the sandbox does not keep: files marked done vanished when the
  application closed. A folder the sandbox does not keep is now refused, with the reason, and
  the wizard suggests your Downloads folder instead of an empty field. The folder browser says
  "Current folder" in the desktop application.
- **The desktop quits when there is no tray.** On desktops without a system tray (GNOME without
  an extension), closing the window only minimized it and nothing could quit. There, the close
  button now asks to quit and quits cleanly; with a tray it hides to it as before. Stopping
  the application from the system shuts it down in order, and the tray's absence is logged
  once instead of every few seconds; the desktop logs now rotate. A window that fails to start
  is said in a notification (Linux and Windows) and logged.
- **Rows say why.** Failed, Requires attention and Waiting for retry rows show the reason under
  their status (and when the next attempt is), the same reason Details gives.
- **File sizes in the units they are labelled in.** File and download sizes were counted in
  1024s but labelled MB; they are now decimal (140,064,536 bytes is 140.1 MB). Speeds and speed
  caps are unchanged. Upload limits are now exact decimal sizes (1 GB, 16 MB, 1 MB). A multi-file download has one name ("+ N
  more") everywhere, and a route whose download then failed shows as failed in Route History.
- **First run.** Users & Access names the built-in Administrator for what it is ("Built-in ·
  used when sign-in is off") instead of an account with no sign-in method. A browser that tries
  to connect before setup is finished is told to finish setup first.
- **Details stays current.** An open Details window was read once and could disagree with the
  row beside it. It now follows the download live, without losing your place.
- **No deprecation warning from sign-in at startup.** OpenID Connect sign-in uses the HTTP
  library its sign-in component asks for. On Linux it now checks the provider's certificate
  against the system's trust store, as macOS and Windows already did.
- **The database file is private.** The database and its journal files (`-wal`, `-shm`) are
  now mode 0600, set again at every start. In the container the data folder was already 0700
  (also set at every start), so a backup tool there still has to run as `PUID:PGID` (99:100 on
  Unraid) or as root, as before. On the desktop the data folder itself is not made private,
  so where your home folder is readable by other accounts, the file mode is now what keeps
  them out of the database.

### Added

- **Each page has its own address.** Downloads, Activity Log, Statistics, Settings and Help open
  at /downloads, /activity, /statistics, /settings and /help, so they can be bookmarked, and Back
  and Forward move between them. Escape closes the Details window and returns to the row it was
  opened from.
- **Show in folder** (desktop application): a finished download's row and Details reveal its
  file in the system's file manager.
- **Update notice.** When a newer release is out, Admins see it once per session: what is
  installed, what is new and a link to the release notes, then a badge in the top bar until they
  update. The desktop applications offer to download the installer through DownloadPulse,
  checked against the release's SHA-256, and show it in its folder; you run it yourself. A server
  only links the release. The first check runs about a minute after start (it was five), and
  Update Check Interval 0 turns off the check and the notice.

### Changed

- **The desktop's store listing describes the desktop application**, with screenshots, and its
  release notes are generated from this changelog.
- **The delete endpoint's provider flag is `remove_remote`.** `from_alldebrid` is still
  accepted; giving both with different values is refused.
- **Logs name DownloadPulse**, and the startup line says what it runs in (desktop application,
  container or host process).
- **README:** the setup wizard, the per-download pause, resume and cancel endpoints, the API
  token's label, and rootless Podman.

## [1.0.2] — Downloads with your sign-in, and one download per file

### Added

- **Downloads that need your sign-in.** A link sent from the browser extension can bring the
  browser's cookies for that link's site and the page it was started from, and DownloadPulse
  uses them for that one download, whichever service or engine fetches it: a file behind a
  sign-in, or a host that checks where a download was started, now downloads as it would in
  the browser. They are held only in memory for that download, used only for the addresses
  they belong to, and never stored or logged.

### Fixed

- **The same file is never downloaded twice.** Sending a direct link that DownloadPulse is
  already downloading -- or has paused -- started a second download from scratch. The same link
  now joins the existing download once its content is confirmed to be the same, and a paused one
  resumes from what it already has. When the second request is another account's, each keeps
  its own transfer and request, and the first transfer's history records that another
  account's request resumed it. A global pause, or an external client's own pause, is never
  lifted by it.
- **A browser download that joined another shows its real progress.** A download sent from the
  browser that joined one already in progress showed as done at once; it now shows the shared
  download's state and progress, and is done when that file is.
- **Pause and Resume from the browser work like they do in DownloadPulse.** The extension refused
  to pause a download another request also needed, and to resume one paused in DownloadPulse.
  A connected browser acts for the person who connected it, so either person may now pause or
  resume a shared download from their browser, whoever paused it, as they can in DownloadPulse.
  Only a global pause is still left for DownloadPulse to lift. An Integration's pause is
  unchanged: it is honoured only while no other request needs the download.

### Changed

- **One sign-in path for every download.** The video sign-in 1.0.14 introduced is now the same
  general mechanism, owned by the transfer core and handed to the service and engine that
  fetch the link, instead of something only Media Downloads could read. It is offered for
  every link, whether or not Media Downloads is on.

## [1.0.1] — Native notifications and dependency updates

### Fixed

- **macOS notifications carry DownloadPulse's name and icon.** The desktop app's window process now
  runs from the application bundle's own code folder, so macOS delivers its notifications as
  DownloadPulse's instead of through Script Editor.
- **Windows notifications carry DownloadPulse's name and icon.** They are Windows notifications under
  DownloadPulse's own application identity, which the installer registers (and the uninstaller
  removes), instead of a tray balloon naming `downloadpulse-shell.exe` with a generic information icon.

### Changed

- **Dependencies:** FastAPI 0.163.2, Uvicorn 0.54.0, uvloop 0.23.0, httptools 0.9.0, aiohttp
  3.14.5, Pydantic 2.14.3, bencode2 0.3.38, python-multipart 0.0.34, AsyncSSH 2.24.1, Authlib 1.9.9,
  joserfc 1.8.0 and yt-dlp 2026.10.26; FastAPI now brings opentelemetry-api 1.45.2 (Apache-2.0). The
  bundled Usenet service's own packages stay at the versions SABnzbd 5.1.3 requires.

## [1.0.0] — First public release

DownloadPulse 1.0.0 is the first public release: a self-hosted download manager for a server or NAS,
and a desktop application for Windows, macOS and Linux, used from your browser. You give it a link, a
magnet, a `.torrent`, an NZB, a Metalink file or a list of links; it records one durable transfer,
works out which of your enabled services can obtain it, downloads the files with the right download
engine, and keeps that transfer and its history whole even when its service, source or engine
changes along the way.

### Included

- **Sources:** HTTP(S), FTP & SFTP, SCP, rsync, WebDAV, Metalink and supported media pages, with no
  premium account needed; magnets, `.torrent` files and NZBs through the services that accept them.
- **Premium services and Usenet, all optional:** AllDebrid, Debrid-Link, Premiumize, Real-Debrid and
  TorBox, and a bundled Usenet service for your own news servers. Each is off until you connect your
  own account.
- **Switching and recovery:** automatic retry and failover, backup torrents on a second service, and
  operator-controlled switching of a transfer's service or source.
- **File selection, extraction and delivery:** choose the files of a multi-file download, optional
  extraction of common archives, and a download folder of your choice.
- **Sonarr and Radarr:** DownloadPulse acts as a SABnzbd or qBittorrent download client through
  Integration Delivery Profiles.
- **The browser extension:** send links, files and supported video pages from Firefox or a
  Chromium-based browser, and follow and control the downloads it sent.
- **Accounts and access:** Standalone and Server deployment types; Admin, User, ReadOnly User and
  Integration accounts; Username & Password sign-in, OpenID Connect, and revocable tokens for
  connected tools.
- **Operating it:** scheduled backups with in-application restore, a searchable event log,
  statistics, Discord notifications and Prometheus metrics.
- **The interface:** a dashboard, downloads list, details and statistics, themes, and 19 languages.

### Installation

- **Server:** the Docker image `ghcr.io/xipher-zero/downloadpulse:v1.0.0` (Linux, amd64 and arm64)
  listens on port 8095. See the README's Installation section for the compose file.
- **Desktop:** installers for Windows (x64), macOS (Apple silicon) and Linux (Flatpak, x86_64).

### Known limitations

These are deliberate boundaries of 1.0.0, not defects. Each one fails safe: nothing is lost or
silently changed, and DownloadPulse tells you why an action is not available.

- **A source that was split into several files cannot have its provider switched.** When one
  submitted source resolves to several files that DownloadPulse downloads as separate members (for
  example a folder on a server, a Metalink description, or a provider's folder link), Switch
  provider is not offered for that source, and a switch requested through the API is refused as
  *Unsupported Request*. A single torrent, including one with many files, and a single link
  (including one link of a download made of several links) can still be switched. Switching an
  individual file to another of its own sources is unaffected.
- **A link shared with another unfinished download cannot be switched.** When DownloadPulse has
  recognised that a link of one download is the same source as another unfinished download's and
  serves both from one, that link's provider cannot be switched; the request is refused as
  *Unsupported Request*, also when the sharing is recognised while the switch is being made. A
  download that is a single torrent is switched as a whole and is not affected by this rule.
- **Automatic promotion of a prepared backup is narrow.** DownloadPulse moves a torrent to a backup
  another provider has already prepared only for a download made of a single torrent, only to the
  first backup that is ready, and never while the download is paused, after you have chosen its
  files, after it has been split into files, after its download has started, or when it was routed
  by the provider you prefer anyway. Backups are prepared only by providers that support it for
  torrents: AllDebrid, Debrid-Link, Real-Debrid and Premiumize (Prepare Backup Torrents on by default,
  once the provider itself is enabled); TorBox only when you turn
  it on, because TorBox's limits on new and active torrents are restrictive. Usenet, the direct
  sources (HTTP(S), FTP/SFTP, SCP, rsync, WebDAV), Metalink and Media Downloads never prepare
  backups.
- **A question waiting for your answer holds up the rest of that download.** While one part of a
  download is asking for input (a password, a sign-in or another answer), no other part of the same
  download starts anything new until you answer. This includes switching another part's provider:
  the switch is accepted and that part's earlier download stops, but it starts again on the
  provider you chose only after the question is answered. Other work of that download that was
  already running keeps running. Other downloads are not affected.
- **Cancelling one volume of a multi-part archive can make its extraction fail.** If you cancel one
  file of a download and that file is a volume of a split archive (for example `part02.rar`), the
  download still finishes once its remaining files are complete, but extraction of that archive
  fails because a volume is missing. The download is shown as completed with *Extraction failed*,
  the delivered volumes are kept, and nothing is overwritten or deleted. Retry downloads it again
  and extracts again.
- **Not every switch can keep what was already downloaded.** Only aria2 and rsync can continue a
  partly downloaded file from data another source delivered. Switching a file to a source that Media
  Downloads or the Usenet service (SABnzbd) would download starts it again from the beginning, and
  DownloadPulse asks you to confirm before it discards progress; if the progress changes after you
  confirmed, nothing is switched and you are asked again. After a provider has given up, automatic
  failover to another provider happens only before the source's files have been created.
- **Sonarr and Radarr see DownloadPulse as a qBittorrent or SABnzbd client, with these limits:**
  - Through the qBittorrent connection, a failed download is shown as *error*, which Sonarr and
    Radarr treat as a warning and never blocklist. Only API-token sign-in is supported (not the
    qBittorrent login/cookie sign-in), a torrent is added as one magnet link or one `.torrent` file
    (an http(s) link to a torrent is refused),
    torrents that are BitTorrent v2-only are refused, and options DownloadPulse does not support (such
    as changing a category, share limits, priority or forced start) are refused rather than ignored.
  - Through the SABnzbd connection, a failed download is reported as *Failed* (so the release is
    blocklisted and another is searched for) unless the cause is one you can fix without a new
    release: a provider's or download engine's credentials, configuration, availability or account
    limits, DownloadPulse's own storage, or a Delivery Profile problem. Those are reported as *Paused*.
    A completed download that cannot be imported yet is reported as *Verifying*.
  - A submitted `.torrent` can be at most 16 MiB and an NZB at most 8 MiB. Each submission is one
    item; the SABnzbd connection accepts uploaded NZBs only (adding by URL is refused). The native
    external-client API (`/api/external/v1`) takes at most 100 links per submission.
  - A Delivery Profile whose DP Download Path is outside the Download Folder keeps offering its
    categories, but new downloads to it are refused (`profile_root_outside_downloads`) until its path
    is moved back inside.
- **Very large downloads are bounded.** A file manifest can list at most 20,000 entries (paths up to
  1,024 characters), a Metalink description at most 10,000 files and 20,000 sources (16 MiB), a
  WebDAV scan at most 10,000 files by default, and an rsync listing 16 MiB. A source beyond these
  limits is refused rather than downloaded in part. Switching the provider of a torrent with
  hundreds of files takes noticeably longer than an ordinary switch.
- **Work grows with the number of files in one transfer.** DownloadPulse opens a fresh database
  connection for each step it takes, and many steps are taken file by file, so the engine's work in
  each cycle grows with how many files a transfer has. In one measured case, a transfer of 133
  files took about 3,000 database connections per engine cycle. That is an observation of that
  transfer, not a rate DownloadPulse guarantees or bounds.
- **Two mirror links of one file can each use the provider.** When a file is submitted through two
  mirror links (two separate links, not one item with alternative sources), each link can create its
  own resource at a debrid provider before DownloadPulse recognises them as the same file. This can
  use more of the account's quota and leave a duplicate at the provider; only one copy is
  downloaded and kept locally.
- **Some provider refusals end the download instead of trying another provider.** Real-Debrid's
  "file not allowed" (code 28) and Debrid-Link's `infringingFile` are treated as a permanent
  failure of the content, so the download fails instead of being handed to another provider.
  (Real-Debrid's code 35, by contrast, does hand over to the next provider.)
- **Live updates after a lost connection.** If the browser's live connection fails after it has
  worked once, the page marks itself as not live and tries to reconnect every 10 seconds, re-reading
  what it shows when it reconnects; it does not switch to periodic refreshing in the meantime. Only a
  page whose very first connection fails refreshes on a timer instead. Reloading the page restores
  live updates as soon as the server is reachable.
- **A browser that stops reading live updates.** When a browser stops reading its live updates (for
  example a suspended tab), DownloadPulse stops sending to it once it is 100 updates behind and keeps
  at most two pending messages for it, so it never holds up other browsers or grows memory. Its
  network connection itself can stay open until the browser or the network closes it.
- **aria2 downloads write each file at its final location.** aria2, and rsync with Partial
  Transfers on, write the file at its final path while it downloads; the Usenet service, Media
  Downloads and rsync with Partial Transfers off (or rebuilding from other material) already
  download privately and move the finished file into place. A general staged, engine-owned download with an atomic move into place on verified
  completion (`STAGED-MATERIALIZATION-001`) is not implemented in 1.0.0. A file
  whose download cannot be proven to have produced its content (for example an unproven empty
  file) is rejected and removed rather than reported complete, so where a file is written in
  place it can exist briefly at its final path before DownloadPulse retires it.
