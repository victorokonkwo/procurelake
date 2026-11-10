select
    tenant_id,
    tenant_name,
    status as tenant_status,
    is_synthetic,
    {{ utc('created_at') }} as created_at,
    {{ utc('updated_at') }} as updated_at,
    {{ lineage_columns() }}
from {{ source('silver', 'silver_tenants') }}
