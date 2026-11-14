"""USAspending connector tests against a local fake API (no network)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

import pytest
from fake_usaspending import FakeUsaspending

from procurelake.common.retry import RetryPolicy
from procurelake.ingestion.connectors.usaspending import (
    SCHEMA_VERSION,
    UsaspendingConfig,
    UsaspendingConnector,
    pending_days,
    resweep_days,
)
from procurelake.ingestion.http import HttpClient
from procurelake.ingestion.landing import LocalLandingStore
from procurelake.ingestion.manifest import load_manifests

DAY = date(2026, 10, 20)  # 21 days before NOW: outside the 10-day settling period
NOW = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)


@pytest.fixture
def stores(tmp_path):
    return LocalLandingStore(tmp_path / "raw"), LocalLandingStore(tmp_path / "artifacts")


def _connector(httpserver, stores, **config):
    raw, manifests = stores
    http = HttpClient(retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01), sleep=lambda _: None)
    return UsaspendingConnector(
        http, raw, manifests, UsaspendingConfig(base_url=httpserver.url_for(""), **config), clock=lambda: NOW
    )


def test_lands_every_page_despite_hasnext_quirk_and_reconciles(httpserver, stores):
    fake = FakeUsaspending(record_count=10_250)  # 103 pages: hasNext turns false at page 100
    fake.install(httpserver)
    manifest = _connector(httpserver, stores).ingest_day(DAY)

    assert manifest.status == "succeeded", manifest.reconciliation
    assert len(manifest.files) == 103
    assert manifest.reconciliation.landed_record_count == 10_250
    assert manifest.reconciliation.unique_record_count == 10_250
    assert (
        manifest.reconciliation.expected_count_before
        == manifest.reconciliation.expected_count_after
        == 10_250
    )
    assert manifest.schema_version == SCHEMA_VERSION
    assert fake.count_requests == 2  # before and after collection


def test_raw_pages_are_stored_byte_for_byte_with_checksums(httpserver, stores):
    fake = FakeUsaspending(record_count=150)
    fake.install(httpserver)
    raw, _ = stores
    manifest = _connector(httpserver, stores).ingest_day(DAY)

    page1 = raw.get(manifest.files[0].key)
    assert json.loads(page1)["results"][0]["generated_internal_id"] == "CONT_AWD_0000000"
    assert manifest.files[0].key.startswith(
        f"bronze/usaspending/spending_by_award/ingestion_date=2026-11-10/batch_id={manifest.batch_id}/"
    )
    assert manifest.files[0].sha256 == hashlib.sha256(page1).hexdigest()
    assert [f.record_count for f in manifest.files] == [100, 50]


def test_requests_use_documented_contract(httpserver, stores):
    fake = FakeUsaspending(record_count=5)
    fake.install(httpserver)
    _connector(httpserver, stores).ingest_day(DAY)

    payload = fake.page_requests[0]
    assert payload["limit"] == 100 and payload["page"] == 1
    assert payload["sort"] == "generated_internal_id" and payload["order"] == "asc"
    assert payload["filters"]["award_type_codes"] == ["A", "B", "C", "D"]
    assert payload["filters"]["time_period"] == [
        {"start_date": "2026-10-20", "end_date": "2026-10-20", "date_type": "last_modified_date"}
    ]


def test_manifest_is_written_last_and_is_loadable(httpserver, stores):
    FakeUsaspending(record_count=42).install(httpserver)
    _, manifest_store = stores
    manifest = _connector(httpserver, stores).ingest_day(DAY)

    loaded = load_manifests(manifest_store, "usaspending", "spending_by_award")
    assert [m.batch_id for m in loaded] == [manifest.batch_id]
    assert loaded[0].files == manifest.files


def test_empty_day_succeeds_with_one_empty_page(httpserver, stores):
    FakeUsaspending(record_count=0).install(httpserver)
    manifest = _connector(httpserver, stores).ingest_day(DAY)
    assert manifest.status == "succeeded"
    assert [f.record_count for f in manifest.files] == [0]


def test_source_count_changing_during_collection_fails_batch(httpserver, stores):
    FakeUsaspending(record_count=120, count_sequence=[120, 121]).install(httpserver)
    manifest = _connector(httpserver, stores).ingest_day(DAY)
    assert manifest.status == "failed"
    assert "count changed" in manifest.reconciliation.detail


def test_duplicate_records_across_pages_fail_batch(httpserver, stores):
    FakeUsaspending(record_count=250, duplicate_on_page=2).install(httpserver)
    manifest = _connector(httpserver, stores).ingest_day(DAY)
    assert manifest.status == "failed"
    assert manifest.reconciliation.unique_record_count < manifest.reconciliation.landed_record_count


def test_transient_errors_are_retried(httpserver, stores):
    fake = FakeUsaspending(record_count=30, fail_first_n_page_requests=2)
    fake.install(httpserver)
    manifest = _connector(httpserver, stores).ingest_day(DAY)
    assert manifest.status == "succeeded"
    assert len(fake.page_requests) == 3  # two 503s, then success


def test_permanent_error_writes_failed_manifest_with_error(httpserver, stores):
    FakeUsaspending(record_count=300, permanent_error_on_page=2).install(httpserver)
    _, manifest_store = stores
    manifest = _connector(httpserver, stores).ingest_day(DAY)

    assert manifest.status == "failed"
    assert "HTTP 422" in manifest.error
    assert len(manifest.files) == 1  # page 1 stays landed (immutable) for investigation
    assert load_manifests(manifest_store, "usaspending", "spending_by_award")[0].status == "failed"


@pytest.mark.parametrize(
    "day", [date(2026, 11, 10), date(2026, 11, 11), date(2007, 9, 30), date(2026, 11, 8)]
)
def test_rejects_incomplete_future_or_unsupported_days(httpserver, stores, day):
    with pytest.raises(ValueError):
        _connector(httpserver, stores).ingest_day(day)


def test_config_rejects_page_size_above_api_max():
    with pytest.raises(ValueError):
        UsaspendingConfig(page_size=101)


def test_pending_days_fills_gaps_and_retries_failed_days(httpserver, stores):
    FakeUsaspending(record_count=3).install(httpserver)
    ok = _connector(httpserver, stores).ingest_day(date(2026, 10, 21))
    assert ok.status == "succeeded" and ok.window_settled

    _, manifest_store = stores
    manifests = load_manifests(manifest_store, "usaspending", "spending_by_award")
    assert pending_days(manifests, date(2026, 10, 20), date(2026, 10, 23)) == [
        date(2026, 10, 20),
        date(2026, 10, 22),
        date(2026, 10, 23),
    ]
    assert pending_days(manifests, date(2026, 10, 20), date(2026, 10, 23), max_days=1) == [date(2026, 10, 20)]


def test_unsettled_batches_land_but_do_not_complete_the_window(httpserver, stores):
    FakeUsaspending(record_count=3).install(httpserver)
    manifest = _connector(httpserver, stores).ingest_day(date(2026, 11, 8), allow_unsettled=True)
    assert manifest.status == "succeeded"
    assert manifest.window_settled is False

    _, manifest_store = stores
    manifests = load_manifests(manifest_store, "usaspending", "spending_by_award")
    assert pending_days(manifests, date(2026, 11, 8), date(2026, 11, 8)) == [date(2026, 11, 8)]


def test_manifests_without_settled_field_are_treated_as_unsettled():
    from procurelake.ingestion.manifest import IngestionManifest

    legacy = {
        "source_system": "usaspending",
        "dataset": "spending_by_award",
        "schema_version": SCHEMA_VERSION,
        "batch_id": "b1",
        "status": "succeeded",
        "ingestion_started_at": "2026-11-10T08:12:13Z",
        "ingestion_completed_at": "2026-11-10T08:12:14Z",
        "window": {"date_type": "last_modified_date", "start_date": "2026-11-08", "end_date": "2026-11-08"},
        "code_version": "0.1.0",
    }
    manifest = IngestionManifest.model_validate(legacy)
    assert manifest.window_settled is False
    assert pending_days([manifest], date(2026, 11, 8), date(2026, 11, 8)) == [date(2026, 11, 8)]


def test_resweep_days_are_the_most_recent_settled_days_oldest_first():
    # As of 2026-11-13 with a 10-day settling period, the last settled day is 2026-11-03.
    assert resweep_days(date(2026, 11, 13), 10, 3) == [
        date(2026, 11, 1),
        date(2026, 11, 2),
        date(2026, 11, 3),
    ]
    assert len(resweep_days(date(2026, 11, 13), 10, 30)) == 30
    with pytest.raises(ValueError):
        resweep_days(date(2026, 11, 13), 10, 0)


def test_purpose_is_recorded_and_a_resweep_lands_a_new_batch_for_a_collected_day(httpserver, stores):
    FakeUsaspending(record_count=5).install(httpserver)
    connector = _connector(httpserver, stores)
    daily = connector.ingest_day(DAY)
    resweep = connector.ingest_day(DAY, purpose="resweep")
    assert (daily.request["purpose"], resweep.request["purpose"]) == ("daily", "resweep")
    assert daily.batch_id != resweep.batch_id and resweep.status == "succeeded"
    assert len(load_manifests(stores[1], "usaspending", "spending_by_award")) == 2
