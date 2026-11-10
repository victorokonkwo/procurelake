-- One row per invoice. paid_amount is cash paid to date (actual spend); paid_at is the latest
-- payment time. Agency and supplier come from the invoiced purchase order.
select
    i.invoice_id,
    i.tenant_id,
    i.po_id,
    po.agency_id,
    po.supplier_id,
    i.invoice_date,
    i.invoice_amount,
    i.paid_amount,
    i.currency_code,
    i.payment_status,
    i.paid_at,
    cast(date_trunc('month', i.paid_at) as date) as paid_month,
    i.updated_at,
    {{ lineage_passthrough('i') }}
from {{ ref('stg_postgres__invoices') }} as i
inner join {{ ref('stg_postgres__purchase_orders') }} as po
    on i.tenant_id = po.tenant_id and i.po_id = po.po_id
