"""Currency utilities for BOM Manager.

Supports USD (US Dollar) and IRR (Iranian Rial).

Key helpers
-----------
- normalize_number()  — converts Persian/Arabic-Indic numerals to ASCII
- irr_to_usd()        — convert IRR amount to USD
- usd_to_irr()        — convert USD amount to IRR
- fmt_irr() / fmt_usd() — format monetary amounts
- fmt_price()         — format with optional converted secondary amount
- parse_manual_price() — parse "600000 IRR" or "0.50 USD" from user input
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

# Translates both Persian-Indic and Extended Arabic-Indic digits to ASCII.
_PERSIAN_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

# Known currency suffixes accepted in manual price input (case-insensitive)
_CURRENCY_ALIASES: dict[str, str] = {
    "usd": "USD",
    "dollar": "USD",
    "$": "USD",
    "irr": "IRR",
    "rial": "IRR",
    "ریال": "IRR",
    "toman": "IRR",   # 1 Toman = 10 Rial; we treat Toman as IRR for simplicity
    "تومان": "IRR",
}

SUPPORTED_CURRENCIES = ("USD", "IRR")


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation
# ─────────────────────────────────────────────────────────────────────────────

def normalize_number(text: str) -> str:
    """Convert Persian/Arabic-Indic numerals to ASCII digits.

    Strips everything except ASCII digits and '.', preserving the decimal point.

    Example
    -------
    >>> normalize_number("۲۵,۱۴۸,۳۴۰")
    '25148340'
    """
    converted = text.translate(_PERSIAN_DIGITS)
    return "".join(c for c in converted if c.isdigit() or c == ".")


def parse_price(text: str) -> Optional[Decimal]:
    """Parse a price string (possibly with Persian digits and separators) into Decimal.

    Returns None if the string cannot be parsed.
    """
    cleaned = normalize_number(text.replace(",", ""))
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Conversion
# ─────────────────────────────────────────────────────────────────────────────

def usd_to_irr(amount: Decimal, rate: Decimal) -> Decimal:
    """Convert USD to IRR.

    Parameters
    ----------
    amount : Decimal
        Amount in USD.
    rate : Decimal
        Exchange rate expressed as IRR per 1 USD (e.g. 600_000).
    """
    return (amount * rate).quantize(Decimal("1"))


def irr_to_usd(amount: Decimal, rate: Decimal) -> Decimal:
    """Convert IRR to USD.

    Parameters
    ----------
    amount : Decimal
        Amount in IRR.
    rate : Decimal
        Exchange rate expressed as IRR per 1 USD (e.g. 600_000).
    """
    if rate == 0:
        return Decimal("0")
    return (amount / rate).quantize(Decimal("0.0001"))


def convert(amount: Decimal, from_currency: str, to_currency: str, rate: Decimal) -> Decimal:
    """Convert ``amount`` between USD and IRR using ``rate`` (IRR per USD).

    Returns the amount unchanged if ``from_currency == to_currency``.
    """
    if from_currency == to_currency:
        return amount
    if from_currency == "USD" and to_currency == "IRR":
        return usd_to_irr(amount, rate)
    if from_currency == "IRR" and to_currency == "USD":
        return irr_to_usd(amount, rate)
    raise ValueError(f"Unsupported currency pair: {from_currency} → {to_currency}")


# ─────────────────────────────────────────────────────────────────────────────
# Formatting
# ─────────────────────────────────────────────────────────────────────────────

def fmt_irr(amount: Decimal) -> str:
    """Format an IRR amount with thousands separators."""
    return f"IRR {amount:,.0f}"


def fmt_usd(amount: Decimal) -> str:
    """Format a USD amount with 4 decimal places."""
    return f"${amount:.4f}"


def fmt_amount(amount: Decimal, currency: str) -> str:
    """Format an amount in its native currency."""
    if currency == "IRR":
        return fmt_irr(amount)
    return fmt_usd(amount)


def fmt_price(
    amount: Decimal,
    currency: str,
    *,
    rate: Optional[Decimal] = None,
    dash: str = "—",
) -> str:
    """Format primary price; if ``rate`` is given, append converted secondary.

    The secondary is shown dim in Rich markup.

    Example
    -------
    >>> fmt_price(Decimal("25148340"), "IRR", rate=Decimal("600000"))
    '﷼25,148,340 [dim]($41.9139)[/]'
    """
    if amount is None:
        return dash
    primary = fmt_amount(amount, currency)
    if rate is None or rate == 0:
        return primary
    other_currency = "USD" if currency == "IRR" else "IRR"
    other = convert(amount, currency, other_currency, rate)
    secondary = fmt_amount(other, other_currency)
    return f"{primary} [dim]({secondary})[/]"


# ─────────────────────────────────────────────────────────────────────────────
# Manual price input parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_manual_price(text: str) -> tuple[Decimal, str]:
    """Parse a manual price string entered by the user.

    Accepted formats (case-insensitive)::

        "600000 IRR"      → (Decimal("600000"), "IRR")
        "600,000 rial"    → (Decimal("600000"), "IRR")
        "0.50 USD"        → (Decimal("0.50"),   "USD")
        "$12.50"          → (Decimal("12.50"),  "USD")
        "۲۵۰۰۰ ریال"     → (Decimal("25000"),  "IRR")

    Raises
    ------
    ValueError
        If the string cannot be parsed or the currency is not recognised.
    """
    text = text.strip()

    # Check for leading "$" symbol
    if text.startswith("$"):
        amount_str = text[1:].replace(",", "").strip()
        amount = parse_price(amount_str)
        if amount is None:
            raise ValueError(f"Cannot parse amount from {text!r}")
        return amount, "USD"

    # Split on whitespace — last token may be the currency
    parts = text.split()
    if not parts:
        raise ValueError("Empty price string")

    # Try last token as currency
    currency = None
    amount_parts = parts

    if len(parts) >= 2:
        last = parts[-1].lower()
        resolved = _CURRENCY_ALIASES.get(last)
        if resolved:
            currency = resolved
            amount_parts = parts[:-1]

    amount_str = "".join(amount_parts).replace(",", "")
    amount = parse_price(amount_str)
    if amount is None:
        raise ValueError(f"Cannot parse price amount from {text!r}")
    if amount < 0:
        raise ValueError("Price cannot be negative")

    if currency is None:
        raise ValueError(
            f"Currency not recognised in {text!r}. "
            f"Use 'USD', '$', 'IRR', or 'rial' suffix."
        )

    return amount, currency
