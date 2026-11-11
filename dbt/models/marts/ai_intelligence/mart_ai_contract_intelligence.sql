-- Approved contract attributes extracted from public contract documents (ADR 0032).
-- One row per document (its latest approved extraction). A field is null when the document
-- does not state it, the model did not find it, or the reviewer rejected it; it is never
-- filled in. stated_amount is the amount the document itself states (see amount_type): it is
-- neither awarded value across the award's life nor spend.
with fields as (
    select * from {{ ref('stg_ai__approved_fields') }}
),

documents as (
    select
        document_id,
        extraction_id,
        max(document_type) as document_type,
        max(source_url) as source_url,
        max(landing_page) as landing_page,
        max(license_basis) as license_basis,
        max(pdf_sha256) as pdf_sha256,
        max(usaspending_award_id) as usaspending_award_id,
        max(reviewer) as reviewer,
        max(reviewed_at) as reviewed_at,
        max(model_id) as model_id,
        max(prompt_version) as prompt_version,
        max(schema_version) as schema_version,
        max(extracted_at) as extracted_at
    from fields
    group by document_id, extraction_id
),

pivoted as (
    select
        document_id,
        max(case when field_name = 'contract_id' then final_value end) as contract_id,
        max(case when field_name = 'contract_id' then final_page end) as contract_id_page,
        max(case when field_name = 'supplier' then final_value end) as supplier_name,
        max(case when field_name = 'supplier' then final_page end) as supplier_name_page,
        max(case when field_name = 'issuing_agency' then final_value end) as issuing_agency,
        max(case when field_name = 'issuing_agency' then final_page end) as issuing_agency_page,
        max(case when field_name = 'effective_date' then final_value end) as effective_date,
        max(case when field_name = 'effective_date' then final_page end) as effective_date_page,
        max(case when field_name = 'performance_start_date' then final_value end) as performance_start_date,
        max(case when field_name = 'performance_end_date' then final_value end) as performance_end_date,
        max(case when field_name = 'contract_category' then final_value end) as contract_category,
        max(case when field_name = 'contract_category' then final_page end) as contract_category_page,
        max(case when field_name = 'contract_amount' then final_value end) as stated_amount,
        max(case when field_name = 'contract_amount' then currency end) as currency_code,
        max(case when field_name = 'contract_amount' then amount_type end) as amount_type,
        max(case when field_name = 'contract_amount' then final_page end) as stated_amount_page,
        count(*) as approved_field_count
    from fields
    group by document_id
)

select
    d.document_id,
    d.extraction_id,
    d.document_type,
    p.contract_id,
    p.contract_id_page,
    p.supplier_name,
    p.supplier_name_page,
    p.issuing_agency,
    p.issuing_agency_page,
    cast(p.effective_date as date) as effective_date,
    p.effective_date_page,
    cast(p.performance_start_date as date) as performance_start_date,
    cast(p.performance_end_date as date) as performance_end_date,
    p.contract_category,
    p.contract_category_page,
    cast(p.stated_amount as decimal(38, 2)) as stated_amount,
    p.currency_code,
    p.amount_type,
    p.stated_amount_page,
    p.approved_field_count,
    d.usaspending_award_id,
    d.source_url,
    d.landing_page,
    d.license_basis,
    d.pdf_sha256,
    d.reviewer,
    d.reviewed_at,
    d.model_id,
    d.prompt_version,
    d.schema_version,
    d.extracted_at
from documents d
join pivoted p on d.document_id = p.document_id
