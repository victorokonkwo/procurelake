"""Invariant tests for the synthetic data generator (no database required).

Sprint 1 Day 2 definition of done: "Repeatable seeded dataset with valid relationships".
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pytest

from procurelake.data_generator.config import load_config, scaled
from procurelake.data_generator.generate import generate_dataset
from procurelake.data_generator.model import Dataset

CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "tenants" / "synthetic_tenants.yaml"


@pytest.fixture(scope="module")
def config():
    return load_config(CONFIG_PATH)


@pytest.fixture(scope="module")
def dataset(config) -> Dataset:
    return generate_dataset(config, seed=42)


# --------------------------------------------------------------------------- repeatability
def test_same_seed_produces_identical_dataset(config, dataset):
    again = generate_dataset(config, seed=42)
    assert again.fingerprint() == dataset.fingerprint()
    assert again == dataset


def test_different_seed_produces_different_dataset(config, dataset):
    assert generate_dataset(config, seed=43).fingerprint() != dataset.fingerprint()


def test_tenant_streams_are_independent(config, dataset):
    """Removing one tenant must not change another tenant's rows."""
    alder_only = config.model_copy(update={"tenants": [config.tenants[0]]})
    alone = generate_dataset(alder_only, seed=42)
    for table, _, rows in dataset.tables():
        expected = [r for r in rows if r.tenant_id == "tenant_alder"]
        assert getattr(alone, table) == expected, table


# --------------------------------------------------------------------------- tenants
def test_two_distinct_synthetic_tenants_each_with_data_in_every_table(dataset):
    assert [t.tenant_id for t in dataset.tenants] == ["tenant_alder", "tenant_birch"]
    assert all(t.is_synthetic for t in dataset.tenants)
    for table, _, rows in dataset.tables():
        per_tenant = {r.tenant_id for r in rows}
        assert per_tenant == {"tenant_alder", "tenant_birch"}, table


def test_primary_keys_unique_across_whole_dataset(dataset):
    keys = {
        "agencies": "agency_id",
        "suppliers": "supplier_id",
        "solicitations": "solicitation_id",
        "awards": "award_id",
        "purchase_orders": "po_id",
        "invoices": "invoice_id",
    }
    for table, key in keys.items():
        values = [getattr(r, key) for r in getattr(dataset, table)]
        assert len(values) == len(set(values)), table


# --------------------------------------------------------------------------- relationships
def test_every_foreign_key_resolves_within_the_same_tenant(dataset):
    agency = {a.agency_id: a.tenant_id for a in dataset.agencies}
    supplier = {s.supplier_id: s.tenant_id for s in dataset.suppliers}
    solicitation = {s.solicitation_id: s for s in dataset.solicitations}
    award = {a.award_id: a for a in dataset.awards}
    po = {p.po_id: p for p in dataset.purchase_orders}

    for s in dataset.solicitations:
        assert agency[s.agency_id] == s.tenant_id
    for a in dataset.awards:
        assert solicitation[a.solicitation_id].tenant_id == a.tenant_id
        assert supplier[a.supplier_id] == a.tenant_id
    for p in dataset.purchase_orders:
        assert agency[p.agency_id] == p.tenant_id
        assert supplier[p.supplier_id] == p.tenant_id
        if p.award_id is not None:
            assert award[p.award_id].tenant_id == p.tenant_id
    for i in dataset.invoices:
        assert po[i.po_id].tenant_id == i.tenant_id


def test_awards_only_exist_for_awarded_solicitations(dataset):
    solicitation = {s.solicitation_id: s for s in dataset.solicitations}
    for a in dataset.awards:
        assert solicitation[a.solicitation_id].status == "awarded"
    awarded_ids = {a.solicitation_id for a in dataset.awards}
    for s in dataset.solicitations:
        if s.status == "awarded":
            assert s.solicitation_id in awarded_ids


def test_award_purchase_orders_match_award_supplier_agency_and_currency(dataset):
    solicitation = {s.solicitation_id: s for s in dataset.solicitations}
    award = {a.award_id: a for a in dataset.awards}
    for p in dataset.purchase_orders:
        if p.award_id is None:
            continue
        a = award[p.award_id]
        assert a.status == "active"
        assert p.supplier_id == a.supplier_id
        assert p.agency_id == solicitation[a.solicitation_id].agency_id
        assert p.currency_code == a.currency_code
        assert p.order_date >= a.awarded_at.date()


def test_open_market_purchase_orders_are_present(dataset):
    assert any(p.award_id is None for p in dataset.purchase_orders)


# --------------------------------------------------------------------------- money
def test_purchase_orders_never_exceed_their_award(dataset):
    committed: dict = defaultdict(Decimal)
    for p in dataset.purchase_orders:
        if p.award_id is not None:
            committed[p.award_id] += p.amount
    award = {a.award_id: a for a in dataset.awards}
    for award_id, total in committed.items():
        assert total <= award[award_id].award_amount


