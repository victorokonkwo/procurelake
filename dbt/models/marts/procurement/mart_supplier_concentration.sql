{{ config(
    unique_key=['tenant_id', 'currency_code', 'supplier_id'],
    post_hook="{{ delete_vanished_keys(['tenant_id', 'currency_code', 'supplier_id']) }}",
) }}

-- Each supplier's share of a tenant's awarded value (active awards, all time), per currency.
with by_supplier as (
    select
        tenant_id,
        currency_code,
        supplier_id,
        count(*) as award_count,
        sum(award_amount) as supplier_awarded_value
    from {{ ref('fct_contract_award') }}
    where award_status = 'active'
    group by tenant_id, currency_code, supplier_id
)

select
    tenant_id,
    currency_code,
    supplier_id,
    award_count,
    supplier_awarded_value,
    sum(supplier_awarded_value) over (partition by tenant_id, currency_code) as total_awarded_value,
    cast(supplier_awarded_value as double)
    / cast(sum(supplier_awarded_value) over (partition by tenant_id, currency_code) as double)
        as awarded_value_share,
    rank() over (
        partition by tenant_id, currency_code order by supplier_awarded_value desc
    ) as supplier_rank
from by_supplier
