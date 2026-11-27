# DownloadPulse Release and Repository Operations

This document is the authoritative operational runbook for DownloadPulse source promotion and release work.

## Source-of-truth boundary

Source changes, branch management, promotion and release preparation are performed against the source repository — either through the connected GitHub tooling or in a clone of the repository dedicated to development.

**The deployment/validation host is not that workspace.** It may have a Compose project directory, but that directory is a **runtime/deployment surface**, not the development source-of-truth. Do not run `git tag`, `git push`, branch-management commands, or source-development commands there merely because a shell is available.

Use shell commands on the deployment host only for things that actually require the running installation: pulling a candidate image, changing the Compose image reference, recreating the service, querying health, controlled failure injection, filesystem inspection, and other local acceptance tests.

## Normal release flow — exact-SHA promotion

Releases are promoted by **exact SHA**, not by merging a pull request. `main` is
fast-forwarded to the exact commit that hosted CI already qualified, so the commit,
the tree, the tag and the published image all name the same artifact.

1. Develop and qualify on the release branch.
2. Re-read changed files/diffs after writes. Verify that only intended files changed.
3. Freeze and approve an exact source tree/SHA. Any later source change creates a new
   candidate and requires requalification from zero.
4. Push the exact approved release-branch commit.
5. Let the required hosted workflows qualify **that exact SHA**: Tests, Browser Runtime,
   CodeQL, Image, Container Security, Candidate Runtime Qualification (and
   WS3 Adversarial on branches that run it). Do not rerun until green; classify a failure
   with the candidate-vs-anchor policy in `docs/internal/QUALIFICATION_DETERMINISM.md`.
6. Prefer the immutable candidate image (`sha-<full-git-sha>`) for local runtime validation,
   and verify the OCI label `org.opencontainers.image.revision` exactly equals the expected
   full candidate SHA before replacing a deployed image.
7. Perform the final audit against the frozen branch state.
8. When the candidate is accepted as DONE, promote that exact release commit/tree to `main`
   by fast-forward:

   ```bash
   git push origin <FULL_RELEASE_SHA>:refs/heads/main
   ```

   **Promotion must not introduce source changes.** No merge commit, no squash, no rebase,
   no edit — anything that changes the tree produces a SHA that nothing has qualified, and
   the promotion gate will refuse it. `main`'s resulting tree must equal the accepted
   release tree exactly.
9. `latest` is promoted from the already-qualified immutable `sha-<full-git-sha>` digest by
   the Release Promotion workflow. It is **never rebuilt** as a separate release artifact.
10. Create the version tag so it resolves to the exact final release commit, and verify the
    version image tag resolves to the same qualified immutable manifest digest.
11. Create the GitHub Release from the canonical bracketed changelog entry for that version
    (`python3 release.py X.Y.Z` publishes exactly that entry; it refuses an entry still titled
    `Development` and a body over GitHub's 125,000-character limit before sending anything, never
    truncates, and exits non-zero on any failed GitHub call). A body that does not fit is a
    changelog decision for the operator, not something to cut silently.
12. The release is closed only after every identity check agrees (see *Release identity*).

### Why not a PR merge

`release-promotion.yml` resolves the candidate as `$GITHUB_SHA` on a push to `main` and
requires every required workflow to be **completed and successful for that exact SHA**,
polling and then failing closed. A merge commit is a new SHA that nothing has qualified,
so it would either fail the gate or force a requalification that promotes a *different*
image digest than the one that passed every gate. Expected-head PR merging is therefore no
longer the canonical release path.

## Release identity

Before publication, verify all release surfaces agree:

- `VERSION`
- top `CHANGELOG.md` entry
- README/install examples
- Compose example image tag
- PR title/body and release notes
- OCI image `version` and `revision` labels

