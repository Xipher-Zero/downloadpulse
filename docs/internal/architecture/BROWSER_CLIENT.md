# Browser client: pairing, browser tokens and downloads (`downloadpulse.browser/1`)

A person connects a browser extension to their own account: the extension
asks, the person approves on this server, and the extension receives its own
**browser token**. That token can do what a browser client needs and nothing
else; its owner, or an Admin, can disconnect it at any time.

Owners in the code:

- `backend/auth/browser_pairing.py`: pending pairings (in memory only).
- `backend/auth/accounts.py`: browser tokens (`kind = browser` in
  `account_tokens`), their issuance, resolution and revocation.
- `backend/auth/permissions.py`: the `browsers.own` and `browser.client`
  capabilities, and the fence (`capabilities`).
- `backend/auth/middleware.py`: extension origins under `/api/browser/v1/`.
- `backend/api/browser_routes.py`: the HTTP routes.
- `frontend/static/ui-browser-pairing.js`: the "Connect a browser" dialog.
- `frontend/static/auth.js`: My Account › Connected browsers.
- `frontend/static/ui-settings-page.js`: Account Details › Connected browsers
  (an Admin's view).

## 1. Pairing

A device-authorization flow, after RFC 8628.

| Step | Who | Route | Answer |
|---|---|---|---|
| Start | the extension, no credential | `POST /api/browser/v1/pairings` `{client_name}` | `{protocol, device_code, user_code, verification_path, expires_in, interval}` |
| Ask | the person, signed in | opens `verification_path` (`/?connect-browser=<user_code>`); `GET /api/account/browser-pairings/{user_code}` | `{user_code, client_name, requested_at, expires_in}` |
| Answer | the person (session + CSRF) | `POST …/{user_code}/approve` `{label}` or `POST …/{user_code}/deny` | `{ok, browser: {id, label}}` / `{ok}` |
| Collect | the extension, holding `device_code` | `POST /api/browser/v1/pairings/poll` `{device_code}` | `{status}`: `pending`, `slow_down`, `denied`, `expired`, or `approved` with `{token, token_id, label}` |

- **Lifetime.** A pairing lives 10 minutes in this process's memory. A restart
  cancels it; the extension connects again. At most 64 are pending.
- **Secrets.** The `device_code` (256 bits) is the only proof that collects the
  token. It is held as a SHA-256 key, never raw, and never shown to the person.
  The `user_code` (`XXXX-XXXX`, from an unambiguous alphabet) only identifies
  the request to the approving person, who sees it beside the code the
  extension shows.
- **Exactly once.** The token is issued at approval, in the same transaction
  as its journal event, and handed to the first poll that follows. The pairing
  is then deleted, and a later poll answers `expired`. A token approved but
  never collected is revoked when its pairing expires. A code that never
  existed and one that expired answer alike.
- **Pace.** Polls closer than `interval` (2 s) answer `slow_down`.
- **Limits.**
  - Starting: 10 per peer and 64 installation-wide per 10 minutes.
  - Viewing and answering: 30 per person per 10 minutes.
  - Over a limit: `429`, `browser.pairing.rate_limited`.

## 2. Browser tokens and their fence

- **Who.** Only an active Admin or User account holds a browser token
  (`BROWSER_ROLES`). ReadOnly and Integration accounts may not approve a pairing.
- **Authority.** A browser token's principal holds exactly one capability,
  `browser.client`, **whatever its account's role**: a browser on an Admin
  account is still only a browser client. No role holds `browser.client`, so no
  session reaches the browser-client API, and no browser token reaches
  anything else (`403`). The download-client dialects refuse a browser token
  as an unknown key.
- **Re-checked on every request.** `resolve_token` re-reads the account, so a
  person demoted to ReadOnly, deactivated, merged or removed stops every browser
  of theirs at once.
- **Provenance.** A browser token is its own client (`client_id` = its own id),
  attributed to its person and its name.
- **The first route.** `GET /api/browser/v1/session` →
  `{protocol: "downloadpulse.browser/1", token: {id, label}, account: {display_name}}`.
  It is how the extension verifies its pairing and the server's protocol, and it
  names what this server offers a browser: `capabilities: {sources, controls,
  withdraw, estimate}`.
- **The person's appearance.** The same answer carries `appearance: {mode,
  dark_palette, light_palette}`: the token's person's own choice, read from its
  one owner (`services.account_appearance.load`; a retired palette reads as its
  mode's native one), as palette IDs. The extension draws its own pages in it
  and carries the palettes' colours itself; an ID it does not know is drawn as
  that mode's native palette. A change in DownloadPulse is the extension's next
  read.

## 2a. A browser's downloads

A browser's submission is that person's own download, admitted exactly as
Quick Add admits it: same core paths, same demand attribution (the person
and this browser's token), into their normal Downloads. Its job belongs to
the one external-client owner (`external/service.py`), as a **browser** job
(`external_jobs.kind`). It has no category, delivery profile, delivery or
acknowledgement. Everything else is the external protocol's, unchanged:
client-scoped idempotency, the change-cursor listing, isolation, demand-only
withdrawal and the per-client limits. Its pause and resume are its person's own (below), not the advisory hold an Integration gets.

| Route | Does |
|---|---|
| `POST /api/browser/v1/jobs` `{idempotency_key, source, display_name?, client_reference?, cookies?, referrer?}` | Submit. `201` new, `200` the same key and intent again; another intent under that key is `409 idempotency_conflict`. `cookies` and `referrer` (links only) are the submission's request context (below); neither is part of the intent. |
| `GET /api/browser/v1/jobs?after&limit&phase` | This browser's jobs by change cursor. |
| `GET /api/browser/v1/jobs/{id}` | One of them; another client's is `404`. |
| `POST /api/browser/v1/jobs/{id}/withdraw` | Remove: withdraw this browser's request. The download stops only if nobody else needs it; finished files are kept. |
| `POST /api/browser/v1/jobs/{id}/controls` `{kind: pause\|resume, request_key}` | The person's own Pause and Resume of the download this job is served by (a joined job's shared transfer), as in DownloadPulse: honoured whoever else's request shares it and whoever paused it (operator, 2026-11-22). Declined only while everything is paused (`globally_paused`), once settled (`already_settled`) or withdrawn (`job_withdrawn`). The pause is an operator pause, not a client hold; `paused_by` is `client` while the standing pause is still the one this browser applied. |

- **Sources.** `links` (1–100), `magnet`, `nzb_link`, `nzb`, `torrent`,
  `metalink` (a `.meta4` file, as DownloadPulse's own upload takes it).
  - A media page is a link: core's routing decides, as for Quick Add.
  - A link that needs an item-or-collection answer is refused
    (`acquisition_scope_required`).
- **Private-LAN links** follow the installation's **Local Network Connections**
  and **Skip Local Connection Confirmation** settings, exactly as for any
  submitter that cannot answer the confirmation. Otherwise they are refused
  (`local_network_confirmation_required`). A browser never consents.
- **Request context (1.0.2).** A links submission may carry `cookies` (the browser's
  cookies for the links' own site, each `{name, value, domain, path, secure, http_only,
  host_only, expires}`) and `referrer` (the page the download was started from). Core holds
  them for that job and hands each integration only what applies to the address it fetches
  (UNIVERSAL_TRANSFER_CORE.md, "Submission request context"): a file behind a sign-in, a
  host that checks the referrer and a video only its signed-in viewer can watch all work the
  same way. The session's `capabilities.cookies` and `capabilities.referrer` say they are
  taken (`sources: ["links"]`, with their bounds); a cookie for any other site, a non-links
  source or a malformed value is `422 invalid_request` naming the field, never a value. Too
  many jobs holding context at once is `503 cookie_capacity` (retryable).
- **The job view:** `id`, `revision`, `cursor`, the timestamps, `display_name`,
  `client_reference`, `name` (the transfer's title as DownloadPulse shows it:
  the Downloads list's derived display name, so a media download's resolved
  title replaces its link's placeholder; a change moves the cursor), `source`
  (a summary without query, credentials or payload), `phase`, `reason`, `actionability`, `paused_by`, `error`,
  `progress`, `estimate` and `control`. A completed download is settled.

## 2b. Open DownloadPulse (the desktop application)
`POST /api/browser/v1/application/show` (a browser token; nothing else is
admitted). On the installed desktop application it asks its supervisor to bring
the window forward -- exactly the tray's Open: a window closed to the tray is
shown again, a minimized one asked back, a shown one raised, and a released one
launched again -- and answers `{"desktop": true}`. On Linux, where the tray is
the supervisor's own, a window hidden in the tray for ten minutes is released
(its native shell ended to free its memory) once its own page answers that
nothing would be lost; Open then launches it again within about a second, back
at its size, its page, the state it held and still signed in (a run-scoped
cookie store), with a note of what happened while it was away. A relaunch that
fails says so in a notification and leaves DownloadPulse running; the next Open
tries again. On Windows and macOS, whose tray is the native shell's own, the shell stays: the released window's page leaves for an empty parked page instead (freeing what the application's page held), and Open sends it back to the same state, with the same note. The request is a flag on the backend's private control
channel (`application.desktop_control`, op `show`), which the supervisor reads
every half second; asked twice before it looks is shown once. Whether the
window may take the focus is the operating system's decision (Windows and some
Wayland desktops may flash the taskbar instead). A server has no window: it
answers `{"desktop": false}` and changes nothing, and the extension opens the
page in a tab, as it does for an older DownloadPulse (`404`).

## 2c. The server's identity (1.0.8)

Whatever listens at the address an extension knows (another program on
`127.0.0.1:8095` while DownloadPulse is stopped, on a shared computer) would
otherwise receive the browser token and, for a video or a captured download,
the person's sign-in on that site. A server that advertises
`capabilities.identity: 1` in its session answer proves it is the one that
issued the token, before the extension sends the token again:

`POST /api/browser/v1/identity` `{token_id, nonce, origin}` (no credential) →
`{protocol, identity: 1, proof}`, where

```
proof = hex(HMAC-SHA256(key = SHA-256(raw token), message =
        "downloadpulse.browser/identity/1" "\n" nonce "\n" origin))
