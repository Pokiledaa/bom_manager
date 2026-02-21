# bom-manager

A Python tool for managing electronics Bills of Materials (BOMs) with live supplier integration. It lets you organize projects and component lists, then automatically look up real-time pricing and stock from electronic component distributors.

## Concept

When building electronics hardware, a BOM is the list of every component that goes into a design — resistors, capacitors, microcontrollers, connectors, and so on. Managing a BOM manually is tedious: you have to visit supplier websites one by one, copy prices into spreadsheets, and repeat the process every time stock or pricing changes.

bom-manager automates this by:

1. Storing your projects and BOM items in a local SQLite database
2. Scraping supplier websites in the background using a real headless browser
3. Caching results for 24 hours to avoid redundant requests
4. Exposing a clean Python API so the data can be used by other tools or scripts

## Features

- **Project and version management** — group BOM items under named projects with multiple versions (e.g. prototype, rev-A, production)
- **Structured BOM items** — each item carries a reference designator, part name, quantity, matched MPN, and supplier info
- **LCSC supplier integration** — searches `lcsc.com` for parts and retrieves full detail: MPN, manufacturer, stock count, price breaks, datasheet link
- **Price break awareness** — stores tiered pricing (e.g. 1+, 10+, 100+) so you can calculate the real cost at your target production volume
- **24-hour cache** — supplier data is cached in SQLite; repeated lookups for the same part number are served locally without hitting the website again
- **Pluggable supplier protocol** — new suppliers can be added by implementing the `SupplierProtocol` interface (`search` + `get_part` methods)
- **Pluggable storage protocol** — the `StorageProtocol` interface allows swapping the SQLite backend for any other store

## Project structure

```
bom-manager/
├── src/bom_manager/
│   ├── core/
│   │   └── models.py          # Domain models: Project, BOMItem, PriceBreak, BOMSummary
│   ├── suppliers/
│   │   ├── base.py            # SupplierProtocol, PartResult, PartDetail, PriceBreakInfo
│   │   └── lcsc.py            # LCSC adapter (Playwright-based browser scraper)
│   └── storage/
│       ├── base.py            # StorageProtocol
│       └── sqlite.py          # SQLite implementation with part cache
├── scripts/
│   └── test_lcsc.py           # End-to-end smoke test: search + detail + price breaks
├── data/                      # SQLite database (auto-created, git-ignored)
└── pyproject.toml
```

## Why Playwright?

LCSC's website is a Vue SPA — the server returns a JavaScript shell with no product data. All content is rendered client-side after page load. Their JSON API backend is also protected by CloudFront WAF, which blocks all non-browser HTTP clients.

Playwright launches a real headless Chromium browser that executes JavaScript and passes every WAF challenge, giving us the fully-rendered DOM with accurate prices and stock counts.

## Quick start

```bash
# Install dependencies
pip install -e .

# Install Playwright's Chromium browser
playwright install chromium

# Run the smoke test (searches LCSC for ESP32-S3-WROOM and prints price breaks)
python scripts/test_lcsc.py
```

## Dependencies

| Package | Purpose |
|---|---|
| `playwright` | Headless browser for scraping LCSC |
| `pydantic` | Data models with validation |
| `rich` | Terminal tables and formatting |
| `httpx` | HTTP client (for future supplier integrations) |
| `click` | CLI framework |
