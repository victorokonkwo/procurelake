"""The DMS replication user gets replication + read-only access, nothing more (local PostgreSQL)."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from procurelake.cdc.source_setup import replication_user_privileges, setup_replication_user

pytestmark = pytest.mark.postgres


@pytest.fixture
def admin(migrated_dsn):
    with psycopg.connect(migrated_dsn, autocommit=True) as c:
        yield c


@pytest.fixture
def username(admin):
    name = f"it_dms_{uuid.uuid4().hex[:8]}"
    yield name
    # Roles are cluster-wide: always remove the test role.
    admin.execute(psycopg.sql.SQL("DROP OWNED BY {}").format(psycopg.sql.Identifier(name)))
    admin.execute(psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(psycopg.sql.Identifier(name)))


def test_user_can_replicate_and_read_but_not_write(admin, username, migrated_dsn):
    setup_replication_user(admin, username, "s3cret-Pw_1", replication_grant="replication_attribute")
    privs = replication_user_privileges(admin, username)
    assert privs["replication_attribute"] and not privs["superuser"]
    assert not privs["createdb"] and not privs["createrole"]
    assert len(privs["tables"]) == 8
    assert all(t["select"] and not t["write"] for t in privs["tables"].values())
    # DMS may create its heartbeat table only in its own schema.
    assert privs["schema_create"] == {"public": False, "procurement": False, "awsdms": True}

    # The password works, and the login really cannot write.
    dsn = psycopg.conninfo.make_conninfo(migrated_dsn, user=username, password="s3cret-Pw_1")
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT count(*) FROM procurement.tenants").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM procurement.tenants")


def test_setup_is_idempotent_and_rotates_the_password(admin, username, migrated_dsn):
    setup_replication_user(admin, username, "first-Pw_1", replication_grant="replication_attribute")
    setup_replication_user(admin, username, "second-Pw_2", replication_grant="replication_attribute")
    with psycopg.connect(psycopg.conninfo.make_conninfo(migrated_dsn, user=username, password="second-Pw_2")):
        pass
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(psycopg.conninfo.make_conninfo(migrated_dsn, user=username, password="first-Pw_1"))


def test_password_with_quotes_is_safe(admin, username, migrated_dsn):
    tricky = "a'b\"c$d--e"
    setup_replication_user(admin, username, tricky, replication_grant="replication_attribute")
    with psycopg.connect(psycopg.conninfo.make_conninfo(migrated_dsn, user=username, password=tricky)):
        pass


def test_rejects_bad_identifiers(admin):
    with pytest.raises(ValueError):
        setup_replication_user(admin, "bad;name", "x", replication_grant="replication_attribute")
