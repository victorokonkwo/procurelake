{#- Post-hook for Gold marts built with the Iceberg `merge` strategy (ADR 0030).

    A mart is recomputed in full each run, then MERGEd into the existing table so that the table
    is never dropped: dropping it would delete its Lake Formation tenant filters, grants and
    opt-ins. MERGE only touches keys present in the new result, so a group that no longer exists
    (say, every award for a supplier cancelled) would stay in the table. This hook deletes those
    keys: target keys that are absent from the freshly computed temporary table.

    It runs inside the materialization after the MERGE and before dbt drops `<model>__dbt_tmp`
    (dbt-athena 1.11.1, incremental.sql: post-hooks with inside_transaction=True run before
    `to_drop`). On a first build or a full refresh there is no merge and nothing to delete.
    Keys are compared null-safely; the marts' key columns are also tested not_null, because the
    MERGE itself matches keys with `=`.
-#}
{% macro delete_vanished_keys(unique_key) -%}
  {%- if is_incremental() -%}
    {%- set src = make_temp_relation(this, suffix='__dbt_tmp') -%}
    merge into {{ this }} as target
    using (
        select {{ unique_key | join(', ') }} from {{ this }}
        except
        select {{ unique_key | join(', ') }} from {{ src }}
    ) as vanished
    on (
      {%- for key in unique_key %}
        target.{{ key }} is not distinct from vanished.{{ key }}{{ " and" if not loop.last }}
      {%- endfor %}
    )
    when matched then delete
  {%- endif -%}
{%- endmacro %}
