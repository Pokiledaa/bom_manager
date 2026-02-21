"""Capture LCSC detail page HTML and screenshot for selector inspection."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from playwright.sync_api import sync_playwright

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# A known LCSC product detail URL
DETAIL_URL = "https://www.lcsc.com/product-detail/ESP32-S3-WROOM-1-N8_ESPRESSIF_C2913202.html"

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            user_agent=_USER_AGENT,
        )
        page = ctx.new_page()

        print(f"Navigating to: {DETAIL_URL}")
        page.goto(DETAIL_URL, wait_until="domcontentloaded", timeout=60_000)

        # Wait for JS rendering
        time.sleep(5)

        # Try to wait for a product container
        for sel in [
            "[class*='product']",
            "h1",
            ".product-container",
            "[class*='detail']",
        ]:
            try:
                page.wait_for_selector(sel, timeout=5_000)
                print(f"  Found selector: {sel}")
                break
            except Exception:
                pass

        # Save screenshot
        screenshot_path = OUTPUT_DIR / "lcsc_detail.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"Screenshot saved: {screenshot_path}")

        # Save HTML
        html = page.content()
        html_path = OUTPUT_DIR / "lcsc_detail.html"
        html_path.write_text(html, encoding="utf-8")
        print(f"HTML saved: {html_path}  ({len(html):,} bytes)")

        # Print useful structural info
        print("\n--- Page title ---")
        print(page.title())

        print("\n--- h1 elements ---")
        for el in page.query_selector_all("h1"):
            text = el.inner_text().strip()
            cls = el.get_attribute("class") or ""
            if text:
                print(f"  class={cls!r}  text={text[:120]!r}")

        print("\n--- Elements with 'price' in class ---")
        for el in page.query_selector_all("[class*='price']"):
            text = el.inner_text().strip()[:60]
            cls = el.get_attribute("class") or ""
            tag = el.evaluate("el => el.tagName.toLowerCase()")
            if text:
                print(f"  <{tag} class={cls!r}>  {text!r}")

        print("\n--- Elements with 'stock' in class ---")
        for el in page.query_selector_all("[class*='stock'], [class*='Stock']"):
            text = el.inner_text().strip()[:80]
            cls = el.get_attribute("class") or ""
            tag = el.evaluate("el => el.tagName.toLowerCase()")
            if text:
                print(f"  <{tag} class={cls!r}>  {text!r}")

        print("\n--- Elements with 'brand' or 'manufacturer' in class ---")
        for el in page.query_selector_all(
            "[class*='brand'], [class*='Brand'], "
            "[class*='manufacturer'], [class*='Manufacturer']"
        ):
            text = el.inner_text().strip()[:80]
            cls = el.get_attribute("class") or ""
            tag = el.evaluate("el => el.tagName.toLowerCase()")
            if text:
                print(f"  <{tag} class={cls!r}>  {text!r}")

        print("\n--- Brand-detail anchors ---")
        for el in page.query_selector_all("a[href*='/brand-detail/']"):
            text = el.inner_text().strip()
            href = el.get_attribute("href") or ""
            if text:
                print(f"  href={href!r}  text={text!r}")

        print("\n--- Tables ---")
        for i, tbl in enumerate(page.query_selector_all("table")):
            cls = tbl.get_attribute("class") or ""
            rows = tbl.query_selector_all("tr")
            print(f"  table[{i}] class={cls!r}  rows={len(rows)}")
            for j, row in enumerate(rows[:3]):
                cells = [td.inner_text().strip()[:30] for td in row.query_selector_all("td, th")]
                if any(cells):
                    print(f"    row[{j}]: {cells}")

        browser.close()


if __name__ == "__main__":
    main()
