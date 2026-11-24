{#
    Use custom schemas verbatim, with no target-schema prefix.

    dbt's default would name the marts schema STAGING_MARTS (target schema +
    custom name). The account already has RAW / STAGING / MARTS / DQ created by
    setup/00_setup.sql, so models must land in exactly those.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