While the top `CHANGELOG.md` entry is titled `Development`, the README and Compose install
examples must keep naming the newest *released* entry's image (for example `v1.0.0` while
`[1.0.1] — Development` is open); the documentation contract tests refuse an image reference to
the development `VERSION` in the README or Compose example. Releasing therefore changes these
surfaces together in one release-identity commit: the top entry's title no longer says
`Development` (it takes the release's title), and the README and Compose image
references move to the new version. That commit is the release candidate, and it is the one whose
exact SHA must pass every required workflow.

Do not reuse an existing Git tag. Release tags are never deleted or rewritten: a fix after a release is a new version.

## Preferred GitHub operations

Use direct connector primitives whenever they exist:

- file/source writes: GitHub contents operations, or ordinary commits in a local clone of the source repository
- promotion: fast-forward push of the exact qualified release SHA to `main`
- tag/release: annotated tag at the exact release commit, then a GitHub Release built from the canonical changelog entry
- CI inspection: commit workflow runs / job logs
- branch/ref inspection: repository/branch/ref operations

Do not conclude that an operation is impossible merely because a narrowly filtered tool discovery did not expose it. If necessary, discover the full GitHub tool surface first.

## Remote Actions fallback for missing repository primitives

When the connector does not expose a required GitHub-side primitive (for example tag creation, GitHub Release creation, workflow dispatch in a needed form, or branch-ref deletion), **do not hand the operation back to the user and do not invent a local repo**.

Use the established one-shot remote Actions pattern.

### Pattern

1. Create a temporary branch such as `release-ops/vX.Y.Z` from the **exact already-merged release commit**.
2. Modify an already-registered workflow **only on that temporary branch** so a push to the temporary branch starts the one-shot job. This avoids modifying `main`.
3. Grant the one-shot job only the permissions it needs, normally:
   - `contents: write`
   - `packages: write` when publishing GHCR
4. Make the job fail closed before any destructive operation. Verify at minimum:
   - `main` still equals the expected merge SHA;
   - `VERSION` equals the intended release version;
   - the staging branch still equals the exact locally accepted candidate SHA;
   - any pre-existing release tag either does not exist or already points at the expected release commit.
5. Checkout the **exact release commit SHA**, not the temporary workflow commit, for build/publication work.
6. Perform the missing GitHub operations with the runner's authenticated `gh api` / GitHub token.
7. Verify each created ref/resource before proceeding to deletion.
8. Delete the staging branch only if it still points at the accepted SHA.
9. Delete the temporary release-operations branch as the final success-gated action.
10. Independently verify afterward that:
    - the tag resolves;
    - the GitHub Release exists;
    - the release image exists with the expected OCI revision;
    - the staging branch is absent;
    - the temporary operations branch is absent;
    - `main` was not altered by the helper.

### Important GitHub Actions behavior

A tag or ref created with the repository `GITHUB_TOKEN` does **not** reliably trigger another workflow from that generated event. Therefore, a one-shot release runner must not assume that creating `vX.Y.Z` will cause the normal tag-publish workflow to run.

If the release image tag must be moved in the same operation, the one-shot runner dispatches Release Promotion for the exact release commit (`gh workflow run release-promotion.yml -f candidate_sha=<FULL_RELEASE_SHA> -f target_tag=vX.Y.Z -f dry_run=false`, which needs `actions: write`; a `workflow_dispatch` made with `GITHUB_TOKEN` does start the workflow), or uses another authenticated mechanism whose events are intentionally allowed to trigger the normal workflow. A one-shot runner never builds or pushes an application image itself: a release image is only ever the already-qualified `sha-<full-git-sha>` digest, whose build provenance must be signed by `image.yml` from a qualifying ref (`docs/SUPPLY_CHAIN_POLICY.md` §1, "Build provenance"). A `release-ops/*` branch is never a qualifying ref, and none of its runs or signatures counts toward qualification.

Never create a branch named like a tag as a substitute for a real tag.

## Release-image contract

For a public release image:

- build from the exact merged release commit;
- publish the versioned tag, e.g. `ghcr.io/xipher-zero/downloadpulse:vX.Y.Z`;
- include multi-arch targets expected by the permanent release workflow;
- preserve SBOM/provenance generation (Image's `attest` job alone signs the build provenance of the digest `publish` pushed; its `provenance` job verifies it);
- run the build tools' privileged images by digest: re-resolve the binfmt and BuildKit index digests the workflows pin, and their tag comments, at the release cut (`docs/SUPPLY_CHAIN_POLICY.md` §1, "Build tool images by digest");
- set OCI labels including:
  - `org.opencontainers.image.version=X.Y.Z`
  - `org.opencontainers.image.revision=<full release commit SHA>`
  - `org.opencontainers.image.source=https://github.com/Xipher-Zero/downloadpulse`
  - `org.opencontainers.image.licenses=GPL-2.0-or-later`

The OCI revision is the authoritative check when moving a deployment from a candidate SHA image to the published release tag.

The image also contains the backend test tree at `/app/tests`: the Dockerfile copies `backend/` whole, and `.dockerignore` does not exclude it. This is deliberate. The production application never imports it, and the container's default command (`uvicorn main:app`) does not use it. Its one runtime purpose is release qualification: the Browser Runtime workflow starts the exact candidate image in an additional container as `uvicorn browser_contract_app:app --app-dir /app/tests`. That test-only composition (`backend/tests/browser_contract_app.py`) serves the real application with an in-memory transport, so that browser checks can raise real `INPUT_REQUIRED` challenges and remote collections against the image that is being qualified rather than a rebuilt one.

## Release assets (the update notice relies on them)

DownloadPulse's update notice (`backend/application/updates.py`) reads the GitHub release that
`releases/latest` names — never a draft or a pre-release — and, in the desktop applications,
offers to download this computer's installer through DownloadPulse, verified against the SHA-256
GitHub states for that asset. The notice shows, in the app, the body of every published release
newer than the one running (`releases`, at most 10, newest first): each release body is the
CHANGELOG entry `release.py` publishes, so it is written for the people updating. A release therefore
carries, attached by the operator (a manual step: `release.py` publishes only the release body):

| Asset | Platform |
|---|---|
| `DownloadPulse-X.Y.Z-windows-x64.exe` | Windows desktop |
| `DownloadPulse-X.Y.Z-macos-arm64.dmg` | macOS desktop |
| `DownloadPulse-X.Y.Z-linux-x86_64.flatpak` | Linux desktop (offered only inside the Flatpak) |
| `SHA256SUMS` | the SHA-256 of each file above, for people who download by hand |

- The names are exact: `X.Y.Z` is the tag without its `v`. A desktop finds its installer by that
  name alone; any other name is not offered.
- The hash the desktop verifies is the asset's own GitHub `digest` (`sha256:…`), which GitHub
  computes on upload. An asset without one is not offered (the notice then only says that the release exists);
  `SHA256SUMS` is never read by the application.
- The download address is the asset's `browser_download_url` on `github.com` under
  `/Xipher-Zero/downloadpulse/releases/download/`; any other address is refused.
- An installer is downloaded as an ordinary transfer and is never started by DownloadPulse: once
  verified, the person opens it from Show in folder. A server installation never downloads
  anything; it only says that a newer release exists.
- The Windows installer updates a running DownloadPulse: it asks, closes it as its own Quit does
  (the named event `Local\DownloadPulse-Quit`, `desktop/supervisor.py` `QUIT_EVENT`), waits for
  the data root's lock, replaces the program files and starts it again. A DownloadPulse that does
  not close (one older than 1.0.7, or one that is stuck) is the person's to Quit: setup offers Retry.

## Local deployment / acceptance rule

The deployment host is for **runtime testing**, not repository administration.

For candidate or release deployment:

1. Pull the requested image before changing Compose.
2. Inspect `org.opencontainers.image.revision` and require exact equality with the expected SHA.
3. Back up the Compose file.
4. Change only the image reference.
5. Run `docker compose config --quiet`.
6. Recreate only the DownloadPulse service.
7. Inspect container image/status/health.
8. Query the internal `/api/health` endpoint.
9. Restore the previous Compose file/image if validation or recreation fails.

For staging candidates, use immutable `sha-<full-git-sha>` images. After release publication, move production to the version tag only after verifying the version tag's OCI revision equals the promoted `main` release commit.

## Promotion gate

A release is ready for promotion only when all applicable gates are green:

- source/architecture audit
- functionality/result-authority audit
- security audit
- V1 scope audit
- license/provenance audit
- release-surface/version audit
- permanent CI/security/image matrix
- exact-head local behavioral acceptance
- the Flatpak metainfo's screenshot and `<url>` addresses opened once by hand (CI validates it with `appstreamcli validate --no-net`, which never fetches them)

Behavioral acceptance should exercise the subsystems materially changed by the release. A green unit suite is not a substitute for the relevant real runtime path.

## Closeout checklist

After release publication:

- `main` verified at the exact qualified release SHA, with a tree identical to the accepted release tree;
- release tag verified at that same commit;
- GitHub Release published from the canonical changelog entry;
- GHCR version image published and OCI revision verified;
- `latest` and the version tag resolve to the same immutable qualified manifest digest;
- temporary release-operations branch/workflow changes absent;
- final local deployment moved from candidate SHA tag to the public version tag when appropriate;
- acceptance evidence preserved in the release record.

If a future session cannot remember how a repository-side operation was performed, **read this document before asking the operator to perform GitHub administration manually**.
