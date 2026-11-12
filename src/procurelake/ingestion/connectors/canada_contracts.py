"""Government of Canada "Proactive Publication - Contracts": contracts over $10,000 (ADR 0039).

Public reference data, Open Government Licence - Canada (catalog ``license_id`` ca-ogl-lgo).
Kept as its own source with its provenance; it is never mixed into the synthetic tenants.

Dataset ``d8f85d91-7dec-4fd1-8055-483b77225d8b``, resource
``fac950c0-00d5-4ec1-a4d3-9cbebf98a305`` (one CSV, replaced in place, catalog frequency P3M).

Measured on the 2026-11-12 file:
- 655,507,792 bytes; ``Last-Modified: Thu, 12 Nov 2026 06:56:19 GMT``; SHA-256 ``bb0fbab2…8375``;
- UTF-8 with a byte-order mark; 43 columns (the 41 fields of the published schema
  ``recombinant-published-schema/contracts.json``, plus ``owner_org`` and ``owner_org_title``);
- 1,336,184 records, none malformed, no empty keys;
- ``reference_number`` alone is **not** unique (529,862 distinct values; departments reuse
  numbers). ``(owner_org, reference_number)`` is unique on every record, so it is the key.

Amounts (``contract_value``, ``original_value``, ``amendment_value``) are Canadian dollars by
the publication's definition. They are kept as text in Bronze; no conversion happens anywhere.
"""

from __future__ import annotations

from procurelake.ingestion.connectors.csv_snapshot import CsvSource

DATASET_ID = "d8f85d91-7dec-4fd1-8055-483b77225d8b"
RESOURCE_ID = "fac950c0-00d5-4ec1-a4d3-9cbebf98a305"
DOWNLOAD_URL = (
    f"https://open.canada.ca/data/dataset/{DATASET_ID}/resource/{RESOURCE_ID}/download/contracts.csv"
)

COLUMNS: tuple[str, ...] = (
    "reference_number", "procurement_id", "vendor_name", "vendor_postal_code", "buyer_name",
    "contract_date", "economic_object_code", "description_en", "description_fr",
    "contract_period_start", "delivery_date", "contract_value", "original_value", "amendment_value",
    "comments_en", "comments_fr", "additional_comments_en", "additional_comments_fr",
    "agreement_type_code", "trade_agreement", "land_claims", "commodity_type", "commodity_code",
    "country_of_vendor", "solicitation_procedure", "limited_tendering_reason",
    "trade_agreement_exceptions", "indigenous_business", "indigenous_business_excluding_psib",
    "intellectual_property", "potential_commercial_exploitation", "former_public_servant",
    "contracting_entity", "standing_offer_number", "instrument_type", "ministers_office",
    "number_of_bids", "article_6_exceptions", "award_criteria", "socioeconomic_indicator",
    "reporting_period", "owner_org", "owner_org_title",
)  # fmt: skip

SOURCE = CsvSource(
    source_system="canada_open_data",
    dataset="contracts_over_10k",
    # Bump when COLUMNS change: a changed header fails the batch rather than drifting silently.
    schema_version="canada_open_data.contracts_over_10k.v1",
    file_name="contracts.csv",
    url=DOWNLOAD_URL,
    published_url=DOWNLOAD_URL,
    expected_columns=COLUMNS,
    key_columns=("owner_org", "reference_number"),
    license="Open Government Licence - Canada (ca-ogl-lgo)",
    provenance_url=f"https://open.canada.ca/data/en/dataset/{DATASET_ID}",
)
