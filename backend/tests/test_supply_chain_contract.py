"""DP 1.0.12 leveling remediation (DEP-001): qualified-artifact authority and
build-input hardening contract.

Source-level assertions against the Dockerfile, the runtime Python lock, and
the supply-chain policy document. Does not build the image or touch a
registry -- Image / Container Security / Candidate Runtime
Qualification remain the authority for the actual built artifact (see
docs/SUPPLY_CHAIN_POLICY.md).
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

_DOCKERFILE = (ROOT / "Dockerfile").read_text()
_REQUIREMENTS = (ROOT / "backend" / "requirements.txt").read_text()
_POLICY = (ROOT / "docs" / "SUPPLY_CHAIN_POLICY.md").read_text()

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def test_dockerfile_base_image_is_pinned_by_verified_manifest_digest():
    match = re.search(
        r"^FROM\s+python:3\.12\.14-slim-trixie@(sha256:[0-9a-f]{64})\s*$",
        _DOCKERFILE, re.MULTILINE,
    )
    assert match, "Dockerfile FROM line must pin python:3.12.14-slim-trixie by sha256 digest"
    assert _DIGEST_RE.fullmatch(match.group(1))


def test_dockerfile_does_not_run_a_blanket_apt_upgrade():
    assert "apt-get upgrade" not in _DOCKERFILE
    assert "apt-get update" in _DOCKERFILE


def test_dockerfile_uses_no_install_recommends_for_apt():
    assert "--no-install-recommends" in _DOCKERFILE


def test_dockerfile_installs_python_deps_with_hash_enforcement():
    assert re.search(r"pip install[^\n]*--require-hashes", _DOCKERFILE)
    assert "requirements.txt" in _DOCKERFILE


def test_runtime_requirements_are_hash_pinned_for_every_package():
    """Every top-level pinned requirement (``name==version``) must carry at
    least one ``--hash=sha256:...`` entry, or ``--require-hashes`` would
    refuse the whole install at build time."""
    lines = _REQUIREMENTS.splitlines()
    package_line_re = re.compile(r"^[A-Za-z0-9_.\-]+==[^\s;\\]+")
    packages = [i for i, line in enumerate(lines) if package_line_re.match(line)]
    assert packages, "expected at least one pinned package in requirements.txt"
    for index in packages:
        # Hash continuation lines for this package run until the next
        # unindented package line (or a "# via ..." comment / EOF).
        window = []
        for line in lines[index:index + 400]:
            if line.startswith("    --hash=sha256:"):
                window.append(line)
            elif window:
                break
        assert window, f"requirements.txt package at line {index + 1} has no --hash entries: {lines[index]!r}"


# --- the bundled Usenet acquisition service (1.0.13) ----------------------
#
# The service runs on the image's own interpreter, so its Python runtime
# closure is part of the shipped runtime and must obey the same lock policy.
# A second install transaction would escape --require-hashes AND be free to
# replace packages the locked one already selected, leaving two contradictory
# package authorities in one environment.

_REQUIREMENTS_IN = (ROOT / "backend" / "requirements.in").read_text()

# What the service actually needs at runtime on Linux, and what it must never
# drag into the shipped image. Both taken from SABnzbd-5.1.3/requirements.txt.
_SERVICE_RUNTIME_PACKAGES = (
    "sabctools", "cheroot", "cherrypy", "feedparser", "configobj", "apprise",
    "guessit", "rebulk", "babelfish", "rarfile", "puremagic", "portend",
    "tempora", "zc-lockfile", "ct3", "sgmllib3k", "hachoir", "ujson", "orjson",
)
_SERVICE_TEST_ONLY_PACKAGES = (
    "selenium", "tavern", "tavalidate", "pyfakefs", "flaky", "pytest-httpbin",
    "pytest-httpserver", "black", "flask", "werkzeug", "xmltodict",
)


def _locked_packages(text):
    return {m.group(1).lower() for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==", text, re.M)}


def test_the_image_runs_exactly_one_python_install_transaction():
    """One lock, one install. Two would be two package authorities."""
    installs = re.findall(r"pip install[^\n\\]*", _DOCKERFILE)
    real = [i for i in installs if "--dry-run" not in i]
    assert len(real) == 1, f"expected exactly one pip install transaction, found: {real}"
    assert "--require-hashes" in real[0]


def test_the_image_never_installs_the_services_own_requirements_file():
    """That file is unhashed, and it also carries the service's test tooling."""
    assert not re.search(r"pip install[^\n]*/app/usenet/requirements\.txt", _DOCKERFILE)
    assert not re.search(r"pip install(?![^\n]*--require-hashes)", _DOCKERFILE)


