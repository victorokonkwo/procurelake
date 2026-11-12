"""Demo helpers: validated SQL inputs and DMS event selection (no AWS)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from procurelake.operations import demo

AWARD = "0b6f2a1e-3c4d-4e5f-8a9b-0c1d2e3f4a5b"


def test_keys_are_canonical_uuids_or_refused():
    assert demo.checked_uuid(AWARD.upper()) == AWARD
    with pytest.raises(ValueError):
        demo.checked_uuid("x' or '1'='1")


@pytest.mark.parametrize("bad", ["0", "-5", "1.234", "nan", "abc", "1e17"])
def test_amount_must_fit_the_award_column(bad):
    with pytest.raises(ValueError):
        demo.checked_amount(bad)


def test_valid_amount_is_decimal():
    assert demo.checked_amount("125000.50") == Decimal("125000.50")


def test_sql_uses_validated_identifiers_only():
    assert f"award_id = '{AWARD}'" in demo.silver_award_sql("silver_db", AWARD)
    assert "from gold_db.fct_contract_award" in demo.gold_fact_sql("gold_db", AWARD)
    assert "mart_awarded_value" in demo.gold_mart_sql("gold_db", AWARD, AWARD)
    with pytest.raises(ValueError):
        demo.silver_award_sql("silver; drop", AWARD)


def test_award_events_are_filtered_and_ordered_by_commit():
    rows = [
        {
            "award_id": AWARD,
            "Op": "U",
            "_dms_commit_ts": "2026-11-12 16:00:02.000000",
            "award_amount": 2,
            "status": "active",
        },
        {
            "award_id": "other",
            "Op": "U",
            "_dms_commit_ts": "2026-11-12 16:00:00.000000",
            "award_amount": 9,
            "status": "active",
        },
        {
            "award_id": AWARD,
            "Op": "U",
            "_dms_commit_ts": "2026-11-12 16:00:01.000000",
            "award_amount": 1,
            "status": "active",
        },
    ]
    events = demo.award_events(rows, AWARD)
    assert [e["award_amount"] for e in events] == [1, 2]
    assert {e["op"] for e in events} == {"U"}
