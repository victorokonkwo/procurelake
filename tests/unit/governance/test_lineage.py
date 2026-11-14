"""Source-to-Gold lineage and the catalog CLI (ADR 0042). The manifest is a small hand-built
dbt manifest (test fixture) with the fields the lineage reads; Glue is a botocore Stubber."""

from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber

from procurelake.governance import cli
from procurelake.governance.catalog import reference_metadata, silver_metadata
from procurelake.governance.lineage import gold_lineage, to_markdown
from procurelake.lakehouse.silver.resources import load_reference, load_silver_specs

SPECS = load_silver_specs()


def _model(
    name: str, path: str, deps: list[str], meta: dict | None = None, materialized: str = "table"
) -> dict:
    return {
        "resource_type": "model",
        "name": name,
        "original_file_path": path,
        "depends_on": {"nodes": deps, "macros": []},
        "config": {"materialized": materialized, "meta": meta or {}},
    }


META = {
    "grain": "g",
    "owner": "procurement-analytics",
    "freshness": "24h",
    "data_classification": "synthetic",
}
MANIFEST = {
    "sources": {
        "source.p.silver.silver_awards": {"name": "silver_awards"},
        "source.p.silver.silver_solicitations": {"name": "silver_solicitations"},
        "source.p.silver_ai.silver_contract_intelligence": {"name": "silver_contract_intelligence"},
    },
    "nodes": {
        "model.p.stg_awards": _model(
            "stg_awards", "models/staging/postgres/stg_awards.sql", ["source.p.silver.silver_awards"],
            materialized="ephemeral",
        ),
        "model.p.stg_solicitations": _model(
            "stg_solicitations", "models/staging/postgres/stg_solicitations.sql",
            ["source.p.silver.silver_solicitations"], materialized="ephemeral",
        ),
        "model.p.fct_contract_award": _model(
            "fct_contract_award", "models/core/fct_contract_award.sql",
            ["model.p.stg_awards", "model.p.stg_solicitations"], META,
        ),
        "model.p.mart_awarded_value": _model(
            "mart_awarded_value", "models/marts/procurement/mart_awarded_value.sql",
            ["model.p.fct_contract_award"], META,
        ),
        "model.p.mart_ai": _model(
            "mart_ai", "models/marts/ai_intelligence/mart_ai.sql",
            ["source.p.silver_ai.silver_contract_intelligence"], {**META, "data_classification": "public"},
        ),
        "test.p.unique_x": {"resource_type": "test", "name": "unique_x", "depends_on": {"nodes": []}},
    },
}  # fmt: skip


def by_model() -> dict:
    return {g.model: g for g in gold_lineage(MANIFEST, SPECS)}


def test_only_gold_models_are_reported():
    assert sorted(by_model()) == ["fct_contract_award", "mart_ai", "mart_awarded_value"]


def test_mart_reaches_silver_and_source_through_its_fact():
    mart = by_model()["mart_awarded_value"]
    assert mart.inputs == ("fct_contract_award",)  # its direct Gold input, not the Silver tables
    assert mart.upstream_models == ("fct_contract_award", "stg_awards", "stg_solicitations")
    awards = {s.silver_table: s for s in mart.silver}["silver_awards"]
    assert awards.source_table == "procurement.awards"
    assert awards.owner == SPECS["awards"].contract.owner
    # The real Bronze layouts: snapshot connector and DMS (one load_id per dev/cdc stack).
    assert awards.bronze_prefixes == (
        "bronze/postgres/awards/",
        "bronze/postgres_cdc/load_id=*/procurement/awards/",
    )


def test_staging_is_collapsed_into_the_silver_tables_it_reads():
    assert by_model()["fct_contract_award"].inputs == ("silver_awards", "silver_solicitations")


def test_silver_without_a_contract_has_no_invented_source():
    (ai,) = by_model()["mart_ai"].silver
    assert (ai.silver_table, ai.source_table, ai.bronze_prefixes, ai.owner) == (
        "silver_contract_intelligence", None, (), None,
    )  # fmt: skip


def test_markdown_has_the_graph_and_one_section_per_gold_model():
    md = to_markdown(gold_lineage(MANIFEST, SPECS))
    assert 'src_procurement_awards["procurement.awards"] --> silver_awards' in md
    assert "fct_contract_award --> mart_awarded_value" in md
    assert "silver_awards --> mart_awarded_value" not in md  # transitive edges are not drawn
    assert md.count("\n## ") == 3
    assert "(no source contract)" in md


def test_cli_lineage_writes_the_report(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(__import__("json").dumps(MANIFEST))
    out = tmp_path / "lineage" / "lineage.md"
    assert cli.main(["lineage", "--manifest", str(manifest), "--out", str(out)]) == 0
    assert out.read_text().startswith("# ProcureLake lineage")


def test_cli_lineage_without_a_manifest_says_to_parse(tmp_path):
    with pytest.raises(SystemExit, match="make dbt-parse"):
        cli.main(["lineage", "--manifest", str(tmp_path / "none.json"), "--out", str(tmp_path / "x.md")])


def test_cli_check_fails_on_drift_and_passes_when_declared(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(__import__("json").dumps(MANIFEST))
    glue = boto3.client("glue", region_name="ca-central-1", aws_access_key_id="x", aws_secret_access_key="x")

    class Session:
        def client(self, name):
            assert name == "glue"
            return glue

    monkeypatch.setattr(cli, "build_session", lambda **_: Session())

    def tables(gold_desc: str | None) -> list[dict]:
        out = []
        for spec in SPECS.values():
            m = silver_metadata(spec)
            out.append({"Name": spec.silver_table, "DatabaseName": "s", "Description": m.description,
                        "Parameters": dict(m.parameters)})  # fmt: skip
        for ref in load_reference().values():
            m = reference_metadata(ref)
            out.append({"Name": ref.silver_table, "DatabaseName": "s", "Description": m.description,
                        "Parameters": dict(m.parameters)})  # fmt: skip
        public = {**META, "data_classification": "public"}
        for name, meta in (("fct_contract_award", META), ("mart_ai", public), ("mart_awarded_value", META)):
            table = {"Name": name, "DatabaseName": "g", "Parameters": dict(meta)}
            if gold_desc:
                table["Description"] = gold_desc
            out.append(table)
        return out

    argv = ["check", "--manifest", str(manifest), "--silver-database", "s", "--gold-database", "g"]
    for gold_desc, code in (("described", 0), (None, 1)):
        with Stubber(glue) as stub:
            for t in tables(gold_desc):
                stub.add_response(
                    "get_table", {"Table": t}, {"DatabaseName": t["DatabaseName"], "Name": t["Name"]}
                )
            assert cli.main(argv) == code
            stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert "FAIL  g.mart_ai: drift (Description)" in out
    assert f"{len(SPECS) + len(load_reference()) + 3} tables checked, 0 not as declared" in out
