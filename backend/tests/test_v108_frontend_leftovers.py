"""1.0.8 (WS-A12): the inline-handler era's page-wide functions stay gone, and
every stylesheet is in the one @import graph.

Since the CSP migration every control dispatches through its owner's frozen
action table (ui-actions.js), so the Downloads owner's and the shell's former
``window.*`` exports had no caller left: a page-wide surface for deleting,
retrying and bulk actions that nothing needed. Storage Health's stylesheet was
injected at runtime with a hand-written ``?v=``, outside ``style.css`` and the
server's content versions.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"

GONE = ("setFilter", "onTorrentOrderChange", "onDownloadRowClick", "reaffirmQueue", "moveSelectionToTop",
        "onPageSizeChange", "toggleAllCheckboxes", "bulkAction", "setLabel", "retryT")
# A text search sees only explicit ``window.X =`` exports: a top-level declaration in a classic
# script (app.js's ``downloadTransferTrace``, among its other implicit globals) is a page global
# without one. The browser spec settings-services-batch2.spec.js checks GONE at runtime.
# Still exported, for one browser spec alone (recorded debt): ui-fix-ws2-p1.spec.js awaits a
# single-row removal to prove it resolves only once its focus has settled, which no control
# exposes. Never more. The specs drive the pager, Clear Selections and the row checkboxes
# instead of the former hooks below, which are gone like GONE (checked at runtime too).
TEST_HOOKS = ("deleteT",)
RETIRED_HOOKS = ("goToTorrentPage", "clearSelection", "onCheckboxChange")
# Unreachable since 1.0.0 (no control, no action-table entry, no caller): deleted outright.
DELETED = ("onPageSizeChange", "setLabel")


def test_the_former_page_wide_functions_are_not_exported_or_called_as_globals():
    for path in STATIC.glob("*.js"):
        source = path.read_text(encoding="utf-8")
        for name in GONE + RETIRED_HOOKS:
            assert not re.search(rf"window\.{name}\b", source), (path.name, name)


def test_only_the_recorded_test_hooks_remain_and_no_production_code_calls_them():
    downloads = (STATIC / "ui-downloads.js").read_text(encoding="utf-8")
    exported = set(re.findall(r"^window\.(\w+) = \w+;$", downloads, flags=re.M))
    assert exported == set(TEST_HOOKS) | {"downloadsDateMarkup", "downloadsDayMarkup", "downloadsDateLabel",
                                          "downloadsCalendarLabel", "downloadsDayLabel",
                                          "updateDownloadsTrackedCopy", "loadTorrents"}, exported
    for path in STATIC.glob("*.js"):
        if path.name == "ui-downloads.js":
            continue
        source = path.read_text(encoding="utf-8")
        for name in TEST_HOOKS:
            assert not re.search(rf"(?<![\w.]){name}\(", source), (path.name, name)


def test_the_dashboard_reaches_the_failed_filter_through_the_downloads_owner():
    # One declaration per name in the owner: a second ``function showFilter`` once replaced
    # the filter presentation's own (WS-A12, caught by the browser specs).
    downloads_source = (STATIC / "ui-downloads.js").read_text(encoding="utf-8")
    names = re.findall(r"^(?:async )?function (\w+)\(", downloads_source, flags=re.M)
    assert len(names) == len(set(names)), sorted({n for n in names if names.count(n) > 1})
    downloads = (STATIC / "ui-downloads.js").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "window.DPDownloads = Object.freeze({scheduleCapacityCheck, contextMenu, enter: enterDownloads, showStatus});" in downloads
    assert "window.DPDownloads.showStatus('error')" in app


def test_no_stylesheet_is_injected_at_runtime():
    for path in STATIC.glob("*.js"):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"createElement\(\s*['\"]link['\"]\s*\)", source), path.name
    assert "@import url('/ui-storage-health.css?v=1');" in (STATIC / "style.css").read_text(encoding="utf-8")


def test_the_unreachable_page_size_and_label_functions_are_deleted():
    downloads = (STATIC / "ui-downloads.js").read_text(encoding="utf-8")
    for name in DELETED:
        assert not re.search(rf"\b{name}\b", downloads), name


def test_no_browser_spec_reaches_for_a_retired_hook():
    browser = STATIC.parent / "browser"
    for path in browser.glob("*.spec.js"):
        source = path.read_text(encoding="utf-8")
        for name in RETIRED_HOOKS:
            assert not re.search(rf"(?<![\w.'\"]){name}\(", source), (path.name, name)
