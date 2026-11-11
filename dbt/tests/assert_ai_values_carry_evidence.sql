-- Every value in the AI mart keeps the page it was read from, and a stated amount always has
-- its currency (and a currency never appears without an amount). Rows returned are failures.
select document_id
from {{ ref('mart_ai_contract_intelligence') }}
where (contract_id is not null and contract_id_page is null)
    or (supplier_name is not null and supplier_name_page is null)
    or (issuing_agency is not null and issuing_agency_page is null)
    or (effective_date is not null and effective_date_page is null)
    or (contract_category is not null and contract_category_page is null)
    or (stated_amount is not null and (stated_amount_page is null or currency_code is null or amount_type is null))
    or (stated_amount is null and (currency_code is not null or amount_type is not null))