```

- `nonce`: 22-128 characters of `A-Z a-z 0-9 _ -`, fresh for every check (the
  extension sends 128 random bits). `origin`: the server's address exactly as the
  extension knows it (`http://127.0.0.1:8095`), no path.
- **The key** is the token's own digest -- the `verifier` this server already
  keeps (`accounts.browser_identity_proof`): nothing new is issued at pairing or
  stored, the pairing answer is unchanged, and a browser connected before 1.0.8
  can be proven too. The verifier never leaves the server; the token is never
  sent for the check.
- **Answers.** `404 browser.identity.unknown` for no such browser token (an
  Integration token's ID too). A revoked browser token is still proven: the
  extension then sends the token and hears its own `401`, as before.
  `422 browser.identity.invalid` for a malformed nonce or origin.
- **Limits** (`429 browser.identity.rate_limited`). Proofs: 600 per browser token
  per 10 minutes, counted for that token alone. Refusals (malformed, unknown token
  ID): 60 per peer and 6000 installation-wide per 10 minutes. Behind a reverse
  proxy, or through the desktop's Local Port, every browser shares one peer, so a
  stranger's flood is never counted against a real browser's proofs: refusing one
  needs its token ID, a random UUID.
- **What the extension does** (1.0.8): once a session answer said
  `identity: 1`, it checks before the first request carrying that token every
  30 seconds or after any failed request, and again before every send carrying
  a sign-in. A failed check sends nothing (the server counts as unreachable).
  It sends a sign-in to DownloadPulse on this computer only once that has
  proven itself. An extension that does not know the route never calls it; a
  server that never advertised it keeps working as before, except that no
  sign-in goes to an unproven loopback address.
- **What it does not cover.** The origin is the extension's claim: the server
  does not check it against its own address (a reverse proxy or a container's
  port mapping would make that wrong). A listener that can itself reach the
  real server could relay a check. Over HTTPS the certificate already proves
  the server; the check matters for plain HTTP, above all on this computer.

## 3. Disconnecting

| Who | Route |
|---|---|
| The person, their own browsers | `GET /api/account/browsers`; `DELETE /api/account/browsers/{token_id}` |
| The browser itself (the extension's Disconnect) | `DELETE /api/browser/v1/session`: the token carrying the request is revoked, journaled as by its owner |
| An Admin, anyone's | `GET /api/accounts/{id}/tokens` (each row names its `kind`); `DELETE /api/accounts/{id}/tokens/{token_id}` |

Revocation is immediate (`401` on the next call). The work the browser sent is
untouched. A person's route finds only their own browser tokens: another
account's token, or an Integration token, is `404`. Nobody creates a browser
token for someone else; only the person's own approval does.

The journal records `administration.browser_connected` (account, token id and
name, requesting address) and `administration.browser_disconnected` (by its
owner or by an Admin). No event holds a secret.

## 4. Origins

An extension's background sends its own origin (`chrome-extension://…`,
`moz-extension://…`). The general web-security boundary admits such an origin
**only under `/api/browser/v1/`**, where no route accepts an ambient
credential. Pairing start and poll take no credential, and every other route
there admits only a browser token sent as `Authorization: Bearer`. A web
page cannot claim an extension origin, and outside this prefix an extension
origin is refused as any foreign origin is. The person-facing routes live
under `/api/account/` and keep every ordinary protection: session, CSRF and
same origin.

An extension must call this server with `credentials: "omit"`. If a session
cookie rides along, the session is what authenticates the request, and it is
refused here (`403`).
