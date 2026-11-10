"""Unit tests for migration discovery (no database required)."""

from __future__ import annotations

import hashlib

import pytest

from procurelake.common.postgres_migrations import MigrationError, discover_migrations


def test_discovers_in_version_order_with_checksums(tmp_path):
    (tmp_path / "V0002__second.sql").write_text("SELECT 2;")
    (tmp_path / "V0001__first.sql").write_text("SELECT 1;")

    migrations = discover_migrations(tmp_path)

    assert [m.filename for m in migrations] == ["V0001__first.sql", "V0002__second.sql"]
    assert migrations[0].checksum_sha256 == hashlib.sha256(b"SELECT 1;").hexdigest()


@pytest.mark.parametrize("name", ["001_init.sql", "V1__init.sql", "V0001-init.sql", "V0001__Init.sql"])
def test_rejects_invalid_filenames(tmp_path, name):
    (tmp_path / name).write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="Invalid migration filename"):
        discover_migrations(tmp_path)


def test_rejects_duplicate_versions(tmp_path):
    (tmp_path / "V0001__a.sql").write_text("SELECT 1;")
    (tmp_path / "V0001__b.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="Duplicate migration version"):
        discover_migrations(tmp_path)


def test_missing_directory_is_an_error(tmp_path):
    with pytest.raises(MigrationError, match="does not exist"):
        discover_migrations(tmp_path / "nope")