def test_the_service_runtime_closure_is_in_the_shipped_lock():
    locked = _locked_packages(_REQUIREMENTS)
    missing = [name for name in _SERVICE_RUNTIME_PACKAGES if name not in locked]
    assert not missing, f"the bundled service needs these at runtime, unlocked: {missing}"


def test_the_service_test_tooling_is_not_shipped_in_the_runtime_lock():
    locked = _locked_packages(_REQUIREMENTS)
    shipped = [name for name in _SERVICE_TEST_ONLY_PACKAGES if name in locked]
    assert not shipped, f"test-only packages leaked into the runtime image: {shipped}"


def test_the_service_runtime_closure_is_declared_in_the_canonical_input():
    """Not merely resolved by accident: the canonical input names them, so a
    later recompile cannot quietly drop the service's dependencies."""
    declared = _locked_packages(_REQUIREMENTS_IN)
    for name in ("sabctools", "cherrypy", "feedparser", "configobj", "apprise"):
        assert name in declared, f"{name} is not declared in requirements.in"


def test_the_service_source_tarball_stays_checksum_pinned():
    assert re.search(r"^ARG USENET_SERVICE_VERSION=\d+\.\d+\.\d+$", _DOCKERFILE, re.M)
    assert re.search(r"^ARG USENET_SERVICE_SHA256=[0-9a-f]{64}$", _DOCKERFILE, re.M)
    assert "sha256sum -c -" in _DOCKERFILE


def test_the_repair_and_licensing_material_is_retained():
    """par2 does the posting's verification/repair; unrar is required for the
    service to start acquiring, and its licence file must ship with it."""
    assert re.search(r"^\s*par2 \\$", _DOCKERFILE, re.M)
    assert re.search(r"^\s*unrar \\$", _DOCKERFILE, re.M)
    assert "path-include=/usr/share/doc/unrar/copyright" in _DOCKERFILE


def test_supply_chain_policy_document_exists_and_states_the_authority_contract():
    assert "authoritative release artifact" in _POLICY.lower()
    assert "manifest" in _POLICY.lower() and "digest" in _POLICY.lower()
    assert "sha-<40-char-git-sha>" in _POLICY


def test_supply_chain_policy_does_not_claim_bit_identical_rebuilds():
    lowered = _POLICY.lower()
    assert "not assumed" in lowered or "not guarantee" in lowered or "does not claim" in lowered
    assert "bit-identical" in lowered or "reproducib" in lowered


