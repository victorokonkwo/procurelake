-- Facts use inner joins to attach agency/supplier; no Silver row may be lost by them.
with counts as (
    select 'fct_contract_award' as model,
           (select count(*) from {{ ref('fct_contract_award') }}) as gold_rows,
           (select count(*) from {{ source('silver', 'silver_awards') }}) as silver_rows
    union all
    select 'fct_purchase_order',
           (select count(*) from {{ ref('fct_purchase_order') }}),
           (select count(*) from {{ source('silver', 'silver_purchase_orders') }})
    union all
    select 'fct_invoice',
           (select count(*) from {{ ref('fct_invoice') }}),
           (select count(*) from {{ source('silver', 'silver_invoices') }})
)

select * from counts where gold_rows <> silver_rows
