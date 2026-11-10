-- Actual spend: cash paid on invoices (paid_amount > 0), by tenant, agency, supplier, payment
-- month and currency. NOT awarded value. The month is that of the latest payment (paid_at):
-- the source keeps cumulative paid_amount, not a payment history.
select
    tenant_id,
    agency_id,
    supplier_id,
    paid_month,
    currency_code,
    count(*) as paid_invoice_count,
    sum(paid_amount) as actual_spend
from {{ ref('fct_invoice') }}
where paid_amount > 0
group by tenant_id, agency_id, supplier_id, paid_month, currency_code
