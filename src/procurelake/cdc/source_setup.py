"""Prepare a PostgreSQL source for AWS DMS full load + CDC (ADR 0027).

Creates (or updates) the login DMS connects as, with only what the AWS DMS User Guide lists for
an RDS source with ongoing CDC: "the endpoint account must have the rds_replication role, along
with SELECT permissions on the tables that you migrate". No rds_superuser. CaptureDdls is off,
so DMS creates no event triggers or DDL tables on the source.

DMS's WAL heartbeat (HeartbeatEnable) creates its own table, ``awsdms_heartbeat``, in the
endpoint's HeartbeatSchema. Since PostgreSQL 15 an ordinary user cannot create objects in
``public``, and the first dev task failed exactly there ("permission denied for schema public").
So the setup creates a dedicated ``awsdms`` schema in which only the DMS user may create objects;
DMS artifacts stay out of ``public`` and out of the replicated schema.

Idempotent: re-running updates the password and re-applies the grants.
"""

from __future__ import annotations

import re
from typing import Literal

import psycopg
from psycopg import sql

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

ReplicationGrant = Literal["rds_replication", "replication_attribute"]


def setup_replication_user(
    conn: psycopg.Connection,
    username: str,
    password: str,
    schema: str = "procurement",
    replication_grant: ReplicationGrant = "rds_replication",
    artifacts_schema: str = "awsdms",
) -> None:
    """Create/update ``username`` with replication rights and read-only access to ``schema``.

    ``replication_grant``: ``rds_replication`` (Amazon RDS: grant the RDS role) or
    ``replication_attribute`` (plain PostgreSQL, e.g. local tests: the REPLICATION attribute).
    """
    for name in (username, schema, artifacts_schema):
        if not _IDENT.match(name):
            raise ValueError(f"invalid identifier {name!r}")
    user, sch = sql.Identifier(username), sql.Identifier(schema)
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,)).fetchone()
    # Passwords cannot be bind parameters in CREATE/ALTER ROLE; sql.Literal quotes them safely.
    verb = sql.SQL("ALTER ROLE") if exists else sql.SQL("CREATE ROLE")
    conn.execute(sql.SQL("{} {} LOGIN PASSWORD {}").format(verb, user, sql.Literal(password)))
    if replication_grant == "rds_replication":
        conn.execute(sql.SQL("GRANT rds_replication TO {}").format(user))
    else:
        conn.execute(sql.SQL("ALTER ROLE {} REPLICATION").format(user))
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(conn.info.dbname), user))
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sch, user))
    conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(sch, user))
    art = sql.Identifier(artifacts_schema)
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(art))
    conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(art))
    conn.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(art, user))
    # Tables added by later migrations are readable too.
    conn.execute(
        sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}").format(sch, user)
    )


def replication_user_privileges(
    conn: psycopg.Connection, username: str, schema: str = "procurement", artifacts_schema: str = "awsdms"
) -> dict:
    """What ``username`` can do: for verification and tests."""
    role = conn.execute(
        "SELECT rolsuper, rolreplication, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = %s",
        (username,),
    ).fetchone()
    tables = conn.execute(
        """
        SELECT c.relname,
               has_table_privilege(%(u)s, c.oid, 'SELECT') AS can_select,
               has_table_privilege(%(u)s, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE') AS can_write
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %(s)s AND c.relkind = 'r'
        ORDER BY 1
        """,
        {"u": username, "s": schema},
    ).fetchall()
    member_of = [
        r[0]
        for r in conn.execute(
            "SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
            "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = %s ORDER BY 1",
            (username,),
        ).fetchall()
    ]
    schema_create = {
        s: conn.execute("SELECT has_schema_privilege(%s, %s, 'CREATE')", (username, s)).fetchone()[0]
        for s in ("public", schema, artifacts_schema)
    }
    return {
        "schema_create": schema_create,
        "superuser": role[0],
        "replication_attribute": role[1],
        "createdb": role[2],
        "createrole": role[3],
        "member_of": member_of,
        "tables": {name: {"select": s, "write": w} for name, s, w in tables},
    }
