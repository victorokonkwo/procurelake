"""Acceptance test 10: pipeline failure. "Interrupt a CDC apply job → recovers from a known
checkpoint."

Real committed changes are delivered, then the CDC job is interrupted at the two points that
matter (ADR 0028, C4):

1. after reading the input, before writing Silver;
2. after the Iceberg merge, before the checkpoint is recorded.

Each time the checkpoint stays where it was (the last successful run), the lease is released,
and the next run replays from that checkpoint. Silver ends equal to PostgreSQL with no duplicate
business keys, and the checkpoint records every file exactly once.
"""

from __future__ import annotations

import pytest
from cdc_flow import commit_workload, full_load
from e2e_support import SPECS, duplicate_keys, silver_state, source_state

import procurelake.cdc.job as cdc_job_module

pytestmark = [pytest.mark.spark, pytest.mark.postgres]


def test_interrupted_cdc_apply_recovers_from_its_checkpoint(
    spark, source, lake, dms, cdc_job, checkpoints, monkeypatch
):
    spec = SPECS["invoices"]
    fq = f"{lake['ns']}.{spec.silver_table}"
    full_load(dms, source)
    [loaded] = cdc_job(spec)
    known = checkpoints.checkpoint(spec.silver_table)
    assert known["batch_id"] == loaded.batch_id

    workload = commit_workload(source, seed=11, operations=60)
    assert dms.changes(spec, workload.changes)

    # 1. Interrupted after reading the input, before the Silver write.
    def die_before_write(*_a, **_k):
        raise RuntimeError("interrupted before the Silver write")

    monkeypatch.setattr(cdc_job_module, "apply_events", die_before_write)
    with pytest.raises(RuntimeError, match="before the Silver write"):
        cdc_job(spec)
    monkeypatch.undo()
    assert checkpoints.checkpoint(spec.silver_table) == known

    # 2. Interrupted after the merge, before the checkpoint.
    def die_before_checkpoint(*_a, **_k):
        raise RuntimeError("interrupted before the checkpoint")

    monkeypatch.setattr(checkpoints, "record_applied", die_before_checkpoint)
    with pytest.raises(RuntimeError, match="before the checkpoint"):
        cdc_job(spec)
    monkeypatch.undo()
    assert checkpoints.checkpoint(spec.silver_table) == known

    # Recovery: the next run replays from the known checkpoint.
    [recovered] = cdc_job(spec)
    assert recovered.status == "applied" and recovered.files == 1
    assert checkpoints.checkpoint(spec.silver_table)["batch_id"] == recovered.batch_id
    assert (
        len(checkpoints.applied_files(spec.silver_table)) == 2
    )  # the load file and the change file, once each
    assert duplicate_keys(spark, fq, spec) == 0
    assert silver_state(spark, fq, spec) == source_state(source, spec)
