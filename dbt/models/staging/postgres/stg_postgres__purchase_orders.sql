select
    po_id,
    tenant_id,
    agency_id,
    supplier_id,
    award_id,
    order_date,
    amount as po_amount,
    currency_code,
    status as po_status,
    {{ utc('created_at') }} as created_at,
    {{ utc('updated_at') }} as updated_at,
    {{ lineage_columns() }}
from {{ source('silver', 'silver_purchase_orders') }}
