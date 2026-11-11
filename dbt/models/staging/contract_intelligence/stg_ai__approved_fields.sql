-- Approved field values from the latest approved extraction of each document (ADR 0032).
-- A field enters Gold only if a reviewer accepted or corrected it, and it carries the page
-- its value was read from.
with decided as (
    select * from {{ source('silver_ai', 'silver_contract_intelligence') }}
    where review_status = 'approved'
),

latest as (
    select document_id, max(extraction_id) as extraction_id
    from decided
    group by document_id
)

select d.*
from decided d
join latest l
    on d.document_id = l.document_id
    and d.extraction_id = l.extraction_id
where d.review_action in ('accept', 'correct')
    and d.final_value is not null
