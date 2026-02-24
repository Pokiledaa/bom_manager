"""Settings service for BOM Manager.

Stores application settings as key-value pairs in the SQLite ``settings`` table.

Keys
----
usd_to_irr_rate   : Decimal-string — IRR per 1 USD (e.g. "600000")
active_suppliers  : comma-separated list — "lcsc", "lion", or "all"
rate_last_fetched : ISO-8601 UTC timestamp of last auto-fetch (empty = never)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

log = logging.getLogger(__name__)

_RATE_API_URL = "https://open.er-api.com/v6/latest/USD"

DEFAULTS: dict[str, str] = {
    "usd_to_irr_rate": "600000",
    "active_suppliers": "all",
    "rate_last_fetched": "",
}

VALID_SUPPLIER_VALUES = {"lcsc", "lion", "all"}


class SettingsService:
    """Read and write application settings backed by SQLite."""

    def __init__(self, storage) -> None:
        self._storage = storage

    # ── Generic get/set ───────────────────────────────────────────────────────

    def get(self, key: str, default: Optional[str] = None) -> str:
        """Return the stored value for *key*, falling back to DEFAULTS then *default*."""
        value = self._storage.get_setting(key)
        if value is not None:
            return value
        return DEFAULTS.get(key, default or "")

    def set(self, key: str, value: str) -> None:
        """Persist *value* for *key*."""
        self._storage.set_setting(key, value)

    # ── Exchange rate ─────────────────────────────────────────────────────────

    def get_rate(self) -> Decimal:
        """Return the current USD→IRR rate as a Decimal."""
        raw = self.get("usd_to_irr_rate")
        try:
            return Decimal(raw)
        except InvalidOperation:
            return Decimal(DEFAULTS["usd_to_irr_rate"])

    def set_rate(self, rate: Decimal) -> None:
        """Persist a manually entered exchange rate."""
        if rate <= 0:
            raise ValueError("Exchange rate must be positive")
        self.set("usd_to_irr_rate", str(rate))

    def fetch_live_rate(self) -> Optional[Decimal]:
        """Fetch USD→IRR from the free open.er-api.com API.

        Returns the fetched rate on success, or None on failure.

        .. warning::
            This returns the **official / interbank** USD/IRR rate.
            In Iran, the market (bazaar) rate used by commercial suppliers
            such as Lion Electronic may differ significantly.
            Always verify and override with ``set_rate()`` if needed.
        """
        try:
            import httpx
            resp = httpx.get(_RATE_API_URL, timeout=10, follow_redirects=True,
                             headers={"User-Agent": "bom-manager/0.1"})
            resp.raise_for_status()
            data = resp.json()
            rates = data.get("rates", {})
            irr_rate = rates.get("IRR")
            if irr_rate is None:
                log.warning("IRR not found in rate API response")
                return None
            rate = Decimal(str(irr_rate))
            self.set_rate(rate)
            self.set(
                "rate_last_fetched",
                datetime.now(timezone.utc).isoformat(),
            )
            return rate
        except Exception as exc:
            log.warning("Failed to fetch live exchange rate: %s", exc)
            return None

    def rate_last_fetched(self) -> Optional[datetime]:
        """Return when the rate was last auto-fetched, or None if never."""
        raw = self.get("rate_last_fetched")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    # ── Active suppliers ──────────────────────────────────────────────────────

    def get_active_suppliers(self) -> list[str]:
        """Return the list of active supplier names.

        Possible values: ["lcsc"], ["lion"], ["lcsc", "lion"] (for "all").
        """
        raw = self.get("active_suppliers", "all").strip().lower()
        if raw == "all":
            return ["lcsc", "lion"]
        names = [n.strip() for n in raw.split(",") if n.strip()]
        return [n for n in names if n in ("lcsc", "lion")]

    def set_active_suppliers(self, value: str) -> None:
        """Set active suppliers. Accepts 'lcsc', 'lion', or 'all'."""
        value = value.strip().lower()
        if value not in VALID_SUPPLIER_VALUES:
            raise ValueError(
                f"Invalid supplier {value!r}. Choose: lcsc, lion, all"
            )
        self.set("active_suppliers", value)

    # ── Summary ───────────────────────────────────────────────────────────────

    def all_settings(self) -> dict[str, str]:
        """Return all settings with their current values (resolved from defaults)."""
        return {key: self.get(key) for key in DEFAULTS}
