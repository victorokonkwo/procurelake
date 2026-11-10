select
    supplier_id,
    tenant_id,
    supplier_name,
    category as supplier_category,
    {{ utc('created_at') }} as created_at,
    {{ utc('updated_at') }} as updated_at,
    {{ lineage_columns() }}
from {{ source('silver', 'silver_suppliers') }}
