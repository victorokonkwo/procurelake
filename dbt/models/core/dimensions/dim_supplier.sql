-- One row per supplier registration (supplier_id) within its tenant. Cross-tenant or
-- name-based supplier matching is not defined yet (docs/open-questions.md), so no merging.
select
    supplier_id,
    tenant_id,
    supplier_name,
    supplier_category,
    created_at,
    updated_at,
    {{ lineage_passthrough() }}
from {{ ref('stg_postgres__suppliers') }}
