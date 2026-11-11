{#- Post-hook for Gold marts built with the Iceberg `merge` strategy (ADR 0033).

    Athena's MERGE is merge-on-read: each dbt run adds a data file with the recomputed mart plus a
    position-delete file for the rows it replaces. Measured on 2026-11-11, mart_actual_spend held
    47 data files and 47 delete files (82,054 records read to return 1,745 rows) after 47 runs.
    Compacting right after the merge keeps every read at one file. Measured the same day: each
    run commits the merge (1 data + 1 delete file) and then a `replace` that rewrites the 2 data
    files into 1 and drops the delete file, in about 5 s of Athena time per mart.

    It runs after `delete_vanished_keys`, so the compaction includes those deletes. On a first
    build or a full refresh the table is new and there is nothing to compact.
-#}
{% macro optimize_iceberg() -%}
  {%- if is_incremental() -%}
    optimize {{ this }} rewrite data using bin_pack
  {%- endif -%}
{%- endmacro %}
