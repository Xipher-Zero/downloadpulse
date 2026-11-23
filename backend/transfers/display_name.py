"""Canonical human-facing transfer display name.

Presentation only: never rewrites persisted transfer identity, source/request
provenance, or acquisition candidate truth. Consumes bounded artifact-name
facts the caller already has in hand (current authoritative artifact
filenames) and produces one deterministic, conservative display string.

Historical precedent (DP 1.0.12 UI presentation pass, Section 15/28 audit):
``backend/services/manager_v2.py`` (commit ``4372cec2``, "Derive useful
direct-link batch names") introduced ``direct_link_collection_name`` /
``_direct_link_collection_base`` to build a useful parent label for a
direct-link submission batch from resolved filenames, with an explicit
conservative rule: recognize a small, fixed set of multipart/split-archive
filename patterns and collapse them to a shared stem; never invent a common
name across unrelated filenames. That logic survived the Universal Transfer
Core rewrite (``a1a33583``) as ``transfers.requests.direct_link_collection_name``
/ ``_direct_link_collection_base`` and remains live today, called once at
direct-link SUBMISSION time with an always-empty ``resolved_names`` list
(``application/service.py``) — so it can never reflect real artifact
filenames once they become known, and the raw root/request name (e.g.
``1fichier.com - <hash> + 23 more``) is what list surfaces have shown ever
since. This module reuses that same proven pattern-matching core (the three
multipart/split-archive regexes and the case-insensitive common-base check)
against the transfer's CURRENT authoritative artifact filenames instead, so
the display name reflects what is actually being downloaded.

This module is also the one owner of a multi-file name's wording
(``multi_file_name``): a shown name plus how many OTHER files or links there
are reads "<name> + N more" -- the submitted name a direct-link batch is
given (``transfers.requests.direct_link_collection_name``) and the
representative-filename form below alike, so one transfer is never named both
ways. A derived collection base, itself none of the files, counts every file:
"<base> + N files". Each name also carries its structured parts
(``TransferDisplayName.parts``), from which the browser says it in the
reader's language (``transfer.name.more`` / ``transfer.name.files``); the
English text stays the API's and the journal's.
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional, Sequence

_UNNAMED = "(unnamed)"


class TransferDisplayName(NamedTuple):
    """A transfer's title: its English ``text`` and, for a multi-file name,
    its ``parts`` -- ``{"name", "count", "form"}``, ``form`` being ``"more"``
    (``name`` is one of the files or links; ``count`` the others) or
    ``"files"`` (``name`` is a derived collection base; ``count`` every file)
    -- else ``None``."""
    text: str
    parts: Optional[dict] = None


def multi_file_name(name: str, count: int) -> TransferDisplayName:
    """``name`` -- one file or link of several -- and the ``count`` others."""
    return TransferDisplayName(f"{name} + {int(count)} more", {"name": name, "count": int(count), "form": "more"})


def _collection_name(base: str, count: int) -> TransferDisplayName:
    return TransferDisplayName(f"{base} + {int(count)} files", {"name": base, "count": int(count), "form": "files"})

_MULTIPART_BASE_PATTERNS = (
    re.compile(r"(?i)^(?P<base>.+)\.part\d+\.rar$"),
    re.compile(r"(?i)^(?P<base>.+)\.r\d{2,3}$"),
    re.compile(r"(?i)^(?P<base>.+)\.(?:7z|zip|rar)\.\d{3}$"),
)


def _collection_base(filename: str) -> str:
    """Return a conservative collection stem for a known multipart filename.

    Only the fixed set of multipart/split-archive suffixes above are ever
    collapsed; an unrecognized filename is returned unchanged so an unrelated
    name never gets silently folded into a fabricated common base.
    """
    name = str(filename or "").strip()
    for pattern in _MULTIPART_BASE_PATTERNS:
        match = pattern.match(name)
        if match:
            base = match.group("base").rstrip(" .-_")
            if base:
                return base
    return name


def normalized_transfer_display_name(
    artifact_filenames: Sequence[str],
    root_name: Optional[str] = None,
    *,
    root_is_canonical_identity: bool = False,
) -> str:
    """The English text of ``transfer_display_name``."""
    return transfer_display_name(
        artifact_filenames, root_name, root_is_canonical_identity=root_is_canonical_identity).text


def transfer_display_name(
    artifact_filenames: Sequence[str],
    root_name: Optional[str] = None,
    *,
    root_is_canonical_identity: bool = False,
) -> TransferDisplayName:
    """Derive the canonical human-facing transfer title.

    Preference order (Section 15 of the DP 1.0.12 presentation task; narrowed
    by the DP 1.0.12 Workstream C name-regression correction):
    0. when the caller asserts ``root_is_canonical_identity`` (the transfer's
       durable submission kind is a torrent/magnet — a canonical
       source/request fact, never inferred from filename shape or count) and
       a non-blank root/request ``name`` exists, that root name IS the
       transfer's real identity and wins outright, regardless of how many
       artifacts exist or what they are named. A torrent/magnet root title
       must never be replaced by one arbitrary member filename.
    1. otherwise, a normalized artifact-derived name (single file, or a safe
       common collection base across multiple related files);
    2. a representative artifact filename plus the remaining file count, when
       no safe common base can be derived without over-normalizing;
    3. the root/request ``name`` fallback, only when no artifact filenames
       are available at all;
    4. a final generic "(unnamed)" fallback.

    ``root_is_canonical_identity`` is the only caller-supplied policy input;
    it must be derived from durable submission/source semantics (e.g. the
    transfer's request kind), never from filename patterns, extensions, UI
    glyphs, provider identity, or member count.

    Deterministic and conservative: never strips words merely because they
    differ, never derives a name from source host/provider/candidate/request
    identity, never infers metadata absent from the filenames themselves.

    Count wording is form-dependent, by exactly what is visibly shown before
    the "+":

    - COLLECTION-IDENTITY form (a derived common base, e.g. "Example.Release"
      — not itself one of the N filenames): the count is the TOTAL file
      count, because none of the N files are separately named already.
      Example: 24 files ``Example.Release.part01.rar`` .. ``part24.rar`` ->
      ``"Example.Release + 24 files"``.
    - REPRESENTATIVE-FILENAME form (no safe common base; the first actual
      filename is shown as-is): the count is TOTAL MINUS ONE, because that
      shown filename already visibly accounts for one of the N files and
      counting it again would overstate the total -- the others, said as
      ``multi_file_name`` says them. Example: a 24-file set whose members do
      not share a recognized common base, with ``Example.Release.part01.rar``
      as the first filename -> exactly
      ``"Example.Release.part01.rar + 23 more"``.
    """
    names = [str(name).strip() for name in (artifact_filenames or []) if str(name or "").strip()]
    if not names:
        fallback = str(root_name or "").strip()
        return TransferDisplayName(fallback or _UNNAMED)

    if root_is_canonical_identity:
        canonical_root = str(root_name or "").strip()
        if canonical_root:
            return TransferDisplayName(canonical_root)

    if len(names) == 1:
        return TransferDisplayName(names[0])

    total = len(names)
    bases = [_collection_base(name) for name in names]
    first_base = bases[0]
    if first_base and all(base.casefold() == first_base.casefold() for base in bases[1:]):
        # The derived base is not itself one of the N filenames: count every
        # file (Section 16's "Example.Release + 24 files" — total, not N-1).
        return _collection_name(first_base, total)

    # No safe common base: a representative filename (itself one of the N)
    # plus how many OTHER files exist, rather than an aggressive/wrong guess
    # ("Example.Release.part01.rar + 23 more" — total-1, since the shown name
    # already accounts for one of the files).
    return multi_file_name(names[0], total - 1)
