"""CI plan summary (ADR 0035): addresses and actions only, and a stable fingerprint."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "tf_plan_summary.py"
spec = importlib.util.spec_from_file_location("tf_plan_summary", SCRIPT)
tps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tps)


def _rc(address, actions, after=None):
    return {"address": address, "change": {"actions": actions, "after": after or {"secret": "x"}}}


PLAN = {
    "resource_changes": [
        _rc("module.a.aws_glue_job.this", ["update"], {"role_arn": "arn:aws:iam::111122223333:role/x"}),
        _rc("aws_s3_object.wheel", ["delete", "create"]),
        _rc("aws_iam_role.new", ["create"]),
        _rc("aws_kms_key.same", ["no-op"]),
        _rc("data.aws_iam_policy_document.x", ["read"]),
        _rc("aws_sns_topic.old", ["delete"]),
    ]
}


def test_lists_changed_addresses_and_actions_only():
    out = tps.summarise(PLAN, "data_platform")
    assert out["changes"] == 4
    md = out["markdown"]
    assert md.startswith("### data_platform: 1 to create, 1 to update, 1 to replace, 1 to delete")
    assert "- **replace** `aws_s3_object.wheel`" in md
    assert "aws_kms_key.same" not in md and "data.aws_iam_policy_document" not in md
    assert "111122223333" not in md and "secret" not in md  # no attribute values, ever


def test_fingerprint_ignores_order_but_not_actions_or_planned_values():
    a = tps.summarise(PLAN, "r")["fingerprint"]
    reordered = {"resource_changes": list(reversed(PLAN["resource_changes"]))}
    other_values = {
        "resource_changes": [_rc(r["address"], r["change"]["actions"]) for r in PLAN["resource_changes"]]
    }
    assert tps.summarise(reordered, "r")["fingerprint"] == a
    assert tps.summarise(other_values, "r")["fingerprint"] != a  # same address, different update
    changed = {"resource_changes": [*PLAN["resource_changes"], _rc("aws_iam_role.extra", ["create"])]}
    assert tps.summarise(changed, "r")["fingerprint"] != a


def test_no_changes_and_unexpected_actions():
    assert tps.summarise({"resource_changes": [_rc("x.y", ["no-op"])]}, "foundation") == {
        "root": "foundation",
        "changes": 0,
        "fingerprint": tps.summarise({}, "foundation")["fingerprint"],
        "markdown": "### foundation: no changes\n",
    }
    with pytest.raises(ValueError, match="unexpected"):
        tps.action_label(["bogus"])
