"""The desktop supervisor reads its tray and notice words without a web
framework and without reading every catalog (1.0.5 memory investigation: the
two cost it about 38 MB it never gave back). Each check runs in a fresh
interpreter, so what is imported or parsed is this process's alone."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
I18N = ROOT / "frontend" / "static" / "i18n"

PROBE = r"""
import json, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1] + "/backend", sys.argv[1] + "/desktop"]
import supervisor
from core import localization
words = supervisor.Words(Path(sys.argv[2]))
said = words("desktop.tray.open")
heavy = sorted(name for name in ("fastapi", "starlette", "pydantic") if name in sys.modules)
print(json.dumps({"tag": words.tag, "said": said, "heavy": heavy,
                  "parsed": localization._parsed.cache_info().currsize}))
"""


def _words(tmp_path, configured):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"default_locale": configured} if configured else {}), encoding="utf-8")
    result = subprocess.run([sys.executable, "-I", "-c", PROBE, str(ROOT), str(config)], capture_output=True,
                            text=True, check=True, env={"PATH": "/usr/bin:/bin", "STATIC_DIR": str(ROOT / "frontend/static")})
    return json.loads(result.stdout.strip().splitlines()[-1])


def _catalog(tag):
    return json.loads((I18N / f"{tag}.json").read_text(encoding="utf-8"))["messages"]


def test_the_tray_words_need_no_web_framework_and_read_only_their_own_catalog(tmp_path):
    shown = _words(tmp_path, "de")
    assert shown["tag"] == "de" and shown["said"] == _catalog("de")["desktop.tray.open"]
    assert shown["heavy"] == []
    # The manifest, English and German: never the other 17 catalogs.
    assert shown["parsed"] == 3


def test_a_tag_the_manifest_does_not_admit_shows_english(tmp_path):
    english = _catalog("en")["desktop.tray.open"]
    for configured in (None, "xx", "en-XA"):           # none, unknown, a pseudo locale without test locales
        shown = _words(tmp_path, configured)
        assert (shown["tag"], shown["said"]) == ("en", english), configured


def test_the_manifest_only_answer_is_the_full_rule_while_every_catalog_reads(tmp_path, monkeypatch):
    from core import localization
    assert localization.locale_tags(catalogs=False) == localization.locale_tags()
    # A supported target whose catalog cannot be read is still named by the
    # manifest-only answer; its words then fall back to English, key by key.
    broken = tmp_path / "i18n"
    broken.mkdir()
    for item in I18N.iterdir():
        (broken / item.name).write_bytes(item.read_bytes())
    (broken / "de.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("STATIC_DIR", str(tmp_path))
    assert "de" not in localization.locale_tags() and "de" in localization.locale_tags(catalogs=False)
    assert localization.message("desktop.tray.open", "de") == _catalog("en")["desktop.tray.open"]
