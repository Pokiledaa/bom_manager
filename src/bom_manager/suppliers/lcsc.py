"""LCSC supplier adapter — Playwright-based web scraper.

Why Playwright?
---------------
LCSC's search page (www.lcsc.com) is a Vue/Nuxt SPA: the initial HTML
returned by the server is a ~26 KB shell with no product data.  Products
are injected into the DOM by client-side JavaScript after the page loads.

Their JSON API backend (wmsc.lcsc.com) is protected by CloudFront WAF with
strict bot-detection rules that block all non-browser HTTP clients.

Playwright launches a real headless Chromium browser, which executes JS and
passes every WAF challenge, giving us the fully-rendered 800 KB DOM with all
25 product rows ready to parse.
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from bom_manager.storage.base import StorageProtocol
from bom_manager.suppliers.base import (
    PartDetail,
    PartNotFoundError,
    PartResult,
    PriceBreakInfo,
    SupplierNetworkError,
    SupplierParseError,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CSS selector config
# Keep ALL selectors here so a layout change only requires editing this dict.
# ---------------------------------------------------------------------------

SELECTORS: dict[str, str] = {
    # ── Search results page ──────────────────────────────────────────────────
    # Each product is a <tr id="productId{N}"> element
    "search_row": 'tr[id^="productId"]',
    # Inside a row, product-detail links appear in order: first=MPN, second=LCSC code
    "search_name_anchor": 'a[href*="/product-detail/"]',
    # Manufacturer brand link
    "search_mfr_anchor": 'a[href*="/brand-detail/"]',
    # Datasheet PDF link (appears as a small PDF icon per row)
    "search_datasheet_anchor": 'a[href*="/datasheet/"]',
    # Shown when a query returns zero matches
    "search_no_results": (
        ".searchNoResult, [class*='noResult'], "
        "[class*='empty-search'], [class*='emptySearch'], "
        "[class*='noData']"
    ),
    # CloudFront / bot challenge page indicators
    "search_captcha": (
        "#captcha, [class*='captcha'], .robot-verify, "
        "#cf-challenge-running, [class*='challenge']"
    ),

    # ── Product detail page ──────────────────────────────────────────────────
    # A stable element present once the detail page has fully rendered.
    # table.tableInfoWrap is always present and contains MPN/manufacturer/LCSC PN.
    "detail_ready": "table.tableInfoWrap, .detailRightPanelWrap",
    # The info table (label → value rows for MPN, Manufacturer, LCSC Part #, …)
    "detail_info_table": "table.tableInfoWrap",
    # The price-break table
    "detail_price_table": "table.priceTable",
    # The right panel wrapping the stock count
    "detail_right_panel": ".detailRightPanelWrap",
    # Datasheet PDF anchor
    "detail_datasheet": (
        "a[title*='Datasheet'], a[title*='datasheet'], "
        "a[href*='/datasheet/'], "
        "a[href$='.pdf'][target='_blank']"
    ),
    # Bot challenge on detail page
    "detail_captcha": (
        "#captcha, [class*='captcha'], "
        "#cf-challenge-running, [class*='challenge']"
    ),
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUPPLIER_NAME = "LCSC"
_BASE_URL = "https://www.lcsc.com"
_SEARCH_URL = _BASE_URL + "/search?q={query}"
_DETAIL_URL = _BASE_URL + "/product-detail/{pn}.html"

_PAGE_TIMEOUT = 30_000   # ms — page.goto() hard timeout
_WAIT_TIMEOUT = 20_000   # ms — wait_for_selector() timeout

_DELAY_MIN = 1.0          # seconds — minimum polite delay between requests
_DELAY_MAX = 3.0          # seconds — maximum polite delay between requests

_DEFAULT_MAX_RESULTS = 10

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# BrowserManager
# ---------------------------------------------------------------------------


class BrowserManager:
    """
    Manages a single long-lived headless Chromium browser instance.

    Can be shared across multiple supplier instances to avoid the overhead
    of launching a new browser per-search.

    Usage::

        with BrowserManager() as bm:
            supplier = LCSCSupplier(browser_manager=bm)
            results  = supplier.search("ESP32-S3")

    Or without a context manager::

        bm = BrowserManager()
        bm.start()
        ...
        bm.stop()
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        proxy: Optional[str] = None,
        slow_mo: int = 0,
    ) -> None:
        self.headless = headless
        self.proxy = proxy if proxy is not None else _resolve_proxy()
        self.slow_mo = slow_mo
        self._pw = None
        self._browser: Optional[Browser] = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch Playwright + Chromium.  Called automatically by ``__enter__``."""
        self._pw = sync_playwright().start()
        kwargs: dict[str, Any] = {
            "headless": self.headless,
            "slow_mo": self.slow_mo,
        }
        if self.proxy:
            kwargs["proxy"] = {"server": self.proxy}
        self._browser = self._pw.chromium.launch(**kwargs)
        log.debug("BrowserManager: Chromium started (headless=%s, proxy=%s)", self.headless, self.proxy)

    def stop(self) -> None:
        """Close browser + Playwright.  Called automatically by ``__exit__``."""
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        log.debug("BrowserManager: stopped")

    @property
    def is_running(self) -> bool:
        return self._browser is not None

    def new_page(self) -> Page:
        """Return a fresh ``Page`` with a standard 1440×900 viewport."""
        if not self._browser:
            raise RuntimeError(
                "BrowserManager is not started. "
                "Call start() or use as a context manager."
            )
        ctx: BrowserContext = self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            user_agent=_USER_AGENT,
        )
        return ctx.new_page()

    def __enter__(self) -> "BrowserManager":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# LCSCSupplier
# ---------------------------------------------------------------------------


class LCSCSupplier:
    """
    LCSC supplier adapter using Playwright for HTML scraping.

    A single browser page is reused across calls.  A new page is created
    transparently if the existing one gets into a broken state.

    Parameters
    ----------
    storage:
        Optional storage backend.  When given, search results and part
        details are cached for *cache_ttl_seconds* to avoid re-scraping.
    browser_manager:
        A pre-started ``BrowserManager``.  When ``None``, one is created
        and owned (started/stopped) by this supplier instance.
    cache_ttl_seconds:
        Cache freshness window.  Default: 24 h.
    max_results:
        Maximum search results to return per query.  Default: 10.
    """

    name: str = _SUPPLIER_NAME

    def __init__(
        self,
        *,
        storage: Optional[StorageProtocol] = None,
        browser_manager: Optional[BrowserManager] = None,
        cache_ttl_seconds: float = 86_400,
        max_results: int = _DEFAULT_MAX_RESULTS,
    ) -> None:
        self._storage = storage
        self._cache_ttl = cache_ttl_seconds
        self._max_results = max_results
        self._owns_browser = browser_manager is None
        self._bm: BrowserManager = browser_manager or BrowserManager()
        self._page: Optional[Page] = None
        self._last_request_at: float = 0.0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._owns_browser:
            self._bm.start()

    def stop(self) -> None:
        if self._page and not self._page.is_closed():
            try:
                self._page.context.close()
            except Exception:
                pass
        self._page = None
        if self._owns_browser:
            self._bm.stop()

    def __enter__(self) -> "LCSCSupplier":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # ── public API ────────────────────────────────────────────────────────────

    def search(self, query: str) -> list[PartResult]:
        """
        Search LCSC for *query*.

        Returns an empty list when nothing matches or a CAPTCHA is detected.
        Raises ``SupplierNetworkError`` on page-load failure.
        """
        cache_key = f"search:{query.lower().strip()}"
        cached = self._get_cache(_SUPPLIER_NAME, cache_key)
        if cached is not None:
            log.debug("Cache hit: search %r", query)
            return [PartResult(**item) for item in cached]

        page = self._get_page()
        url = _SEARCH_URL.format(query=query)
        self._navigate(page, url, wait_selector=SELECTORS["search_row"])

        if _check_captcha(page, SELECTORS["search_captcha"]):
            log.warning("LCSC: CAPTCHA / bot challenge detected — returning []")
            return []

        if _check_no_results(page, SELECTORS["search_no_results"]):
            log.debug("LCSC: no results for %r", query)
            return []

        results = _parse_search_rows(page, self._max_results)
        log.debug("LCSC: search %r → %d results", query, len(results))

        if results:
            self._set_cache(
                _SUPPLIER_NAME, cache_key,
                [r.model_dump(mode="json") for r in results],
            )
        return results

    def get_part(self, part_number: str) -> PartDetail:
        """
        Fetch full detail for LCSC part number *part_number* (e.g. ``"C2913202"``).

        Raises ``PartNotFoundError`` when the part does not exist.
        Raises ``SupplierNetworkError`` on page-load or CAPTCHA failure.
        """
        cached = self._get_cache(_SUPPLIER_NAME, part_number)
        if cached is not None:
            log.debug("Cache hit: part %r", part_number)
            return PartDetail(**cached)

        page = self._get_page()
        url = _DETAIL_URL.format(pn=part_number)
        self._navigate(page, url, wait_selector=SELECTORS["detail_ready"])

        if _check_captcha(page, SELECTORS["detail_captcha"]):
            raise SupplierNetworkError(
                f"CAPTCHA detected while fetching part {part_number!r}"
            )

        detail = _parse_detail_page(page, part_number)
        self._set_cache(_SUPPLIER_NAME, part_number, detail.model_dump(mode="json"))
        return detail

    # ── internals ─────────────────────────────────────────────────────────────

    def _get_page(self) -> Page:
        """Return the shared page, lazily creating one if needed."""
        if self._page is None or self._page.is_closed():
            self._page = self._bm.new_page()
        return self._page

    def _navigate(self, page: Page, url: str, *, wait_selector: str) -> None:
        """Navigate to *url*, throttle, then wait for *wait_selector*.

        Uses ``domcontentloaded`` (not ``networkidle``) because LCSC is a
        Vue SPA that keeps background XHR requests alive indefinitely, so
        ``networkidle`` would never fire.  ``wait_for_selector`` is the real
        readiness gate — it waits until the Vue-rendered content appears.
        """
        self._throttle()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=_PAGE_TIMEOUT)
        except Exception as exc:
            raise SupplierNetworkError(f"Timeout loading {url}: {exc}") from exc

        try:
            page.wait_for_selector(wait_selector, timeout=_WAIT_TIMEOUT)
        except Exception:
            # Content may still be present even if the specific selector timed out.
            # Continue and let parsers decide whether data is available.
            log.debug("wait_for_selector(%r) timed out on %s", wait_selector, url)

    def _throttle(self) -> None:
        """Sleep a random 1–3 s if needed, measuring from the last request."""
        target_delay = random.uniform(_DELAY_MIN, _DELAY_MAX)
        elapsed = time.monotonic() - self._last_request_at
        remaining = target_delay - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _get_cache(self, supplier: str, key: str) -> Optional[dict[str, Any]]:
        if self._storage is None:
            return None
        try:
            return self._storage.get_cached_part(
                supplier, key, max_age_seconds=self._cache_ttl
            )
        except Exception:
            return None

    def _set_cache(self, supplier: str, key: str, data: dict[str, Any]) -> None:
        if self._storage is None:
            return
        try:
            self._storage.cache_part(supplier, key, data)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Page-state helpers
# ---------------------------------------------------------------------------


def _check_captcha(page: Page, selector: str) -> bool:
    try:
        return page.query_selector(selector) is not None
    except Exception:
        return False


def _check_no_results(page: Page, selector: str) -> bool:
    try:
        return page.query_selector(selector) is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Search-page parser
# ---------------------------------------------------------------------------


def _parse_search_rows(page: Page, max_results: int) -> list[PartResult]:
    """Extract PartResult objects from the rendered search results table."""
    rows = page.query_selector_all(SELECTORS["search_row"])
    results: list[PartResult] = []

    for row in rows[:max_results]:
        try:
            result = _parse_one_search_row(row)
            if result:
                results.append(result)
        except Exception as exc:
            log.debug("Row parse error (skipped): %s", exc)

    return results


def _parse_one_search_row(row: Any) -> Optional[PartResult]:
    """
    Parse a single ``<tr id="productId…">`` row into a PartResult.

    Row TD layout (confirmed from captured HTML):
      [0] checkbox
      [1] image + MPN anchor + LCSC-code anchor + datasheet icon
      [2] brand-category text + manufacturer anchor
      [3] "N,NNN In Stock"
      [4..] alternating "N+" / "$X.XXXX" price-break cells
    """
    # ── MPN and LCSC part number ───────────────────────────────────────────
    # The row has 3 product-detail anchors; anchor[0] wraps an image (empty text).
    # Filter to those with visible text: anchor[0]=MPN, anchor[1]=LCSC C-code.
    all_name_anchors = row.query_selector_all(SELECTORS["search_name_anchor"])
    name_anchors = [a for a in all_name_anchors if a.inner_text().strip()]
    if not name_anchors:
        return None

    mpn = name_anchors[0].inner_text().strip()
    if not mpn:
        return None

    # Second text-bearing anchor is the C-code; fall back to extracting from href
    supplier_pn = ""
    if len(name_anchors) >= 2:
        supplier_pn = name_anchors[1].inner_text().strip()
    if not re.match(r"^C\d+$", supplier_pn):
        href = all_name_anchors[0].get_attribute("href") or ""
        m = re.search(r"/(C\d+)\.html", href)
        supplier_pn = m.group(1) if m else ""

    # ── Product URL ────────────────────────────────────────────────────────
    raw_href = all_name_anchors[0].get_attribute("href") or ""
    url = raw_href if raw_href.startswith("http") else _BASE_URL + raw_href

    # ── Manufacturer ───────────────────────────────────────────────────────
    mfr_el = row.query_selector(SELECTORS["search_mfr_anchor"])
    manufacturer = mfr_el.inner_text().strip() if mfr_el else ""

    # ── Description (TD[14] in observed layout — specs/features string) ────
    tds = row.query_selector_all("td")
    td_texts = [_clean_text(td.inner_text()) for td in tds]
    description = _extract_description(td_texts)

    return PartResult(
        mpn=mpn,
        supplier_pn=supplier_pn,
        description=description,
        manufacturer=manufacturer,
        url=url,
    )


# ---------------------------------------------------------------------------
# Detail-page parser
# ---------------------------------------------------------------------------


def _parse_detail_page(page: Page, part_number: str) -> PartDetail:
    """
    Extract full part detail from a rendered LCSC product-detail page.

    Data is read primarily from the structured ``table.tableInfoWrap`` table
    (which contains label→value rows for MPN, Manufacturer, LCSC Part #, etc.)
    and the ``table.priceTable`` price-break table.
    """
    final_url = page.url  # may have redirected from short C-code URL

    # ── Parse the structured info table ────────────────────────────────────
    info = _extract_info_table(page)

    # ── Supplier PN: prefer info-table value, then URL ──────────────────────
    supplier_pn = info.get("LCSC Part #", "")
    if not supplier_pn:
        m = re.search(r"_(C\d+)\.html", final_url)
        supplier_pn = m.group(1) if m else part_number

    # ── MPN: from info table "Mfr. Part #" ─────────────────────────────────
    mpn = info.get("Mfr. Part #", "") or part_number

    # ── Manufacturer: brand-detail anchor inside the info table ───────────
    # The "Manufacturer" cell contains a brand anchor plus optional tag text
    # (e.g. "ESPRESSIF Asian Brands").  Grabbing the anchor text is exact.
    manufacturer = _extract_manufacturer_from_info_table(page) or info.get("Manufacturer", "")

    # ── Description: from info table "Description" or "Key Attributes" ─────
    description = info.get("Description", "") or info.get("Key Attributes", "")

    # ── Price breaks ───────────────────────────────────────────────────────
    price_breaks = _extract_detail_price_breaks(page)

    # ── Stock ──────────────────────────────────────────────────────────────
    stock = _extract_detail_stock(page)

    # ── Datasheet ──────────────────────────────────────────────────────────
    datasheet_url = _extract_detail_datasheet(page, supplier_pn)

    return PartDetail(
        mpn=mpn,
        supplier_pn=supplier_pn,
        description=description or "",
        manufacturer=manufacturer or "",
        url=final_url,
        price_breaks=price_breaks,
        stock=stock,
        datasheet_url=datasheet_url or None,
    )


def _extract_info_table(page: Page) -> dict[str, str]:
    """
    Parse ``table.tableInfoWrap`` into a label → value dict.

    Each row has exactly two cells: a label (e.g. "Mfr. Part #") and a value.
    Values are stored as the first non-empty line of the cell text so that
    cells like "ESPRESSIF\\nAsian Brands" yield just "ESPRESSIF".
    """
    result: dict[str, str] = {}
    try:
        tbl = page.query_selector(SELECTORS["detail_info_table"])
        if not tbl:
            return result
        for row in tbl.query_selector_all("tr"):
            cells = row.query_selector_all("td, th")
            if len(cells) >= 2:
                label = _clean_text(cells[0].inner_text())
                # Preserve newlines in raw text so multi-part values can be split
                raw_value = cells[1].inner_text()
                # Take the first non-empty line, then clean that line
                first_line = next(
                    (ln.strip() for ln in raw_value.splitlines() if ln.strip()),
                    "",
                )
                value = _clean_text(first_line)
                if label:
                    result[label] = value
    except Exception as exc:
        log.debug("_extract_info_table failed: %s", exc)
    return result


def _extract_manufacturer_from_info_table(page: Page) -> str:
    """
    Return the manufacturer name by finding the brand-detail anchor that lives
    inside ``table.tableInfoWrap``.  This avoids picking up unrelated brand
    links from the sidebar or related-products sections.
    """
    try:
        el = page.query_selector(
            "table.tableInfoWrap a[href*='/brand-detail/']"
        )
        if el:
            return _clean_text(el.inner_text())
    except Exception as exc:
        log.debug("_extract_manufacturer_from_info_table failed: %s", exc)
    return ""


def _extract_detail_price_breaks(page: Page) -> list[PriceBreakInfo]:
    """
    Extract price breaks from ``table.priceTable``.

    Each data row has three cells: quantity (e.g. "1+"), unit price
    (e.g. "$ 5.7468"), and extended price (ignored).
    """
    breaks: list[PriceBreakInfo] = []
    try:
        tbl = page.query_selector(SELECTORS["detail_price_table"])
        if not tbl:
            return breaks
        for row in tbl.query_selector_all("tr"):
            cells = [_clean_text(td.inner_text()) for td in row.query_selector_all("td")]
            if len(cells) < 2:
                continue
            qty_text, price_text = cells[0], cells[1]
            # qty: "1+", "1,300+"
            m_qty = re.match(r"^([\d,]+)\+$", qty_text)
            # price: "$ 5.7468" or "$5.7468"
            m_price = re.match(r"^\$\s*([\d.]+)$", price_text)
            if m_qty and m_price:
                try:
                    qty = int(m_qty.group(1).replace(",", ""))
                    price = Decimal(m_price.group(1))
                    breaks.append(PriceBreakInfo(min_quantity=qty, unit_price=price))
                except (InvalidOperation, ValueError):
                    pass
    except Exception as exc:
        log.debug("_extract_detail_price_breaks failed: %s", exc)

    return sorted(breaks, key=lambda pb: pb.min_quantity)


def _extract_detail_stock(page: Page) -> int:
    """
    Return stock count, or 0 if not found / out of stock.

    Strategy 1: JSON-LD ``inventoryLevel`` property (most reliable).
    Strategy 2: scan page body text for "N,NNN In Stock" pattern.
    """
    # Strategy 1 — JSON-LD embedded in the page
    try:
        html = page.content()
        m = re.search(r'"inventoryLevel"\s*:\s*(\d+)', html)
        if m:
            return int(m.group(1))
    except Exception:
        pass

    # Strategy 2 — "N,NNN In Stock" visible text
    try:
        body = page.inner_text("body")
        m = re.search(r"([\d,]+)\s+In Stock", body)
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception:
        pass

    return 0


def _extract_detail_datasheet(page: Page, supplier_pn: str) -> Optional[str]:
    """Return an absolute datasheet URL, or None."""
    try:
        el = page.query_selector(SELECTORS["detail_datasheet"])
        if el:
            href = el.get_attribute("href") or ""
            if href:
                return href if href.startswith("http") else _BASE_URL + href
    except Exception:
        pass
    # Construct canonical datasheet URL from supplier PN
    if re.match(r"^C\d+$", supplier_pn):
        return f"{_BASE_URL}/datasheet/{supplier_pn}.pdf"
    return None


# ---------------------------------------------------------------------------
# Shared parsing helpers
# ---------------------------------------------------------------------------


def _price_breaks_from_td_texts(texts: list[str]) -> list[PriceBreakInfo]:
    """
    Scan a flat list of cell texts for consecutive qty/price pairs.

    Pattern:  "N+"  followed immediately by  "$X.XXXX"
    Commas in quantities are handled (e.g. "1,000+").
    """
    breaks: list[PriceBreakInfo] = []
    pending_qty: Optional[int] = None

    for text in texts:
        text = text.strip()
        # Match quantity cell: "1+", "10+", "1,300+"
        m_qty = re.match(r"^([\d,]+)\+$", text)
        # Match price cell: "$4.8016", "$ 5.7468" (with or without space)
        m_price = re.match(r"^\$\s*([\d.]+)$", text)

        if m_qty:
            pending_qty = int(m_qty.group(1).replace(",", ""))
        elif m_price and pending_qty is not None:
            try:
                unit_price = Decimal(m_price.group(1))
                if unit_price >= 0:
                    breaks.append(
                        PriceBreakInfo(min_quantity=pending_qty, unit_price=unit_price)
                    )
            except InvalidOperation:
                pass
            pending_qty = None
        else:
            # Non-matching cell resets the pending quantity
            pending_qty = None

    return breaks


def _extract_description(td_texts: list[str]) -> str:
    """
    Pick the best description from a row's TD texts.

    The specs/features string is usually the longest TD beyond index 10,
    containing comma-separated electronic attributes like "2.4GHz I2C GPIO…".
    """
    candidates = [t for t in td_texts[10:] if len(t) > 20 and "," in t]
    return candidates[0] if candidates else ""


def _extract_text_by_selectors(page: Page, selectors: list[str]) -> str:
    """Try each selector in order; return inner text of the first match."""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                text = _clean_text(el.inner_text())
                if text:
                    return text
        except Exception:
            continue
    return ""


def _clean_text(raw: str) -> str:
    """Collapse whitespace and strip zero-width chars."""
    return re.sub(r"\s+", " ", raw).replace("\u200b", "").strip()


# ---------------------------------------------------------------------------
# Proxy resolution
# ---------------------------------------------------------------------------


def _resolve_proxy() -> Optional[str]:
    """
    Return a proxy URL suitable for Playwright (http:// or socks5://), or None.

    Skips bare ``socks://`` (version-less) as it is not a valid scheme.
    """
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var, "").strip()
        if val and val.startswith(("http://", "https://")):
            return val

    for var in ("ALL_PROXY", "all_proxy"):
        val = os.environ.get(var, "").strip()
        if val and val.startswith(("http://", "https://", "socks4://", "socks5://")):
            return val

    return None
