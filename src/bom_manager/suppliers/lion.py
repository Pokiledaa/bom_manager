"""Lion Electronic supplier adapter — httpx-based scraper.

Why httpx (not Playwright)?
----------------------------
Lion Electronic (lionelectronic.ir) is a server-side rendered PHP application.
Product search is available as a simple JSON endpoint with no JavaScript required.
Product detail pages are fully rendered HTML — no SPA/WAF that would require
a headless browser.  Using httpx is faster, cheaper, and more reliable.

Search API
----------
GET https://lionelectronic.ir/products/products-name-list?q={query}
→ JSON: [{"id": "2769", "value": "ESP32-C3-DevKitC-02", "type": "products"}, ...]

Product page
------------
GET https://lionelectronic.ir/products/{id}-{slug}
→ Server-rendered HTML; price breaks and stock in the page body.

Currency
--------
All prices are in Iranian Rial (IRR).  Prices are displayed with Persian
numerals (۰–۹) and comma separators; ``normalize_number()`` converts them.
"""

from __future__ import annotations

import logging
import re
import time
from decimal import Decimal
from typing import Optional
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

from bom_manager.core.currency import normalize_number, parse_price
from bom_manager.suppliers.base import (
    PartDetail,
    PartNotFoundError,
    PartResult,
    PriceBreakInfo,
    SupplierNetworkError,
    SupplierParseError,
)

log = logging.getLogger(__name__)

_BASE_URL = "https://lionelectronic.ir"
_SEARCH_URL = f"{_BASE_URL}/products/products-name-list"
_LION_PARTS_URL = f"{_BASE_URL}/products/lion-part-list"

# Polite delay between requests (seconds)
_MIN_DELAY = 2.0
_MAX_DELAY = 4.0

# ─────────────────────────────────────────────────────────────────────────────
# CSS selectors — centralised for easy update if Lion changes their layout
# ─────────────────────────────────────────────────────────────────────────────

SELECTORS: dict[str, str] = {
    # Product title / part name
    "product_title": "h1.product-title, h1, .product-name h1",
    # Primary price layout: div.price-row > div.price-qty + span.new-price
    "price_row": "div.price-row",
    "price_qty": "div.price-qty",
    "price_new": "span.new-price",
    # Stock status selectors (fallback — Lion uses a table row, see _parse_stock)
    "stock_text": ".stock-status, .availability, [class*='stock'], [class*='موجودی']",
    # Strategy-3 fallback: any element containing a Rial sign or 'ریال'
    "rial_elements": "[class*='price'], [class*='قیمت']",
}

# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://lionelectronic.ir/products",
}

