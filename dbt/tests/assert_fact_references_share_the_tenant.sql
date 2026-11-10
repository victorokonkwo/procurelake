-- Tenant isolation: every agency and supplier a fact points to belongs to the fact's tenant.
select 'award_agency' as check_name, f.award_id as fact_id
from {{ ref('fct_contract_award') }} as f
inner join {{ ref('dim_agency') }} as d on f.agency_id = d.agency_id
where f.tenant_id <> d.tenant_id
union all
select 'award_supplier', f.award_id
from {{ ref('fct_contract_award') }} as f
inner join {{ ref('dim_supplier') }} as d on f.supplier_id = d.supplier_id
where f.tenant_id <> d.tenant_id
union all
select 'po_supplier', f.po_id
from {{ ref('fct_purchase_order') }} as f
inner join {{ ref('dim_supplier') }} as d on f.supplier_id = d.supplier_id
where f.tenant_id <> d.tenant_id
union all
select 'invoice_supplier', f.invoice_id
from {{ ref('fct_invoice') }} as f
inner join {{ ref('dim_supplier') }} as d on f.supplier_id = d.supplier_id
where f.tenant_id <> d.tenant_id
