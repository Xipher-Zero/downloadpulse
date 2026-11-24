import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_install_surfaces_preserve_published_image_and_public_health_endpoint():
    root = Path(__file__).resolve().parents[2]
    # Install surfaces name the newest published release, derived from the
    # canonical CHANGELOG. VERSION owns the in-development version, which on a
    # development line has no published image, so install surfaces must not
    # fabricate a tag for it.
    headings = re.findall(
        r"^## \[(\d+(?:\.\d+)+)\](.*)$", (root / "CHANGELOG.md").read_text(), re.M
    )
    released = next(v for v, title in headings if "development" not in title.casefold())
    expected_image = f"ghcr.io/xipher-zero/downloadpulse:v{released}"
    compose = (root / "docker-compose.yml").read_text()
    readme = (root / "README.md").read_text()
    assert expected_image in compose
    assert readme.count(expected_image) >= 2
    development = (root / "VERSION").read_text().strip()
    if development != released:
        unreleased = f"ghcr.io/xipher-zero/downloadpulse:v{development}"
        for surface in (compose, readme):
            assert unreleased not in surface
    # The image's own HEALTHCHECK is the one health check, on the public health
    # endpoint; the compose example declares none that could diverge from it.
    dockerfile = (root / "Dockerfile").read_text()
    healthcheck = dockerfile[dockerfile.index("HEALTHCHECK"):].split("\n\n", 1)[0]
    assert "http://localhost:8095/api/health" in healthcheck
    assert "http://localhost:8095/api/stats" not in dockerfile
    assert "healthcheck:" not in compose
    assert "/api/stats" not in compose


@pytest.mark.asyncio
async def test_the_database_and_its_wal_and_shm_are_created_owner_only(tmp_path, monkeypatch):
    import os
    import sqlite3

    import db.database as database

    # The container's default UMASK (002) would otherwise leave them group-readable.
    previous = os.umask(0o002)
    try:
        created = tmp_path / "data" / "downloadpulse.db"
        await database.init_db(created)
        with sqlite3.connect(created) as live:   # WAL: the -wal and -shm exist while it is open
            live.execute("INSERT INTO torrents(hash, name) VALUES('mode', 'mode')")
            members = [created, created.with_name(created.name + "-wal"), created.with_name(created.name + "-shm")]
            assert {m.name: _mode(m) for m in members} == {m.name: 0o600 for m in members}
        # An existing database a previous release created group- and world-readable is made owner-only.
        existing = tmp_path / "old.db"
        sqlite3.connect(existing).close()
        existing.chmod(0o644)
        await database.init_db(existing)
        assert _mode(existing) == 0o600
    finally:
        os.umask(previous)


@pytest.mark.asyncio
async def test_online_backup_sqlite_copy_is_private(tmp_path, monkeypatch):
    import db.database as database
    import services.backup as backup

    db_path = tmp_path / "live.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    async with database.get_db() as db:
        await db.execute("INSERT INTO torrents(hash, name) VALUES(?, ?)", ("mode-test", "mode-test"))
        await db.commit()
    from backup_support import prepare_backup_installation
    await prepare_backup_installation(tmp_path, monkeypatch)

    backup_root = tmp_path / "backups"
    monkeypatch.setattr(
        backup,
        "_cfg",
        lambda: SimpleNamespace(
            backup_enabled=True,
            backup_folder=str(backup_root),
            backup_keep_days=7,
        ),
    )
    result = await backup.run_backup()
    backup_dir = Path(result["backup_dir"])
    db_copy = backup_dir / db_path.name

    assert result["errors"] == []
    assert _mode(backup_root) == 0o700
    assert _mode(backup_dir) == 0o700
    # Every member of the restore point is private, not only the database.
    for member in backup_dir.iterdir():
        assert _mode(member) == 0o600, member.name
    assert _mode(db_copy) == 0o600
