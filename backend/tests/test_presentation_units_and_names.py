"""One owner each for what a transfer's size, multi-file name and route
outcome read as (DP 1.0.3 WS10).

- Sizes are decimal (1 KB = 1000 B) through app.js's one ``fmtSize`` -- the
  storage and folder-browser sizes delegate to it -- and the backend's one
  ``core.units.format_bytes`` says them by the same rule.
- A multi-file name is ``transfers.display_name``'s, said in the page's
  language from its parts by app.js's one ``transferTitle``.
- A route that ended failed reads "Failed" in Route History, never
  "Resolved" (``_route_presentation_outcome``).

The frontend functions run under Node with the real localization runtime and
English catalog.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.units import format_bytes
from transfers._repository_base import _route_presentation_outcome
from transfers.downloads_read_model import _downloads_search_match

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
SIZES = [999, 1000, 1500, 140_064_536, 999_960, 999.9e6, 1_000_000_000, 987.6e12, 1e15, 0]
SHOWN = ["999 B", "1 KB", "1.5 KB", "140.1 MB", "1 MB", "999.9 MB", "1 GB", "987.6 TB", "1 PB", "—"]


def _function(name: str) -> str:
    body = APP[APP.index("function " + name):]
    return body[:body.index("\n}\n") + 3]


def _run(functions, expression):
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to run the shared presentation functions")
    from i18n_node import program
    script = "\n".join(_function(name) for name in functions) + f"\nconsole.log(JSON.stringify({expression}));"
    completed = subprocess.run([node, "-e", program(script)], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(completed.stdout)


# -- sizes -------------------------------------------------------------------------------

def test_the_one_size_formatter_is_decimal_at_its_boundaries():
    assert _run(["fmtSize(b)"], f"{json.dumps(SIZES)}.map(fmtSize)") == SHOWN


def test_the_backend_says_a_size_by_the_same_decimal_rule():
    assert [format_bytes(value) for value in SIZES[:-1]] == SHOWN[:-1]
    assert format_bytes(0) == "0 B"


def test_storage_and_folder_sizes_have_no_formatter_of_their_own():
    for name in ("ui-storage-health.js", "ui-settings-directory-picker.js"):
        source = (STATIC / name).read_text(encoding="utf-8")
        assert "1024" not in source and "fmtSize(" in source, name
    assert "1024" not in _function("fmtSize(b)")


# -- multi-file names ----------------------------------------------------------------------

def test_a_multi_file_name_is_said_from_its_parts_with_a_plural():
    cases = [
        {"display_name": "SHA256SUMS + 1 more", "display_name_parts": {"name": "SHA256SUMS", "count": 1, "form": "more"}},
        {"display_name": "a.mkv + 3 more", "display_name_parts": {"name": "a.mkv", "count": 3, "form": "more"}},
        {"display_name": "Example.Release + 2 files",
         "display_name_parts": {"name": "Example.Release", "count": 2, "form": "files"}},
        {"display_name": "one.mkv", "display_name_parts": None},
        {"name": "watch"},
    ]
    assert _run(["transferTitle(t)"], f"{json.dumps(cases)}.map(transferTitle)") == [
        "SHA256SUMS + 1 more", "a.mkv + 3 more", "Example.Release + 2 files", "one.mkv", "watch"]


def test_search_reaches_into_the_one_multi_file_wording():
    names = json.dumps(["SHA256SUMS", "ubuntu.iso"])
    match = lambda needle, files=names: _downloads_search_match(needle, "batch", "direct_link", files,
                                                                 None, None, None, "[]", "[]")
    assert match("+ 1 more") == 1 and match("1 more") == 1
    assert match("1 files") == 0
    parts = json.dumps(["Example.Release.part01.rar", "Example.Release.part02.rar"])
    assert match("2 files", parts) == 1


# -- Route History outcome -----------------------------------------------------------------

def _route(**row):
    return {"request_id": "r1", "contributing_transfer_id": 7, "outcome": "resolved", **row}


def test_a_route_that_ended_failed_never_reads_resolved():
    states = {"r1": "materializing"}
    assert _route_presentation_outcome(_route(), states, False, 7) == "resolved"
    # Its source failed after the provider answered.
    assert _route_presentation_outcome(_route(), {"r1": "failed"}, False, 7) == "failed"
    # Its own transfer ended failed without the route ever delivering.
    assert _route_presentation_outcome(_route(), states, True, 7) == "failed"
    # A contribution the backend states failed.
    assert _route_presentation_outcome(_route(contributing_transfer_id=9, verification_state="failed"),
                                       states, False, 7) == "failed"
    # Another transfer's route is not this transfer's failure; a delivered route stays completed.
    assert _route_presentation_outcome(_route(contributing_transfer_id=9), states, True, 7) == "resolved"
    assert _route_presentation_outcome(_route(outcome="completed"), states, True, 7) == "completed"


def test_route_history_renders_the_routes_presentation_outcome():
    attempts = [{"ordinal": 1, "provider_name": "HTTP", "outcome": "resolved", "presentation_outcome": "failed",
                 "relation": "original", "route_identity": "https://host.example/a"}]
    script_functions = ["esc(s)", "routeOutcomePresentation(value)", "renderRouteHistory(t)"]
    html = _run(script_functions, f"renderRouteHistory({{id: 7, route_attempts: {json.dumps(attempts)}}})")
    assert 'data-route-outcome="failed"' in html and ">Failed<" in html and "Resolved" not in html
