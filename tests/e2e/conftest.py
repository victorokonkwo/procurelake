"""Acceptance (end-to-end) fixtures: plan §3, local tier.

Every test here starts from the same small, reproducible e2e dataset (the synthetic generator,
seed 42: 5,702 rows over two fictional tenants) loaded into a throwaway PostgreSQL database,
and runs the real pipeline code into a local lake: the snapshot connector into Bronze, the
Silver Glue job's ``run_batch``, and the CDC Glue job's ``run_cdc_table`` with its DynamoDB
lease and checkpoint.

Two test doubles, both test-only (AGENTS.md §2.1):

- ``DmsDouble`` stands in for AWS DMS. It writes PostgreSQL's real committed rows as
  DMS-shaped Parquet files (``Op``, ``_dms_commit_ts``, then the typed source columns; deletes
  carry only the primary key; ``LOAD00000001.parquet`` for the full load and
  ``YYYYMMDD-HHMMSSmmm.parquet`` for changes), the shape DMS wrote in dev (ADR 0027).
- DynamoDB is moto.

What this tier cannot show, Gold on Athena and real DMS, is covered by the live tier
(``make test-acceptance-live``).
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import boto3
import psycopg
import pytest
from e2e_support import CDC_ROOT, CONTRACTS, REPO, DmsDouble
from moto import mock_aws

from procurelake.cdc.checkpoint import CheckpointStore
from procurelake.cdc.job import run_cdc_table
from procurelake.common.postgres_migrations import apply_migrations
from procurelake.data_generator.config import load_config
from procurelake.data_generator.generate import generate_dataset
from procurelake.data_generator.load import load_dataset
from procurelake.ingestion.connectors.postgres_snapshot import PostgresSnapshotConnector
from procurelake.ingestion.landing import LocalLandingStore
from procurelake.lakehouse.silver.spec import SilverTableSpec


@pytest.fixture(scope="session")
def e2e_dataset():
    return generate_dataset(load_config(REPO / "configs" / "tenants" / "synthetic_tenants.yaml"), seed=42)


@pytest.fixture
def source(scratch_db_factory, e2e_dataset) -> str:
    """A fresh PostgreSQL database holding the e2e dataset; returns its DSN."""
    dsn = scratch_db_factory()
    apply_migrations(dsn, REPO / "database" / "postgres" / "migrations")
    with psycopg.connect(dsn) as conn:
        load_dataset(conn, e2e_dataset)
    return dsn


@pytest.fixture
def lake(spark, tmp_path):
    """A local lake: Bronze and artifacts stores, a quarantine root, and a Silver namespace."""
    ns = f"lake.e2e_{tmp_path.name.replace('-', '_')[-20:]}"
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {ns}")
    return {
        "raw": LocalLandingStore(tmp_path / "raw"),
        "artifacts": LocalLandingStore(tmp_path / "artifacts"),
        "quarantine": (tmp_path / "quarantine").as_uri(),
        "ns": ns,
        "root": tmp_path,
    }


@pytest.fixture
def snapshot(source, lake):
    """Take a consistent PostgreSQL snapshot into Bronze; one second apart (batch id order)."""
    taken, start = itertools.count(), datetime.now(UTC)

    def take() -> dict:
        when = start + timedelta(seconds=next(taken))
        connector = PostgresSnapshotConnector(
            source, lake["raw"], lake["artifacts"], CONTRACTS, clock=lambda: when
        )
        return {m.dataset: m for m in connector.snapshot()}

    return take


@pytest.fixture
def checkpoints():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="ca-central-1")
        ddb.create_table(
            TableName="cp",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield CheckpointStore(ddb, "cp")


@pytest.fixture
def dms(spark, lake):
    return DmsDouble(spark, lake["raw"], lake["root"], load_id="20261112T120000Z")


@pytest.fixture
def cdc_job(spark, lake, checkpoints):
    """Run the CDC Glue job's logic for one table, as the scheduled job does."""

    def run(spec: SilverTableSpec, **kw):
        return run_cdc_table(
            spark, spec, lake["raw"], CDC_ROOT, checkpoints,
            f"{lake['ns']}.{spec.silver_table}", lake["quarantine"], "e2e",
            complete_full_loads=frozenset({"20261112T120000Z"}), **kw,
        )  # fmt: skip

    return run
