# quiz/templatetags/quiz_extras.py
from __future__ import annotations

from django import template

register = template.Library()


def _to_number(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


@register.filter
def get_item(d, key):
    try:
        return d.get(key)
    except Exception:
        return ""


@register.filter
def default0(v):
    if v in (None, ""):
        return 0
    return v


@register.filter
def sub(a, b):
    """
    {{ a|sub:b }}  => a - b
    يدعم أرقام/سترينج/None
    """
    return _to_number(a, 0) - _to_number(b, 0)


@register.filter
def addn(a, b):
    """
    {{ a|addn:b }} => a + b  (اختياري مفيد)
    """
    return _to_number(a, 0) + _to_number(b, 0)


@register.filter
def clamp_min(v, min_value=0):
    """
    {{ v|clamp_min:0 }} => يضمن ما ينزل عن الحد الأدنى
    """
    v = _to_number(v, 0)
    m = _to_number(min_value, 0)
    return v if v >= m else m


@register.filter
def clamp_max(v, max_value=0):
    """
    {{ v|clamp_max:100 }} => يضمن ما يتجاوز الحد الأعلى
    """
    v = _to_number(v, 0)
    m = _to_number(max_value, 0)
    return v if v <= m else m


@register.filter
def pct(n, total):
    """
    {{ n|pct:total }} => نسبة مئوية 0..100
    """
    n = _to_number(n, 0)
    total = _to_number(total, 0)
    if total <= 0:
        return 0
    return int(round((n / total) * 100))


@register.simple_tag
def qparam(request, **kwargs):
    """
    يبني QueryString مع الحفاظ على بقية الباراميترات:
    <a href="?{% qparam request page=2 %}">...</a>
    """
    try:
        q = request.GET.copy()
        for k, v in kwargs.items():
            if v is None or v == "":
                q.pop(k, None)
            else:
                q[k] = str(v)
        return q.urlencode()
    except Exception:
        return ""
