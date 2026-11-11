"""Iceberg maintenance: statements, health parsing, leases, retries (fake Athena; moto DynamoDB)."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from procurelake.cdc.checkpoint import CheckpointStore
from procurelake.lakehouse.maintenance import Athena, Retention, TableTarget, run, statements
from procurelake.lakehouse.maintenance_job import parse_args, targets

CHECKPOINTS = "procurelake-dev-cdc-checkpoints"
SILVER = Retention(604800, 10)
GOLD = Retention(86400, 5)


class FakeAthena:
    """Answers health queries from a scripted table state; records every statement."""

    def __init__(self, states: dict[str, list[tuple]], fail: dict[str, list[str]] | None = None):
        self.states = {k: list(v) for k, v in states.items()}  # table -> successive health rows
        self.fail = fail or {}  # statement prefix -> errors to raise, in order
        self.sql: list[str] = []
        self._results: dict[str, tuple[str, str, list]] = {}

    def start_query_execution(self, QueryString, WorkGroup, QueryExecutionContext):
        qid = f"q{len(self.sql)}"
        self.sql.append(QueryString)
        verb = QueryString.split(" ", 1)[0]
        errors = self.fail.get(verb, [])
        if errors:
            self._results[qid] = ("FAILED", errors.pop(0), [])
        elif "$snapshots" in QueryString:
            table = QueryString.split('"."', 1)[1].split("$", 1)[0]
            row = self.states[table].pop(0) if len(self.states[table]) > 1 else self.states[table][0]
            self._results[qid] = ("SUCCEEDED", "", [row])
        else:
            self._results[qid] = ("SUCCEEDED", "", [])
        return {"QueryExecutionId": qid}

    def get_query_execution(self, QueryExecutionId):
        state, reason, _ = self._results[QueryExecutionId]
        return {
            "QueryExecution": {
                "Status": {"State": state, "StateChangeReason": reason},
                "Statistics": {"DataScannedInBytes": 100, "EngineExecutionTimeInMillis": 10},
                "ResultConfiguration": {"OutputLocation": "s3://x/"},
            }
        }

    def get_paginator(self, _name):
        fake = self

        class Paginator:
            def paginate(self, QueryExecutionId):
                rows = fake._results[QueryExecutionId][2]
                header = {"Data": [{"VarCharValue": f"c{i}"} for i in range(7)]}
                data = [{"Data": [{"VarCharValue": str(v)} for v in r]} for r in rows]
                columns = [{"Name": f"c{i}"} for i in range(7)]
                yield {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": columns}, "Rows": [header, *data]}}

        return Paginator()

    def stop_query_execution(self, QueryExecutionId):
        pass


# (snapshots, data files, delete files, records, position deletes, equality deletes, bytes)
BLOATED = (51, 47, 47, 82054, 80309, 0, 876640)
COMPACT = (5, 1, 0, 1745, 0, 0, 18000)


@pytest.fixture
def checkpoints():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="ca-central-1")
        ddb.create_table(
            TableName=CHECKPOINTS,
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
        yield CheckpointStore(ddb, CHECKPOINTS)


def _athena(fake):
    return Athena(fake, "wg", sleep=lambda _s: None)


def test_statements_set_retention_then_optimize_then_vacuum():
    sql = statements(TableTarget("procurelake_dev_gold", "mart_actual_spend", GOLD, False))
    assert sql == [
        "ALTER TABLE procurelake_dev_gold.mart_actual_spend SET TBLPROPERTIES ("
        "'vacuum_max_snapshot_age_seconds'='86400', 'vacuum_min_snapshots_to_keep'='5')",
        "OPTIMIZE procurelake_dev_gold.mart_actual_spend REWRITE DATA USING BIN_PACK",
        "VACUUM procurelake_dev_gold.mart_actual_spend",
    ]


def test_unsafe_names_and_retention_rejected():
    with pytest.raises(ValueError, match="unsafe"):
        TableTarget("db", "x; DROP TABLE y", GOLD, False)
    with pytest.raises(ValueError, match="at least"):
        Retention(60, 1)


def test_maintains_and_measures(checkpoints):
    fake = FakeAthena({"mart_actual_spend": [BLOATED, COMPACT]})
    (result,) = run(
        _athena(fake), [TableTarget("gold", "mart_actual_spend", GOLD, False)], checkpoints, "m:1"
    )
    assert result.status == "maintained"
    assert (result.before.data_files, result.before.delete_files, result.before.live_records) == (
        47,
        47,
        1745,
    )
    assert (result.after.data_files, result.after.snapshots) == (1, 5)
    assert [s["sql"] for s in result.statements] == ["ALTER", "OPTIMIZE", "VACUUM"]


def test_leased_table_is_skipped_while_cdc_holds_it(checkpoints):
    checkpoints.acquire_lease("silver_invoices", "glue:jr_1", 1800)
    fake = FakeAthena({"silver_invoices": [COMPACT]})
    (result,) = run(
        _athena(fake), [TableTarget("silver", "silver_invoices", SILVER, True)], checkpoints, "m:1"
    )
    assert result.status == "skipped_lease_held"
    assert fake.sql == []


def test_lease_is_taken_and_released(checkpoints):
    fake = FakeAthena({"silver_invoices": [COMPACT]})
    (result,) = run(
        _athena(fake), [TableTarget("silver", "silver_invoices", SILVER, True)], checkpoints, "m:1"
    )
    assert result.status == "maintained"
    checkpoints.acquire_lease("silver_invoices", "glue:jr_2", 1800)  # free again: CDC can take it


def test_commit_conflict_is_retried_and_other_failures_are_reported(checkpoints):
    fake = FakeAthena(
        {"a": [COMPACT], "b": [COMPACT]},
        fail={"OPTIMIZE": ["ICEBERG_COMMIT_ERROR: conflict"], "VACUUM": [], "ALTER": []},
    )
    targets_ = [TableTarget("gold", "a", GOLD, False), TableTarget("gold", "b", GOLD, False)]
    results = run(_athena(fake), targets_, checkpoints, "m:1")
    assert [r.status for r in results] == ["maintained", "maintained"]
    assert sum(1 for s in fake.sql if s.startswith("OPTIMIZE")) == 3  # one retry, then table b

    fake = FakeAthena({"a": [COMPACT], "b": [COMPACT]}, fail={"VACUUM": ["ACCESS_DENIED"]})
    results = run(_athena(fake), targets_, checkpoints, "m:1")
    assert [r.status for r in results] == ["failed", "maintained"]
    assert "ACCESS_DENIED" in results[0].error


def test_changed_live_records_fail_the_table(checkpoints):
    grown = (6, 1, 0, 1800, 0, 0, 18000)
    fake = FakeAthena({"a": [COMPACT, grown]})
    (result,) = run(_athena(fake), [TableTarget("gold", "a", GOLD, False)], checkpoints, "m:1")
    assert result.status == "failed" and "live records changed" in result.error


def test_job_arguments_build_targets():
    args = parse_args([
        "--workgroup", "wg", "--silver-database", "s", "--gold-database", "g", "--report-bucket", "b",
        "--checkpoint-table", "c", "--silver-tables", "silver_a,silver_b", "--leased-tables", "silver_a",
        "--gold-tables", "mart_x", "--silver-max-age-seconds", "604800", "--silver-min-snapshots", "10",
        "--gold-max-age-seconds", "86400", "--gold-min-snapshots", "5", "--JOB_RUN_ID", "jr_9",
        "--extra", "x",
    ])  # fmt: skip
    built = targets(args)
    assert [(t.fq, t.lease, t.retention) for t in built] == [
        ("s.silver_a", True, SILVER), ("s.silver_b", False, SILVER), ("g.mart_x", False, GOLD),
    ]  # fmt: skip
    args.leased_tables = "mart_x"
    with pytest.raises(ValueError, match="must be Silver"):
        targets(args)
