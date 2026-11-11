-- The mart holds exactly the approved (accepted or corrected) fields of each document's latest
-- approved extraction in Silver: nothing approved is lost, nothing unapproved is added.
with approved as (
    select document_id, count(*) as fields
    from {{ ref('stg_ai__approved_fields') }}
    group by document_id
)

select coalesce(a.document_id, m.document_id) as document_id
from approved a
full outer join {{ ref('mart_ai_contract_intelligence') }} m
    on a.document_id = m.document_id
where a.document_id is null
    or m.document_id is null
    or a.fields <> m.approved_field_count
