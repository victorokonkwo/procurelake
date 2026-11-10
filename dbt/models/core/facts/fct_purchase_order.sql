-- One row per purchase order (amount in its own currency).
select
    po_id,
    tenant_id,
    agency_id,
    supplier_id,
    award_id,
    order_date,
    cast(date_trunc('month', order_date) as date) as order_month,
    po_amount,
    currency_code,
    po_status,
    updated_at,
    {{ lineage_passthrough() }}
from {{ ref('stg_postgres__purchase_orders') }}