def _workflow(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text()


def _step(workflow: str, name: str) -> str:
    """Text of one workflow step, from its ``- name:`` line to the next step."""
    marker = f"- name: {name}"
    assert workflow.count(marker) == 1, f"expected exactly one step named {name!r}"
    start = workflow.index(marker)
    following = workflow.find("\n      - name:", start + len(marker))
    return workflow[start:] if following == -1 else workflow[start:following]


def test_image_workflow_publishes_only_the_full_sha_tag():
    workflow = _workflow("image.yml")
    # Exactly one tag rule, and it is the full source SHA -- never a truncation.
    assert workflow.count("type=raw,value=sha-${{ github.sha }}") == 2
    assert "type=sha" not in workflow
    assert "format=short" not in workflow
    assert "GITHUB_SHA:0:7" not in workflow
    assert "latest=false" in workflow
    # The publish job's own runtime guard: the one generated tag must equal the
    # full-SHA candidate ref, and may never be a mutable latest/version tag.
    assert '"${generated_tags[0]}" != "${IMAGE_NAME}:sha-${GITHUB_SHA}"' in workflow
    assert "^[0-9a-f]{40}$" in workflow
    assert '":latest"' in workflow and '":v"' in workflow


def test_image_checks_for_an_existing_candidate_before_publishing():
    workflow = _workflow("image.yml")
    check = workflow.index("- name: Check for an existing candidate for this source SHA")
    build = workflow.index("- name: Build and publish")
    assert check < build, "the existence check must precede the build/push step"

    existing = _step(workflow, "Check for an existing candidate for this source SHA")
    assert "docker buildx imagetools inspect" in existing
    # An inconclusive registry answer must never be read as "absent".
    assert "Unable to determine whether" in existing
    assert "denied|unauthorized|forbidden" in existing

    publish = _step(workflow, "Build and publish")
    assert "if: steps.existing.outputs.exists != 'true'" in publish
    assert "push: true" in publish
    # Exactly one push in the whole workflow, and it is the guarded step.
    assert workflow.count("push: true") == 1


def test_image_reuses_a_valid_existing_candidate_without_overwriting_it():
    workflow = _workflow("image.yml")
    existing = _step(workflow, "Check for an existing candidate for this source SHA")
    assert 'echo "exists=true" >> "$GITHUB_OUTPUT"' in existing
    assert 'echo "digest=$digest" >> "$GITHUB_OUTPUT"' in existing
    assert "will be reused unchanged" in existing
    # A reuse path performs no build and no push of its own.
    assert "build-push-action" not in existing
    assert "imagetools create" not in existing
    assert "docker push" not in existing

    selected = _step(workflow, "Select the immutable candidate digest")
    assert 'if [ "$EXISTING" = "true" ]' in selected
    assert "An existing candidate was reused but a build also reported a digest" in selected

    # Later verification is keyed on the selected digest, for both paths.
    verify = _step(workflow, "Verify candidate digest and OCI identity")
    assert "steps.candidate.outputs.digest" in verify
    assert "steps.publish.outputs.digest" not in verify


def test_image_fails_closed_when_an_existing_tag_records_another_revision():
    workflow = _workflow("image.yml")
    existing = _step(workflow, "Check for an existing candidate for this source SHA")
    assert 'existing_revision" != "$GITHUB_SHA"' in existing
    assert "refusing to reuse or overwrite" in existing
    revision_check = existing.index('existing_revision" != "$GITHUB_SHA"')
    assert "exit 1" in existing[revision_check:revision_check + 400]
    # Reuse still requires both platform children to be present.
    assert 'for arch in amd64 arm64' in existing


def test_image_serializes_runs_for_the_same_source_sha():
    workflow = _workflow("image.yml")
    head = workflow[:workflow.index("\njobs:")]
    assert "concurrency:" in head
    assert "group: image-${{ github.sha }}" in head
    # Never cancel a publication that is already in flight.
    assert "cancel-in-progress: false" in head
    assert "cancel-in-progress: true" not in workflow


def test_no_candidate_consumer_or_promotion_step_truncates_the_source_sha():
    for name in (
        "image.yml",
        "container-security.yml",
        "candidate-runtime-qualification.yml",
        "release-promotion.yml",
    ):
        text = _workflow(name)
        assert "GITHUB_SHA:0:7" not in text, name
        assert "candidate_sha:0:7" not in text, name
        assert "format=short" not in text, name
    for name in ("container-security.yml", "candidate-runtime-qualification.yml"):
        assert 'source_ref="${IMAGE_NAME}:sha-${GITHUB_SHA}"' in _workflow(name), name
    promotion = _workflow("release-promotion.yml")
    assert 'source_tag=sha-${candidate_sha}' in promotion
    assert "^[0-9a-f]{40}$" in promotion


def test_supply_chain_policy_states_the_write_once_full_sha_rule():
    lowered = _POLICY.lower()
    assert "write-once" in lowered
    assert "sha-<40-char-git-sha>" in _POLICY
    assert "sha-<short7>" not in _POLICY
    assert "reuses" in lowered and "fails closed" in lowered
    assert "new candidate requires a new source sha" in lowered
    assert "cancel-in-progress: false" in _POLICY
    # The policy must not both promise a write-once tag and treat a same-SHA
    # rebuild as a fresh candidate.
    assert "a rebuild is a new candidate" not in lowered


def test_release_promotion_remains_digest_preserving_no_rebuild():
    workflow = (ROOT / ".github" / "workflows" / "release-promotion.yml").read_text()
    assert "imagetools create" in workflow
    assert re.search(r"docker\s+build\b(?!x)", workflow) is None


# -- Signed build provenance and least privilege on the candidate chain --------------------------
#
# Signed build provenance and least privilege on the candidate image chain.
#
# ``Image`` signs SLSA build provenance for the digest it pushes; ``Image``
# (before any reuse), ``Container Security``, ``Candidate Runtime
# Qualification`` and ``Release Promotion`` accept a candidate only once that
# provenance verifies as signed by ``image.yml`` for the exact commit from a
# qualifying ref, and each qualification attestation only as signed by its own
# workflow. Promotion matches required runs by workflow file and push event,
# across every page. The qualifiers run read-only except for the one job that
# signs; the Docker Hub token leaves the runner before any test code runs
# (docs/SUPPLY_CHAIN_POLICY.md 1). Source-level checks only: nothing is fetched.

WORKFLOWS = ROOT / ".github" / "workflows"
CANDIDATE_CHAIN = ("image.yml", "container-security.yml", "candidate-runtime-qualification.yml",
                   "release-promotion.yml")
QUALIFIERS = ("container-security.yml", "candidate-runtime-qualification.yml")
PROVENANCE_ACTION = "actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8 # v4.2.2"


def _text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _yaml(name: str) -> dict:
    return yaml.safe_load(_text(name))


def _steps(name: str, job: str) -> dict:
    return {step["name"]: step for step in _yaml(name)["jobs"][job]["steps"]}


def _qualifying(pattern: str, ref: str) -> bool:
    return re.fullmatch(f"({pattern})", ref) is not None


def test_one_qualifying_ref_set_is_spelled_identically_across_the_chain():
    patterns = {name: _yaml(name)["env"]["QUALIFYING_REFS"] for name in CANDIDATE_CHAIN}
    assert len(set(patterns.values())) == 1, patterns
    pattern = patterns["image.yml"]
    for ref in ("refs/heads/main", "refs/heads/1.0.8", "refs/heads/1.0.14", "refs/heads/staging/1.0.8",
                "refs/tags/v1.0.8", "refs/tags/internal-v1.0.8-rc1"):
        assert _qualifying(pattern, ref), ref
    for ref in ("refs/heads/release-ops/v1.0.8", "refs/heads/feature/x", "refs/heads/fix/x", "refs/heads/chore/x",
                "refs/pull/7/merge", "refs/heads/main2", "refs/heads/1.0.8x", "refs/heads/1.1.0",
                "refs/heads/audit/x", "refs/tags/release-ops", "refs/heads/v1.0.8"):
        assert not _qualifying(pattern, ref), ref
    for name in CANDIDATE_CHAIN:
        assert _yaml(name)["env"]["BUILD_PROVENANCE_PREDICATE_TYPE"] == "https://slsa.dev/provenance/v1", name


def test_image_signs_build_provenance_for_exactly_the_digest_it_pushes():
    text = _text("image.yml")
    assert text.count("actions/attest-build-provenance@") == 1
    assert PROVENANCE_ACTION in text
    # Its own small job (1.0.8), after publish, on the digest publish pushed.
    jobs = _yaml("image.yml")["jobs"]
    assert jobs["attest"]["needs"] == "publish"
    assert jobs["publish"]["outputs"]["pushed_digest"] == "${{ steps.publish.outputs.digest }}"
    steps = _steps("image.yml", "attest")
    assert list(steps) == ["Log in to GHCR", "Attest candidate build provenance"]
    attest = steps["Attest candidate build provenance"]
    assert attest["if"] == "needs.publish.outputs.candidate_reused != 'true'"
    assert steps["Log in to GHCR"]["if"] == attest["if"]
    assert steps["Log in to GHCR"]["uses"].startswith("docker/login-action@")
    assert attest["with"]["subject-name"] == "${{ env.IMAGE_NAME }}"
    assert attest["with"]["subject-digest"] == "${{ needs.publish.outputs.pushed_digest }}"
    assert attest["with"]["push-to-registry"] is True
    # Only the attest job may sign: no other job in Image holds id-token or attestations write
    # (the pull-request path holds no scope at all).
    assert jobs["attest"]["permissions"] == {"contents": "read", "packages": "write", "id-token": "write",
                                             "attestations": "write"}
    for key, job in jobs.items():
        if key != "attest":
            granted = job.get("permissions", {})
            assert granted.get("id-token") != "write" and granted.get("attestations") != "write", key
    assert "permissions" not in jobs["build-and-smoke-test"]


def test_image_requires_verified_provenance_before_any_reuse_or_consumer():
    jobs = _yaml("image.yml")["jobs"]
    provenance = jobs["provenance"]
    # After the signing, read-only, and the one source of the candidate the testing alias follows.
    assert provenance["needs"] == ["publish", "attest"] and "if" not in provenance
    assert provenance["permissions"] == {"contents": "read", "packages": "read", "attestations": "read"}
    assert jobs["testing-alias"]["needs"] == "provenance"
    assert provenance["outputs"] == {key: f"${{{{ needs.publish.outputs.{key} }}}}"
                                     for key in ("candidate_ref", "candidate_digest", "candidate_reused")}
    names = [step["name"] for step in jobs["publish"]["steps"]]
    assert names.index("Select the immutable candidate digest") < names.index("Verify candidate digest and OCI identity")
    step = _steps("image.yml", "provenance")["Verify candidate build provenance"]
    assert "if" not in step, "both the reuse path and the fresh path must verify provenance"
    run = step["run"]
    assert "needs.publish.outputs.candidate_digest" in step["env"]["CANDIDATE_DIGEST"]
    for flag in ('--cert-identity-regex "$signer"', '--source-digest "$GITHUB_SHA"', "--deny-self-hosted-runners",
                 '--predicate-type "$BUILD_PROVENANCE_PREDICATE_TYPE"'):
        assert flag in run, flag
    assert 'signer="^https://github\\.com/Xipher-Zero/downloadpulse/\\.github/workflows/image\\.yml@(${accepted})$"' in run
    assert "$c.sourceRepositoryDigest == $sha" in run and "$c.buildSignerDigest == $sha" in run
    assert "test($refs)" in run and 'runnerEnvironment == "github-hosted"' in run
    # A pre-provenance candidate fails closed with a message saying so; nothing rebuilds over it.
    assert "can never be reused, qualified or promoted" in run and "exit 1" in run
    assert "build-push-action" not in str(step) and "imagetools create" not in run


def test_each_qualifier_verifies_build_provenance_before_publishing_its_identity():
    for name in QUALIFIERS:
        run = _steps(name, "identity")["Wait for and resolve exact published candidate"]["run"]
        assert 'signer="^https://github\\.com/Xipher-Zero/downloadpulse/\\.github/workflows/image\\.yml@(${QUALIFYING_REFS})$"' in run
        for flag in ('--cert-identity-regex "$signer"', '--source-digest "$GITHUB_SHA"', "--deny-self-hosted-runners"):
            assert flag in run, (name, flag)
        assert run.index("gh attestation verify") < run.index('echo "manifest_digest=$manifest_digest" >> "$GITHUB_OUTPUT"')
        assert "cannot be qualified" in run, name


def test_qualifiers_are_read_only_except_the_job_that_signs():
    for name in QUALIFIERS:
        workflow = _yaml(name)
        assert workflow["permissions"] == {"contents": "read", "actions": "read", "packages": "read"}, name
        for key, job in workflow["jobs"].items():
            granted = job.get("permissions", workflow["permissions"])
            writes = {scope for scope, level in granted.items() if level == "write"}
            if key == "evidence":
                assert writes == {"packages", "id-token", "attestations"}, name
                assert any("actions/attest@" in str(step.get("uses", "")) for step in job["steps"]), name
            else:
                assert not writes, (name, key, writes)
        assert workflow["jobs"]["identity"]["permissions"]["attestations"] == "read", name


def test_promotion_counts_only_each_required_workflow_files_push_runs_on_every_page():
    run = _steps("release-promotion.yml", "promote")["Verify all qualification workflows succeeded for exact SHA"]["run"]
    for name, path in (("Tests", "tests.yml"), ("Browser Runtime", "browser-runtime.yml"), ("CodeQL", "codeql.yml"),
                       ("Container Security", "container-security.yml"),
                       ("Candidate Runtime Qualification", "candidate-runtime-qualification.yml"),
                       ("Image", "image.yml")):
        assert f'["{name}"]=".github/workflows/{path}"' in run
    assert "gh api --paginate" in run and "&event=push&per_page=100" in run
    assert ".path == $path" in run and '.event == "push"' in run and "test($branches)" in run
    branches = _yaml("release-promotion.yml")["env"]["QUALIFYING_HEAD_BRANCHES"]
    for branch in ("main", "1.0.8", "staging/1.0.8", "v1.0.8", "internal-v1.0.8"):
        assert re.fullmatch(branches, branch), branch
    for branch in ("release-ops/v1.0.8", "feature/x", "main2", "dependabot/npm/x"):
        assert not re.fullmatch(branches, branch), branch


def test_promotion_pins_every_attestation_to_its_own_signer_workflow():
    run = _steps("release-promotion.yml", "promote")[
        "Verify signed security and runtime attestations target exact source digest"]["run"]
    assert 'local signer="^https://github\\.com/Xipher-Zero/downloadpulse/\\.github/workflows/${workflow}@(${QUALIFYING_REFS})$"' in run
    for flag in ('--cert-identity-regex "$signer"', '--source-digest "$CANDIDATE_SHA"', "--deny-self-hosted-runners"):
        assert flag in run, flag
    for signer, predicate, output in (
        ("image\\.yml", "BUILD_PROVENANCE_PREDICATE_TYPE", "build-provenance.json"),
        ("container-security\\.yml", "SECURITY_PREDICATE_TYPE", "security-attestations.json"),
        ("candidate-runtime-qualification\\.yml", "RUNTIME_PREDICATE_TYPE", "runtime-attestations.json"),
    ):
        assert f"verify_signed '{signer}' \"${predicate}\" {output}" in run
    # The predicate checks read only attestations whose certificate met the policy.
    assert run.index("verify_signed 'candidate-runtime-qualification") < run.index("security-attestations.json >/dev/null")
    assert "can never be promoted" in run


def test_docker_hub_token_leaves_the_runner_before_any_test_code():
    browser = [step["name"] for step in _yaml("browser-runtime.yml")["jobs"]["browser-runtime"]["steps"]]
    logout = browser.index("Log out of Docker Hub")
    assert browser.index("Log in to Docker Hub") < browser.index("Build exact candidate container") < logout
    assert logout == browser.index("Build exact candidate container") + 1
    steps = _yaml("browser-runtime.yml")["jobs"]["browser-runtime"]["steps"]
    first_test_code = min(i for i, step in enumerate(steps) if re.search(r"\bnpm\b|npx|playwright", step.get("run", "")))
    assert logout < first_test_code
    for name in ("image.yml", "browser-runtime.yml"):
        text = _text(name)
        # Every name Docker Hub is stored under is logged out, and a leftover entry is dropped
        # (1.0.8: docker.io alone left https://index.docker.io/v1/ behind on the runners).
        assert "for registry in docker.io index.docker.io https://index.docker.io/v1/; do" in text, name
        assert 'docker logout "$registry"' in text, name
        assert 'if (.auths | type) == "object" then .auths |= with_entries(select(.key | test("docker\\\\.io") | not)) else . end' in text, name

    image = [step["name"] for step in _yaml("image.yml")["jobs"]["publish"]["steps"]]
    assert image.index("Build test image") < image.index("Log out of Docker Hub") < image.index("Smoke test image")
    assert (image.index("Log in to Docker Hub for the multi-architecture build") < image.index("Build and publish")
            < image.index("Log out of Docker Hub after the multi-architecture build"))
    after = _steps("image.yml", "publish")["Log out of Docker Hub after the multi-architecture build"]
    assert after["if"] == "always()"


def test_every_action_in_the_candidate_chain_is_sha_pinned():
    for name in CANDIDATE_CHAIN + ("browser-runtime.yml",):
        for match in re.finditer(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)", _text(name), re.MULTILINE):
            ref = match.group(1)
            if ref.startswith("./"):
                continue
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), (name, ref)


