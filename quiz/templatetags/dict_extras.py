# quiz/templatetags/dict_extras.py
from __future__ import annotations

from django import template

register = template.Library()


@register.filter
def get_item(d, key):
    """Safe dict/object key getter for templates."""
    if d is None:
        return None
    try:
        # dict-like
        if hasattr(d, "get"):
            return d.get(key)
        # fallback indexing
        return d[key]
    except Exception:
        return None


@register.filter
def truthy(v):
    """Template-friendly truthiness."""
    return bool(v)
