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
# No test hook remains (1.0.8): ui-fix-ws2-p1.spec.js clicks a row's real Remove and awaits its
# ``downloadpulse:downloads-delete-settled`` notification, sent once its focus has settled (as
# bulkAction's ``downloadpulse:downloads-bulk-action-settled``). The specs drive the pager, Clear
# Selections and the row checkboxes instead of the former hooks below, which are gone like GONE
# (checked at runtime too).
RETIRED_HOOKS = ("goToTorrentPage", "clearSelection", "onCheckboxChange", "deleteT")
# Unreachable since 1.0.0 (no control, no action-table entry, no caller): deleted outright.
DELETED = ("onPageSizeChange", "setLabel")


def test_the_former_page_wide_functions_are_not_exported_or_called_as_globals():
    for path in STATIC.glob("*.js"):
        source = path.read_text(encoding="utf-8")
        for name in GONE + RETIRED_HOOKS:
            assert not re.search(rf"window\.{name}\b", source), (path.name, name)


def test_no_test_hook_remains_and_a_removal_says_when_it_has_settled():
    downloads = (STATIC / "ui-downloads.js").read_text(encoding="utf-8")
    exported = set(re.findall(r"^window\.(\w+) = \w+;$", downloads, flags=re.M))
    assert exported == {"downloadsDateMarkup", "downloadsDayMarkup", "downloadsDateLabel",
                        "downloadsCalendarLabel", "downloadsDayLabel",
                        "updateDownloadsTrackedCopy", "loadTorrents"}, exported
    # Both removals notify once focus has settled, the single row's as the bulk action's.
    for event in ("downloadpulse:downloads-delete-settled", "downloadpulse:downloads-bulk-action-settled"):
        settled = downloads.index(f"new CustomEvent('{event}'")
        assert downloads.rindex("await settleRemovalFocus(button, restored);", 0, settled) > settled - 300, event


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
