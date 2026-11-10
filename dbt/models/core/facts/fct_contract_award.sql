-- One row per award. award_amount is AWARDED value, not spend; currency is kept as-is.
select
    a.award_id,
    a.tenant_id,
    a.solicitation_id,
    s.agency_id,
    a.supplier_id,
    a.award_amount,
    a.currency_code,
    a.award_status,
    a.awarded_at,
    cast(a.awarded_at as date) as award_date,
    cast(date_trunc('month', a.awarded_at) as date) as award_month,
    s.published_at as solicitation_published_at,
    -- Procurement cycle time (docs/architecture/data_model.md section 5): published -> awarded.
    -- Null when the solicitation has no publication time.
    case
        when s.published_at is not null
            then date_diff('day', s.published_at, a.awarded_at)
    end as cycle_time_days,
    a.updated_at,
    {{ lineage_passthrough('a') }}
from {{ ref('stg_postgres__awards') }} as a
inner join {{ ref('stg_postgres__solicitations') }} as s
    on a.tenant_id = s.tenant_id and a.solicitation_id = s.solicitation_id
