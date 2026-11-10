select
    agency_id,
    tenant_id,
    agency_name,
    created_at,
    updated_at,
    {{ lineage_passthrough() }}
from {{ ref('stg_postgres__agencies') }}
