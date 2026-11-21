"""Small, presentation-only helpers for Pool templates."""

from decimal import Decimal, InvalidOperation

from django import template


register = template.Library()


@register.filter
def lilangeni(value):
    """Format an optional decimal as a compact emalangeni amount."""

    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return ""
    formatted = f"{amount:,.2f}"
    if formatted.endswith(".00"):
        formatted = formatted[:-3]
    return f"E{formatted}"
