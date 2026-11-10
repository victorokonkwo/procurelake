{{ config(
    unique_key=['tenant_id', 'agency_id', 'supplier_id', 'award_month', 'currency_code'],
    post_hook="{{ delete_vanished_keys(['tenant_id', 'agency_id', 'supplier_id', 'award_month', 'currency_code']) }}",
) }}

-- Awarded value (NOT spend) of active awards by tenant, agency, supplier, month and currency.
-- Cancelled awards are excluded. Currencies are never summed together.
select
    tenant_id,
    agency_id,
    supplier_id,
    award_month,
    currency_code,
    count(*) as award_count,
    sum(award_amount) as awarded_value
from {{ ref('fct_contract_award') }}
where award_status = 'active'
group by tenant_id, agency_id, supplier_id, award_month, currency_code
