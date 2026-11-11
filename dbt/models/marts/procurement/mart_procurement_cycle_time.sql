{{ config(
    unique_key=['tenant_id', 'agency_id', 'award_month'],
    post_hook=["{{ delete_vanished_keys(['tenant_id', 'agency_id', 'award_month']) }}", "{{ optimize_iceberg() }}"],
) }}

-- Days from solicitation publication to award (active awards on published solicitations),
-- by tenant, agency and award month.
select
    tenant_id,
    agency_id,
    award_month,
    count(*) as award_count,
    avg(cast(cycle_time_days as double)) as avg_cycle_time_days,
    approx_percentile(cycle_time_days, 0.5) as approx_median_cycle_time_days,
    min(cycle_time_days) as min_cycle_time_days,
    max(cycle_time_days) as max_cycle_time_days
from {{ ref('fct_contract_award') }}
where award_status = 'active' and cycle_time_days is not null
group by tenant_id, agency_id, award_month
