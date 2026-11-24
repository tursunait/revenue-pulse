{#
    A local surrogate-key helper so the project needs no package dependency.
    dbt_utils.generate_surrogate_key would do the same, but adding a package
    for one hash is not worth the extra install step.
#}
{% macro surrogate_key(parts) -%}
    md5(
        {%- for part in parts %}
        coalesce(to_varchar({{ part }}), '~')
        {%- if not loop.last %} || '|' || {% endif %}
        {%- endfor %}
    )
{%- endmacro %}
