-- Financial reconciliation: awarded value per tenant and currency in the mart equals the sum
-- of active award amounts in Silver, to the cent. Returns the rows that differ.
with mart as (
    select tenant_id, currency_code, sum(awarded_value) as amount, sum(award_count) as n
    from {{ ref('mart_awarded_value') }}
    group by tenant_id, currency_code
),

silver as (
    select tenant_id, currency_code, sum(award_amount) as amount, count(*) as n
    from {{ source('silver', 'silver_awards') }}
    where status = 'active' and not coalesce(_is_deleted, false)
    group by tenant_id, currency_code
)

select coalesce(m.tenant_id, s.tenant_id) as tenant_id, coalesce(m.currency_code, s.currency_code) as currency_code,
       m.amount as mart_amount, s.amount as silver_amount, m.n as mart_count, s.n as silver_count
from mart as m
full outer join silver as s on m.tenant_id = s.tenant_id and m.currency_code = s.currency_code
where m.amount is null or s.amount is null or m.amount <> s.amount or m.n <> s.n
