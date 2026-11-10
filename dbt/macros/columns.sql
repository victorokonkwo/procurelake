{#- Silver timestamps are instants (Athena: timestamp(6) with time zone). Gold stores them as
    UTC wall-clock timestamp(6), the type Athena Iceberg CTAS writes. -#}
{% macro utc(column) -%}
    cast({{ column }} at time zone 'UTC' as timestamp(6))
{%- endmacro %}

{#- Lineage columns carried from Silver so every Gold row can be traced to its Bronze line. -#}
{% macro lineage_columns(alias=none) -%}
    {%- set p = (alias ~ '.') if alias else '' -%}
    {{ p }}_batch_id as source_batch_id,
    {{ p }}_source_file as source_file,
    {{ p }}_source_line_hash as source_line_hash,
    {{ p }}_source_sequence as source_sequence
{%- endmacro %}

{#- The lineage columns as already renamed by staging (for core models). -#}
{% macro lineage_passthrough(alias=none) -%}
    {%- set p = (alias ~ '.') if alias else '' -%}
    {{ p }}source_batch_id,
    {{ p }}source_file,
    {{ p }}source_line_hash,
    {{ p }}source_sequence
{%- endmacro %}
