select
    invoice_id,
    tenant_id,
    po_id,
    invoice_date,
    invoice_amount,
    paid_amount,
    currency_code,
    payment_status,
    {{ utc('paid_at') }} as paid_at,
    {{ utc('created_at') }} as created_at,
    {{ utc('updated_at') }} as updated_at,
    {{ lineage_columns() }}
from {{ source('silver', 'silver_invoices') }}
-- CDC tombstones (ADR 0028) are deleted at the source: Gold never shows them.
where not coalesce(_is_deleted, false)