_JSON_HEADERS = {
    **_HEADERS,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


# ─────────────────────────────────────────────────────────────────────────────
# Price-break extraction
# ─────────────────────────────────────────────────────────────────────────────

_DIGIT_RE = re.compile(r"[\d۰-۹٠-٩][,\d۰-۹٠-٩\.]*")


def _extract_price_breaks(tree: HTMLParser) -> list[PriceBreakInfo]:
    """
    Extract price break rows from a Lion product detail page.

    Lion's actual HTML layout (verified):
        <div class="price-row">
            <div class="price-qty"><a class="pdct-quantity">1</a></div>
            <div class="price-text">
                <span class="price-group">
                    <span class="new-price">25,148,340 <small>ریال</small></span>
                </span>
            </div>
        </div>

    Returns an empty list if no price data can be found.
    """
    breaks: list[PriceBreakInfo] = []

    # Strategy 1: div.price-row > div.price-qty + span.new-price
    # This is Lion's actual layout as of 2025.
    for row in tree.css("div.price-row"):
        qty_el = row.css_first("div.price-qty")
        price_el = row.css_first("span.new-price")
        if not qty_el or not price_el:
            continue
        qty = _parse_int(qty_el.text(strip=True))
        price = _parse_decimal(price_el.text(strip=True))
        if qty is not None and price is not None and price > 0:
            breaks.append(PriceBreakInfo(min_quantity=qty, unit_price=price))

    if breaks:
        return sorted(breaks, key=lambda pb: pb.min_quantity)

    # Strategy 2: table rows with two numeric-looking cells (layout fallback)
    for row in tree.css("table tr"):
        cells = row.css("td")
        if len(cells) < 2:
            continue
        qty = _parse_int(cells[0].text(strip=True))
        price = _parse_decimal(cells[-1].text(strip=True))
        if qty is not None and price is not None and price > 0:
            breaks.append(PriceBreakInfo(min_quantity=qty, unit_price=price))

    if breaks:
        return sorted(breaks, key=lambda pb: pb.min_quantity)

    # Strategy 3: any element containing a Rial amount, pair with adjacent qty hints
    rial_pattern = re.compile(r"(\d[\d,۰-۹٠-٩]+)\s*(?:ریال|﷼|IRR)", re.IGNORECASE)
    qty_pattern = re.compile(r"(\d+)\s*(?:\+|عدد|قطعه|pcs?)", re.IGNORECASE)

    for el in tree.css(SELECTORS["rial_elements"]):
        text = el.text(strip=True)
        prices = rial_pattern.findall(text)
        qtys = qty_pattern.findall(text)
        for i, p_str in enumerate(prices):
            price = parse_price(p_str)
            if price is None or price <= 0:
                continue
            qty = int(qtys[i]) if i < len(qtys) else (1 if i == 0 else None)
            if qty is None:
                continue
            breaks.append(PriceBreakInfo(min_quantity=qty, unit_price=price))

    return sorted(set_of_breaks(breaks), key=lambda pb: pb.min_quantity)


def set_of_breaks(breaks: list[PriceBreakInfo]) -> list[PriceBreakInfo]:
    """Deduplicate by min_quantity, keeping lowest price."""
    seen: dict[int, PriceBreakInfo] = {}
    for pb in breaks:
        if pb.min_quantity not in seen or pb.unit_price < seen[pb.min_quantity].unit_price:
            seen[pb.min_quantity] = pb
    return list(seen.values())


def _parse_int(text: str) -> Optional[int]:
    """Extract the first integer from *text* (handles Persian numerals)."""
    normalized = normalize_number(text.replace(",", ""))
    m = re.search(r"\d+", normalized)
    return int(m.group()) if m else None


def _parse_decimal(text: str) -> Optional[Decimal]:
    """Extract the first price-like decimal from *text* (handles Persian numerals)."""
    normalized = normalize_number(text.replace(",", ""))
    m = re.search(r"\d+(?:\.\d+)?", normalized)
    if not m:
        return None
    try:
        return Decimal(m.group())
    except Exception:
        return None


def _parse_stock(tree: HTMLParser) -> int:
    """Try to determine stock availability from product page.

    Lion's table layout (verified):
        Row 0: ['موجودی محصول', 'اتمام موجودی']  ← out of stock
        Row 0: ['موجودی محصول', '<number>']        ← stock count
        Row 0: ['موجودی محصول', 'موجود']           ← in stock (no count)
    """
    # Strategy 1: check the stock table row (Lion's actual layout)
    for row in tree.css("table tr"):
        cells = row.css("td")
        if len(cells) < 2:
            continue
        key_text = cells[0].text(strip=True)
        val_text = cells[1].text(strip=True)
        if "موجودی" in key_text or "stock" in key_text.lower():
            # "اتمام موجودی" = stock exhausted / out of stock
            if "اتمام" in val_text or "ناموجود" in val_text:
                return 0
            n = _parse_int(val_text)
            if n is not None:
                return n
            if "موجود" in val_text and "نا" not in val_text:
                return 1  # in stock, count unknown

    # Strategy 2: CSS selectors for other page layouts
    for sel in (SELECTORS["stock_text"], ".product-stock", ".qty"):
        for el in tree.css(sel):
            text = el.text(strip=True)
            n = _parse_int(text)
            if n is not None and n >= 0:
                return n
            if "اتمام" in text or "ناموجود" in text:
                return 0
            if "موجود" in text and "نا" not in text:
                return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# LionSupplier
# ─────────────────────────────────────────────────────────────────────────────

class LionSupplier:
    """
    Supplier adapter for Lion Electronic (lionelectronic.ir).

    Uses httpx for HTTP requests and selectolax for HTML parsing.
    All prices are returned in IRR (Iranian Rial).
    """

    name: str = "lion"

    def __init__(
        self,
        *,
        storage=None,
        cache_ttl_seconds: float = 86_400,
        max_results: int = 8,
    ) -> None:
        self._storage = storage
        self._cache_ttl = cache_ttl_seconds
        self._max_results = max_results
        self._last_request: float = 0.0
        self._client = httpx.Client(
            headers=_HEADERS,
            follow_redirects=True,
            timeout=40,
            trust_env=False,   # ignore ALL_PROXY/HTTP_PROXY env vars (socks:// unsupported)
        )

    def _polite_delay(self) -> None:
        """Enforce a polite delay between requests."""
        elapsed = time.time() - self._last_request
        if elapsed < _MIN_DELAY:
            time.sleep(_MIN_DELAY - elapsed)
        self._last_request = time.time()

    def _get(self, url: str, **params) -> httpx.Response:
        self._polite_delay()
        try:
            resp = self._client.get(url, params=params)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            raise SupplierNetworkError(
                f"Lion Electronic HTTP {exc.response.status_code}: {url}"
            ) from exc
        except httpx.RequestError as exc:
            raise SupplierNetworkError(
                f"Lion Electronic request failed: {exc}"
            ) from exc

    # ── SupplierProtocol.search ───────────────────────────────────────────────

    def search(self, query: str) -> list[PartResult]:
        """Search Lion Electronic for parts matching *query*.

        Uses the products-name-list JSON endpoint, with a fallback to the
        lion-part-list endpoint for exact part number lookups.

        Retries once on transient network errors (2 s delay).  Raises
        ``SupplierNetworkError`` if both the primary and fallback endpoints
        fail after retrying, so callers can surface the error to the user.
        """
        _OLD_PN = re.compile(r"^LION-\d+$", re.IGNORECASE)   # old format: no slug
        cache_key = f"search:{query.lower().strip()}"
        if self._storage:
            cached = self._storage.get_cached_part(
                self.name, cache_key, max_age_seconds=self._cache_ttl
            )
            if cached:
                results_cached = [PartResult(**r) for r in cached]
                # Discard cache if any entry uses old supplier_pn format (no slug embedded)
                if not any(_OLD_PN.match(r.supplier_pn) for r in results_cached):
                    return results_cached
                log.info("Lion: stale cache detected (old supplier_pn format) — refreshing")

        results: list[PartResult] = []
        primary_exc: Optional[Exception] = None

        # Primary search — product names (with one retry on transient errors)
        for attempt in range(2):
            try:
                resp = self._get(_SEARCH_URL, q=query)
                items = resp.json()
                for item in items[: self._max_results]:
                    pid = str(item.get("id", ""))
                    name = item.get("value", "")
                    if not pid or not name:
                        continue
                    slug = _slugify(name)
                    url = f"{_BASE_URL}/products/{pid}-{slug}"
                    results.append(
                        PartResult(
                            mpn=name,
                            supplier_pn=f"LION-{pid}-{slug}",
                            description=name,
                            manufacturer="",
                            url=url,
                        )
                    )
                primary_exc = None  # success
                break
            except Exception as exc:
                primary_exc = exc
                if attempt == 0:
                    log.warning(
                        "Lion: product search failed for %r (attempt 1/2) — retrying in 2s: %s",
                        query, exc,
                    )
                    time.sleep(2)

        if primary_exc is not None:
            log.warning("Lion product search failed for %r after retry: %s", query, primary_exc)

        # Fallback — lion-part-list (exact part number)
        if not results:
            fallback_exc: Optional[Exception] = None
            for attempt in range(2):
                try:
                    resp = self._get(_LION_PARTS_URL, q=query)
                    items = resp.json()
                    for item in items[: self._max_results]:
                        pid = str(item.get("id", ""))
                        name = item.get("value", "")
                        if not pid or not name:
                            continue
                        slug = _slugify(name)
                        url = f"{_BASE_URL}/products/{pid}-{slug}"
                        results.append(
                            PartResult(
                                mpn=name,
                                supplier_pn=f"LION-{pid}",
                                description=name,
                                manufacturer="",
                                url=url,
                            )
                        )
                    fallback_exc = None  # success
                    break
                except Exception as exc:
                    fallback_exc = exc
                    if attempt == 0:
                        log.warning(
                            "Lion: part-list fallback failed for %r (attempt 1/2) — retrying in 2s: %s",
                            query, exc,
                        )
                        time.sleep(2)

            # Both endpoints failed — re-raise so search_parts_all can warn the user
            if fallback_exc is not None and primary_exc is not None:
                raise SupplierNetworkError(
                    f"Lion Electronic search failed for {query!r}: {primary_exc}"
                ) from primary_exc

        if results and self._storage:
            self._storage.cache_part(
                self.name, cache_key, [r.model_dump() for r in results]
            )

        return results

    # ── SupplierProtocol.get_part ─────────────────────────────────────────────

    def get_part(self, part_number: str) -> PartDetail:
        """Fetch full detail for a Lion part number (format: LION-{id}-{slug}).

        Scrapes the product detail page for price breaks, stock, MPN, and
        manufacturer.  All prices are in IRR.

        Raises
        ------
        PartNotFoundError
            If the product page returns a 404.
        SupplierNetworkError
            On timeout or other connectivity failures (after 1 retry).
        """
        if self._storage:
            cached = self._storage.get_cached_part(
                self.name, part_number, max_age_seconds=self._cache_ttl
            )
            if cached:
                return PartDetail(**cached)

        product_url = self._resolve_url(part_number)

        # Fetch with one retry on transient network errors (timeout, reset, etc.)
        resp = None
        last_exc: Optional[SupplierNetworkError] = None
        for attempt in range(2):
            try:
                resp = self._get(product_url)
                break
            except SupplierNetworkError as exc:
                err_str = str(exc)
                # 404 means the product genuinely doesn't exist — fail immediately
                if "HTTP 404" in err_str or "HTTP 410" in err_str:
                    raise PartNotFoundError(
                        f"Lion Electronic: product not found for {part_number!r}"
                    ) from exc
                last_exc = exc
                if attempt == 0:
                    log.warning(
                        "Lion: request failed for %s (attempt 1/2) — retrying in 4s: %s",
                        product_url, exc,
                    )
                    time.sleep(4)

        if resp is None:
            # Both attempts failed — surface the real error (timeout, etc.)
            raise last_exc  # type: ignore[misc]

        tree = HTMLParser(resp.text)

        # Extract product name / MPN
        mpn = ""
        for sel in (SELECTORS["product_title"], "h1"):
            el = tree.css_first(sel)
            if el:
                mpn = el.text(strip=True)
                break
        if not mpn:
            mpn = part_number  # fallback

        # Extract price breaks, manufacturer, stock, and datasheet
        price_breaks = _extract_price_breaks(tree)

        if not price_breaks:
            log.warning(
                "Lion: no price data found on product page %s "
                "(site layout may have changed — check SELECTORS in lion.py)",
                product_url,
            )

        manufacturer = _parse_manufacturer(tree)
        stock = _parse_stock(tree)
        datasheet_url = _parse_datasheet_url(tree)

        detail = PartDetail(
            mpn=mpn,
            supplier_pn=part_number,
            description=mpn,
            manufacturer=manufacturer,
            url=product_url,
            price_breaks=price_breaks,
            stock=stock,
            datasheet_url=datasheet_url,
            currency="IRR",
        )

        if self._storage:
            self._storage.cache_part(self.name, part_number, detail.model_dump(mode="json"))

        return detail

    def _resolve_url(self, part_number: str) -> str:
        """Convert a supplier_pn to the product page URL.

        Formats handled:
          LION-2769-ESP32-C3-DevKitC-02  →  /products/2769-ESP32-C3-DevKitC-02
          LION-2769                       →  /products/2769  (fallback, may 404)
        """
        if part_number.upper().startswith("LION-"):
            rest = part_number[5:]                    # "2769-ESP32-C3-DevKitC-02"
            m = re.match(r"^(\d+)(-.*)?$", rest)
            if m:
                pid = m.group(1)
                slug_part = m.group(2) or ""          # "-ESP32-C3-DevKitC-02" or ""
                return f"{_BASE_URL}/products/{pid}{slug_part}"
        # Treat as a direct path or fallback
        return f"{_BASE_URL}/products/{part_number}"

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_manufacturer(tree: HTMLParser) -> str:
    """Extract manufacturer name from Lion product detail page.

    Lion's detail-row structure (verified 2025):
        <div class="detail-row">
            <div class="detail-label">Diodes Incorporated</div>  ← the value
            <div class="detail-value">Manufacture</div>          ← the key name
        </div>

    Note: despite the confusing naming, `.detail-label` holds the actual
    manufacturer name and `.detail-value` holds the field key ("Manufacture").
    """
    for row in tree.css("div.detail-row"):
        value_el = row.css_first(".detail-value")
        if value_el and "manufactur" in value_el.text(strip=True).lower():
            label_el = row.css_first(".detail-label")
            if label_el:
                return label_el.text(strip=True)
            # Fallback: strip the key text from the combined row text
            full_text = row.text(strip=True)
            key_text = value_el.text(strip=True)
            return full_text.replace(key_text, "").strip()
    return ""


def _parse_datasheet_url(tree: HTMLParser) -> Optional[str]:
    """Extract datasheet download URL from Lion product detail page.

    Looks for a detail-row whose key text contains 'datasheet' and
    extracts the href from the first anchor inside it.
    """
    for row in tree.css("div.detail-row"):
        value_el = row.css_first(".detail-value")
        if value_el and "datasheet" in value_el.text(strip=True).lower():
            link = row.css_first("a[href]")
            if link:
                href = link.attributes.get("href", "")
                if href:
                    return href if href.startswith("http") else f"{_BASE_URL}{href}"
    return None


def _slugify(name: str) -> str:
    """Convert a product name to a URL-safe slug matching Lion's convention.

    Keeps alphanumeric, hyphens, dots, and underscores — replacing other
    characters with hyphens.  Dots are preserved so that part numbers like
    ``AZ1117CR2-3.3TRG1`` map correctly to their product page URL.
    """
    slug = re.sub(r"[^\w\-\.]", "-", name)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug
