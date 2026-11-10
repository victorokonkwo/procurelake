-- Financial reconciliation: actual spend per tenant and currency equals the cash paid on
-- Silver invoices, to the cent. Returns the rows that differ.
with mart as (
    select tenant_id, currency_code, sum(actual_spend) as amount
    from {{ ref('mart_actual_spend') }}
    group by tenant_id, currency_code
),

silver as (
    select tenant_id, currency_code, sum(paid_amount) as amount
    from {{ source('silver', 'silver_invoices') }}
    where paid_amount > 0 and not coalesce(_is_deleted, false)
    group by tenant_id, currency_code
)

select coalesce(m.tenant_id, s.tenant_id) as tenant_id, coalesce(m.currency_code, s.currency_code) as currency_code,
       m.amount as mart_amount, s.amount as silver_amount
from mart as m
full outer join silver as s on m.tenant_id = s.tenant_id and m.currency_code = s.currency_code
where m.amount is null or s.amount is null or m.amount <> s.amount