def test_invoices_never_exceed_po_and_use_po_currency(dataset):
    po = {p.po_id: p for p in dataset.purchase_orders}
    invoiced: dict = defaultdict(Decimal)
    for i in dataset.invoices:
        invoiced[i.po_id] += i.invoice_amount
        assert i.currency_code == po[i.po_id].currency_code
    for po_id, total in invoiced.items():
        assert total <= po[po_id].amount


def test_amounts_positive_with_cent_precision(dataset):
    for value in (
        [a.award_amount for a in dataset.awards]
        + [p.amount for p in dataset.purchase_orders]
        + [i.invoice_amount for i in dataset.invoices]
    ):
        assert value > 0
        assert value == value.quantize(Decimal("0.01"))


def test_currency_mix_follows_tenant_config(dataset):
    alder = {a.currency_code for a in dataset.awards if a.tenant_id == "tenant_alder"}
    birch = {a.currency_code for a in dataset.awards if a.tenant_id == "tenant_birch"}
    assert alder == {"CAD"}
    assert birch == {"CAD", "USD"}


# --------------------------------------------------------------------------- lifecycle / time
def test_nothing_happens_after_as_of(config, dataset):
    as_of = config.as_of
    for _, _, rows in dataset.tables():
        for r in rows:
            assert r.created_at <= as_of
            assert r.updated_at <= as_of
            assert r.created_at <= r.updated_at
    for s in dataset.solicitations:
        if s.published_at is not None:
            assert s.published_at <= as_of
    for i in dataset.invoices:
        if i.paid_at is not None:
            assert i.paid_at <= as_of


def test_solicitation_status_matches_timeline(dataset):
    for s in dataset.solicitations:
        if s.status == "draft":
            assert s.published_at is None and s.closing_at is None
        else:
            assert s.published_at is not None and s.closing_at > s.published_at
            assert s.published_at >= s.created_at


def test_awards_follow_solicitation_close(dataset):
    solicitation = {s.solicitation_id: s for s in dataset.solicitations}
    for a in dataset.awards:
        assert a.awarded_at > solicitation[a.solicitation_id].closing_at


def test_invoice_payment_state_is_consistent(dataset):
    for i in dataset.invoices:
        if i.payment_status == "paid":
            assert i.paid_amount == i.invoice_amount and i.paid_at is not None
        elif i.payment_status == "partially_paid":
            assert 0 < i.paid_amount < i.invoice_amount and i.paid_at is not None
        else:
            assert i.paid_amount == 0 and i.paid_at is None


def test_closed_purchase_orders_are_fully_invoiced_and_paid(dataset):
    by_po = defaultdict(list)
    for i in dataset.invoices:
        by_po[i.po_id].append(i)
    for p in dataset.purchase_orders:
        active = [i for i in by_po[p.po_id] if i.payment_status != "cancelled"]
        if p.status == "closed":
            assert sum(i.invoice_amount for i in active) == p.amount
            assert all(i.payment_status == "paid" for i in active)
        if p.status == "cancelled":
            assert by_po[p.po_id] == []


def test_every_lifecycle_state_is_represented(dataset):
    """The dataset should exercise every status the schema allows (except tenant states)."""
    assert {s.status for s in dataset.solicitations} == {
        "draft",
        "published",
        "closed",
        "awarded",
        "cancelled",
    }
    assert {a.status for a in dataset.awards} == {"active", "cancelled"}
    assert {p.status for p in dataset.purchase_orders} == {"open", "closed", "cancelled"}
    assert {i.payment_status for i in dataset.invoices} == {"pending", "partially_paid", "paid", "cancelled"}


# --------------------------------------------------------------------------- data quality fixtures
def test_near_duplicate_suppliers_exist_for_silver_deduplication(dataset):
    def normalise(name: str) -> str:
        return " ".join(
            name.upper()
            .rstrip(". ")
            .replace("LIMITED", "LTD")
            .replace("INCORPORATED", "INC")
            .replace("CORPORATION", "CORP")
            .replace(".", "")
            .split()
        )

    for tenant in ("tenant_alder", "tenant_birch"):
        names = [s.supplier_name for s in dataset.suppliers if s.tenant_id == tenant]
        assert len({normalise(n) for n in names}) < len(names), tenant
        assert len(set(names)) == len(names), "exact duplicate names are not generated"


# --------------------------------------------------------------------------- scaling
def test_scale_multiplies_volume_and_stays_valid(config):
    big = generate_dataset(scaled(config, 3), seed=42)
    base = generate_dataset(config, seed=42)
    assert len(big.solicitations) == 3 * len(base.solicitations)
    assert len(big.suppliers) > 2 * len(base.suppliers)
