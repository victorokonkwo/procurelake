select
    solicitation_id,
    tenant_id,
    agency_id,
    title as solicitation_title,
    status as solicitation_status,
    {{ utc('published_at') }} as published_at,
    {{ utc('closing_at') }} as closing_at,
    {{ utc('created_at') }} as created_at,
    {{ utc('updated_at') }} as updated_at,
    {{ lineage_columns() }}
from {{ source('silver', 'silver_solicitations') }}
-- CDC tombstones (ADR 0028) are deleted at the source: Gold never shows them.
where not coalesce(_is_deleted, false)
