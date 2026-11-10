{#- Generic test: no two rows share the given column combination (no dbt packages are used;
    package versions are not pinned yet, open question 2). -#}
{% test unique_combination(model, columns) %}
select {{ columns | join(', ') }}, count(*) as n
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}
