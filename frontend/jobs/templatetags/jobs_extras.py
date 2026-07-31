from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    """Dict lookup by a variable key -- Django templates only support
    literal-key dot access (d.key), not d[some_variable]."""
    if not mapping:
        return None
    return mapping.get(key)
