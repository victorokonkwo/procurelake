-- One row per calendar day, spanning every date used by the facts (first to last), so each
-- fact date has a dimension row. Derived from the data; no hard-coded range.
with bounds as (
    select min(d) as first_day, max(d) as last_day
    from (
        select cast(awarded_at as date) as d from {{ ref('stg_postgres__awards') }}
        union all
        select cast(published_at as date) from {{ ref('stg_postgres__solicitations') }}
        union all
        select order_date from {{ ref('stg_postgres__purchase_orders') }}
        union all
        select invoice_date from {{ ref('stg_postgres__invoices') }}
        union all
        select cast(paid_at as date) from {{ ref('stg_postgres__invoices') }}
    )
),

days as (
    select date_day
    from bounds
    cross join unnest(sequence(first_day, last_day, interval '1' day)) as t (date_day)
)

select
    date_day,
    year(date_day) as year,
    quarter(date_day) as quarter,
    month(date_day) as month,
    cast(date_trunc('month', date_day) as date) as month_start,
    day_of_week(date_day) as iso_day_of_week,
    week_of_year(date_day) as iso_week
from days
