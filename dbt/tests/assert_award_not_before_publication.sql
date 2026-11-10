-- Business rule: an award cannot be made before its solicitation was published.
select award_id, solicitation_published_at, awarded_at, cycle_time_days
from {{ ref('fct_contract_award') }}
where cycle_time_days < 0
