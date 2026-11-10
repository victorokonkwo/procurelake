select
    agency_id,
    tenant_id,
    agency_name,
    {{ utc('created_at') }} as created_at,
    {{ utc('updated_at') }} as updated_at,
    {{ lineage_columns() }}
from {{ source('silver', 'silver_agencies') }}
-- CDC tombstones (ADR 0028) are deleted at the source: Gold never shows them.
where not coalesce(_is_deleted, false)
