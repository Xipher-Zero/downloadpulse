"""How the backend says a quantity to a person: the one size formatter, by the
browser's own rule (``app.js`` ``fmtSize``), for every backend text that names
a size -- notifications and statistics reports alike."""
from __future__ import annotations


def format_bytes(b) -> str:
    """A size in decimal units (1 KB = 1000 B), the browser's ``fmtSize`` rule:
    whole bytes, else one decimal without a trailing ".0", rolling to the next
    unit before four integer digits."""
    value = max(0.0, float(b or 0))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    unit = 0
    while round(value, 1) >= 1000 and unit < len(units) - 1:
        value /= 1000
        unit += 1
    if unit == 0:
        return f"{round(value)} B"
    text = f"{value:.1f}"
    return f"{text[:-2] if text.endswith('.0') else text} {units[unit]}"
