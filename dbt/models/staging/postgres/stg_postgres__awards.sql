select
    award_id,
    tenant_id,
    solicitation_id,
    supplier_id,
    award_amount,
    currency_code,
    {{ utc('awarded_at') }} as awarded_at,
    status as award_status,
    {{ utc('created_at') }} as created_at,
    {{ utc('updated_at') }} as updated_at,
    {{ lineage_columns() }}
from {{ source('silver', 'silver_awards') }}
