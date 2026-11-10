-- Business rule: within a tenant and currency, supplier shares of awarded value add up to 1.
select tenant_id, currency_code, sum(awarded_value_share) as total_share
from {{ ref('mart_supplier_concentration') }}
group by tenant_id, currency_code
having abs(sum(awarded_value_share) - 1.0) > 1e-9
