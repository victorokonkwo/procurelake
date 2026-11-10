-- Business rule: actual spend on an invoice never exceeds the amount invoiced.
select invoice_id, invoice_amount, paid_amount
from {{ ref('fct_invoice') }}
where paid_amount < 0 or paid_amount > invoice_amount
