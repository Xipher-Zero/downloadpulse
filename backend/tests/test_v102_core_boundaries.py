"""1.0.2 (operator, 2026-11-22): the core's boundaries, enforced -- every rule here
would have failed on a crossing the boundary review found.

* An integration (provider, executor, post-processor, integration package) receives
  what it needs from core on the requests core passes it and through
  ``IntegrationEnvironment``: it imports no edge (``external``, ``api``,
  ``application``, ``auth``), no settings owner, no database (beyond locating its
  private runtime folder), and reaches no ambient core state.
* It never stages, discards or sweeps staged input, never converges durable rows,
  and is never handed the transfer repository or the application service.
* An edge reads core through public names only -- never ``transfers._*`` or an
  underscore name -- and never writes core state outside an application command.
* Core declares no ContextVar an integration could read, and neutral code names no
  concrete integration.
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
EDGES = ("external", "api", "application", "auth")


def _files(*bases):
    for base in bases:
        for path in (BACKEND / base).rglob("*.py"):
            if "__pycache__" not in path.parts and "tests" not in path.parts:
                yield path


def _integration_files():
    yield from _files("providers", "executors", "postprocessors")
    for package in (BACKEND / "integrations").iterdir():
        if package.is_dir() and package.name != "__pycache__":
            yield from _files(f"integrations/{package.name}")


def _imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module, tuple(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, ()


def _rel(path):
    return path.relative_to(BACKEND).as_posix()


def test_no_integration_imports_an_edge_a_settings_owner_or_the_database():
    found = []
    for path in _integration_files():
        for module, names in _imports(path):
            top = module.split(".")[0]
            if top in EDGES:
                found.append((_rel(path), module))
            elif module == "core.config" and set(names) - {"AppSettings"}:
                found.append((_rel(path), f"core.config:{names}"))
            elif module == "db.database" and set(names) - {"DB_PATH"}:
                found.append((_rel(path), f"db.database:{names}"))
            elif top == "db" and module != "db.database":
                found.append((_rel(path), module))
    assert found == []


def test_no_integration_stages_or_reclaims_input_or_converges_durable_rows():
    forbidden = ("staged_input.stage", "staged_input.discard", "staged_input.sweep", "converge_staged_input",
                 "environment.repository",
                 "self.repository.executions(", "self.repository.live_executions(")
    found = [(_rel(path), token) for path in _integration_files()
             for token in forbidden if token in path.read_text(encoding="utf-8")]
    assert found == []


def test_integrations_are_lent_grants_and_a_reader_never_the_repository_or_the_service():
    composition = (BACKEND / "application" / "composition.py").read_text(encoding="utf-8")
    assert "IntegrationEnvironment(ExecutionGrants(application.repository)" in composition
    assert "commands=IntegrationCommands(application)" in composition
    assert "staged_input=StagedInputReader(" in composition
    definition = (BACKEND / "integrations" / "definition.py").read_text(encoding="utf-8")
    assert "    repository: object" not in definition and "    executions: object" in definition


def test_no_edge_reaches_a_private_core_name():
    found = [(_rel(path), module, names) for path in _files(*EDGES, "services")
             for module, names in _imports(path)
             if module.startswith("transfers.")
             and (module.split(".")[1].startswith("_") or any(name.startswith("_") for name in names))]
    assert found == []
    for path in _files(*EDGES):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            # ``<...>.repository._private`` -- an edge calling a repository internal.
            if (isinstance(node, ast.Attribute) and node.attr.startswith("_")
                    and isinstance(node.value, ast.Attribute) and node.value.attr == "repository"):
                raise AssertionError(f"{_rel(path)}: repository.{node.attr}")
            # A queue/label write outside the application command that serializes it.
            if (isinstance(node, ast.Attribute) and node.attr == "update_metadata"
                    and isinstance(node.value, ast.Attribute) and node.value.attr == "repository"
                    and path.name != "service.py"):
                raise AssertionError(f"{_rel(path)}: repository.update_metadata")


def test_core_declares_no_ambient_state_for_an_integration():
    assert [(_rel(path)) for path in _files("transfers") if "ContextVar(" in path.read_text(encoding="utf-8")] == []


def _code_strings(path):
    """String constants that are code, not docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.value


def test_neutral_code_names_no_concrete_integration():
    from integrations.catalog import definitions
    # Concrete integration identities (and the executors' own), never schemes or kinds: the
    # ids ``rsync``, ``usenet`` and ``media`` also name a scheme, a request class and a
    # neutral concept, so only the unambiguous ones are scanned.
    names = {definition.id.casefold() for definition in definitions}
    names |= {"aria2", "yt_dlp", "sabnzbd", "debrid-link", "multimeta"}
    names -= {"rsync", "usenet", "media"}
    neutral = [*_files("transfers"), BACKEND / "application" / "service.py",
               BACKEND / "services" / "network_safety.py", BACKEND / "services" / "artifact_sampling.py"]
    found = []
    for path in neutral:
        for value in _code_strings(path):
            lowered = value.casefold()
            hit = next((name for name in names if name and (lowered == name or f"{name} " in lowered
                                                              or f"{name}-" in lowered)), None)
            if hit:
                found.append((_rel(path), value[:60]))
    # Legacy settings keys translated into universal fields (never branched on).
    found = [item for item in found if not item[1].startswith("aria2_")]
    assert found == []


# The transfer core's own tables. An edge asks core (the application service, a
# ``transfers.read_models`` name) -- e.g. ``serving_transfer`` for which transfer serves a
# consolidated one -- and never derives a core decision from these tables itself.
CORE_TABLES = frozenset({
    "torrents", "download_files", "artifact_consolidations", "artifact_material_state", "artifact_recovery_state",
    "canonical_candidate_bindings", "canonical_candidate_origins", "execution_attempt_provenance",
    "execution_attempts", "pending_provider_choices", "postprocess_attempts", "provider_quota_holds",
    "provider_resources", "resolution_attempts", "route_attempt_provenance", "standby_resources",
    "transfer_controls", "transfer_demands", "transfer_file_manifest_entries", "transfer_file_manifests",
    "transfer_file_selection_entries", "transfer_file_selection_intent_entries", "transfer_file_selection_intents",
    "transfer_file_selections", "transfer_input_challenges", "transfer_outcomes", "transfer_pause_intents",
    "transfer_requests"})

# 1.0.2 (operator, 2026-11-22): the direct reads that predated this rule -- the
# Downloads read model, the statistics routes and the External API's job projection --
# now live in core (transfers.downloads_read_model, transfers.statistics,
# transfers.demand_fulfilment). No edge holds any; none may come back.
CORE_TABLE_DEBT: dict[str, int] = {}


def test_no_edge_queries_a_core_table_beyond_its_recorded_debt():
    import re
    statement = re.compile(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)
    found = {}
    for path in _files("api", "external", "auth"):
        count = sum(1 for table in statement.findall(path.read_text(encoding="utf-8"))
                    if table.lower() in CORE_TABLES)
        if count:
            found[_rel(path)] = count
    assert {name: count for name, count in found.items() if count > CORE_TABLE_DEBT.get(name, 0)} == {}