def test_the_build_actions_run_their_privileged_images_by_digest_in_every_workflow():
    """setup-qemu-action runs binfmt privileged and setup-buildx-action starts
    BuildKit; by default both by floating tag. Every use names a multi-arch
    index digest instead (1.0.8), with the tag it was resolved from beside it,
    and the policy says how they are refreshed (Dependabot does not)."""
    binfmt = re.compile(r"image: docker\.io/tonistiigi/binfmt@sha256:[0-9a-f]{64}\n")
    buildkit = re.compile(r"driver-opts: image=docker\.io/moby/buildkit@sha256:[0-9a-f]{64}\n")
    seen = {"qemu": 0, "buildx": 0}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        text = path.read_text(encoding="utf-8")
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                uses = step.get("uses", "")
                if uses.startswith("docker/setup-qemu-action@"):
                    seen["qemu"] += 1
                    assert binfmt.fullmatch(f"image: {step['with']['image']}\n"), (path.name, step)
                elif uses.startswith("docker/setup-buildx-action@"):
                    seen["buildx"] += 1
                    assert buildkit.fullmatch(f"driver-opts: {step['with']['driver-opts']}\n"), (path.name, step)
        assert len(binfmt.findall(text)) == text.count("# tonistiigi/binfmt:qemu-v"), path.name
        assert len(buildkit.findall(text)) == text.count("# moby/buildkit:v"), path.name
        assert "binfmt:latest" not in text.replace("(latest, the action's default)", ""), path.name
    assert seen["qemu"] >= 2 and seen["buildx"] >= 8, seen
    # One digest each across the repository: a refresh moves every use together.
    everything = "".join(path.read_text(encoding="utf-8") for path in WORKFLOWS.glob("*.yml"))
    assert len(set(binfmt.findall(everything))) == 1 and len(set(buildkit.findall(everything))) == 1
    assert "**Build tool images by digest.**" in _POLICY and "refreshed by hand" in _POLICY


def test_dependabot_covers_every_dependency_ecosystem_the_repository_ships():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    updates = {entry["package-ecosystem"]: entry for entry in config["updates"]}
    assert set(updates) == {"pip", "github-actions", "npm", "docker"}
    assert updates["npm"]["directory"] == "/frontend/browser"
    assert (ROOT / "frontend" / "browser" / "package-lock.json").is_file()
    docker = updates["docker"]["directories"]
    for directory in ("/", "/packaging/aria2", "/packaging/ffmpeg"):
        assert directory in docker
        assert any((ROOT / directory.lstrip("/")).glob("Dockerfile*")), directory
    # The SABnzbd-pinned pip closure stays ignored, and its minor/patch group stays.
    assert {"dependency-name": "sabctools"} in updates["pip"]["ignore"]
    assert "minor-and-patch" in updates["pip"]["groups"]


def test_policy_documents_build_provenance_and_the_real_publishing_branches():
    assert "**Build provenance.**" in _POLICY
    assert "release-ops/*" in _POLICY and "--cert-identity-regex" in _POLICY and "--source-digest" in _POLICY
    assert "`main` or `1.0.0`" not in _POLICY
    assert "`staging/*` branch" in _POLICY
