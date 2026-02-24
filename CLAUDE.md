# BOM Manager — Claude Code Guide

## Project Overview

BOM Manager is a Python CLI/TUI application for tracking and pricing electronics Bills of Materials (BOMs). It searches multiple electronics suppliers (LCSC in USD, Lion Electronic in IRR) for real-time part prices, supports manual price entry, and stores everything in a local SQLite database with a configurable USD↔IRR exchange rate.

**Primary entry point:** `bom` (installs as a script via `pyproject.toml`)

---

## Running the App

```bash
# Launch TUI (default)
bom

# Use the original Click CLI
bom --cli <command>

# Examples
bom --cli project list
bom --cli version create MyProject v1.0
bom --cli bom add MyProject v1.0 "ESP32" --qty 5
```

Install for development:
```bash
pip install -e .
playwright install chromium   # required for LCSC scraping
```

Database is created automatically at `data/bom.db` on first run. Override with `--db PATH` or `BOM_DB` env var.

---

## Architecture

Strict layered architecture — never skip layers:

```
interfaces/   (CLI + TUI)
    ↓
core/         (services + models + exceptions + currency)
    ↓
storage/      (StorageProtocol + SQLiteStorage)
    +
suppliers/    (SupplierProtocol + LCSCSupplier + LionSupplier)
```

### Key files

| File | Purpose |
|---|---|
| `src/bom_manager/interfaces/main.py` | Entry point — launches TUI or delegates to CLI |
| `src/bom_manager/interfaces/tui.py` | Textual TUI application |
| `src/bom_manager/interfaces/cli.py` | Original Click CLI (do not break) |
| `src/bom_manager/core/models.py` | Pydantic domain models |
| `src/bom_manager/core/project_service.py` | Project + version CRUD |
| `src/bom_manager/core/bom_service.py` | BOM items, export, diff, search, manual add, multi-source |
| `src/bom_manager/core/currency.py` | IRR/USD helpers, Persian numeral normalization, formatting |
| `src/bom_manager/core/settings_service.py` | Settings CRUD — exchange rate, active suppliers |
| `src/bom_manager/core/exceptions.py` | Exception hierarchy |
| `src/bom_manager/storage/sqlite.py` | SQLite persistence |
| `src/bom_manager/storage/base.py` | `StorageProtocol` interface |
| `src/bom_manager/suppliers/lcsc.py` | LCSC Playwright scraper (prices in USD) |
| `src/bom_manager/suppliers/lion.py` | Lion Electronic httpx scraper (prices in IRR) |
| `src/bom_manager/suppliers/base.py` | `SupplierProtocol` interface |
| `data/bom.db` | SQLite database (auto-created, not committed) |
| `exports/` | BOM export output directory |

---

## Domain Models (`core/models.py`)

```
Project
  id, name, description, created_at, updated_at

ProjectVersion
  id, project_id, version_name, notes, created_at

PriceBreak
  min_quantity, unit_price   (frozen, immutable)

SupplierSource                        ← added v0.3.0
  supplier: str                       # "lcsc", "lion", "manual"
  supplier_part_number: Optional[str]
  supplier_url: Optional[str]
  matched_mpn: Optional[str]
  unit_price: Optional[Decimal]
  price_breaks: list[PriceBreak]
  currency: str = "USD"              # "USD" or "IRR"
  (frozen, immutable)

BOMItem
  id, version_id, reference_designator, user_part_name
  matched_mpn, supplier, supplier_part_number, supplier_url
  quantity, unit_price, price_breaks: list[PriceBreak], total_price
  currency: str = "USD"              # "USD" (LCSC/manual) or "IRR" (Lion)
  alt_sources: list[SupplierSource]  ← added v0.3.0 (alternative supplier sources)
  .effective_unit_price() → best price for current qty from price_breaks
  .calculate_total()      → qty × effective_unit_price

BOMSummary
  version_id, items: list[BOMItem], total_cost, item_count
  .from_items(version_id, items) → classmethod builder
```

---

## Currency System (`core/currency.py`)

```python
normalize_number(text)          # Persian/Arabic numerals → ASCII digits
parse_price(text)               # extract Decimal from price string
usd_to_irr(amount, rate)        # rate = IRR per 1 USD (e.g. Decimal("600000"))
irr_to_usd(amount, rate)        # rate = IRR per 1 USD
convert(amount, from_cur, to_cur, rate) → Decimal

fmt_irr(amount)                 # "IRR 600,000"  (plain text, no Farsi symbol)
fmt_usd(amount)                 # "$0.1234"
fmt_amount(amount, currency)    # dispatches to fmt_irr or fmt_usd
fmt_price(amount, currency, *, rate=None)  # native + dim converted secondary
parse_manual_price(text)        # "600000 IRR" or "0.50 USD" → (Decimal, str)
                                # accepts: "USD", "$", "IRR", "rial" suffix
```

**Note:** `fmt_irr()` outputs `IRR {amount}` (plain ASCII). The Farsi ﷼ symbol is intentionally not used — it renders poorly in most terminals.

---

## Settings System (`core/settings_service.py`)

```python
SettingsService(storage)
  .get(key)                     # → str (falls back to DEFAULTS)
  .set(key, value)
  .get_rate()                   # → Decimal  (usd_to_irr_rate)
  .set_rate(rate)               # validates > 0
  .fetch_live_rate()            # GET open.er-api.com/v6/latest/USD → Decimal|None
  .get_active_suppliers()       # → ["lcsc", "lion"] | ["lcsc"] | ["lion"]
  .set_active_suppliers(value)  # "lcsc" | "lion" | "all"
  .all_settings()               # → dict[str, str]

DEFAULTS = {"usd_to_irr_rate": "600000", "active_suppliers": "all", ...}
```

**Important:** `fetch_live_rate()` fetches the **official interbank rate**. Lion Electronic uses the **market (bazaar) rate** which can differ significantly. Always warn users to verify and override manually with `settings rate <VALUE>`.

---

## Suppliers

### LCSC (`suppliers/lcsc.py`)

**Why Playwright?** LCSC is a Vue/Nuxt SPA with CloudFront WAF protection. Uses Playwright headless Chromium. Prices in **USD**. Starts Playwright lazily via `svc.bom_service()`.

### Lion Electronic (`suppliers/lion.py`)

**Why httpx (not Playwright)?** lionelectronic.ir is server-side rendered PHP. Search is a plain JSON API endpoint. Prices in **IRR** (Iranian Rial).

```
Search: GET https://lionelectronic.ir/products/products-name-list?q={query}
        → [{"id": "2769", "value": "ESP32-C3-DevKitC-02", "type": "products"}]

Detail: GET https://lionelectronic.ir/products/{id}-{slug}
        → HTML parsed with selectolax; price breaks extracted from tables
        → Persian numerals (۰-۹) normalized via normalize_number()
        → Manufacturer extracted from .detail-row elements (see HTML structure below)
```

**Lion HTML structure (product detail page):**
```html
<div class="detail-row">
    <div class="detail-label">Diodes Incorporated</div>  <!-- VALUE (the manufacturer name) -->
    <div class="detail-value">Manufacture</div>           <!-- KEY (confusingly named) -->
</div>
```
`.detail-label` holds the *value* and `.detail-value` holds the *key name*. The `_parse_manufacturer()` helper searches for `.detail-value` containing "manufactur" and then reads the sibling `.detail-label`.

**Reliability settings (current values):**
- `_MIN_DELAY = 2.0`, `_MAX_DELAY = 4.0` — polite delays between requests
- `timeout=40` — product pages are slow (PHP SSR); increased from 20s
- Retry logic: 2 attempts with 4s delay on transient network errors
- `Referer: https://lionelectronic.ir/products` header included

**Error handling in `get_part()`:**
- HTTP 404/410 → raises `PartNotFoundError` immediately (no retry)
- Timeout / network error → retries once after 4s; if both fail, raises `SupplierNetworkError` (not `PartNotFoundError`)
- This prevents timeouts from being misreported as "product not found"

`LionSupplier` uses 24h cache, `name = "lion"`. Does **not** need Playwright.

---

## Service Layer

### `_Services` (defined in `cli.py`, reused by `tui.py`)

Lazy dependency injection container. Never instantiate services directly — use this:

```python
from bom_manager.interfaces.cli import _Services

svc = _Services(db_path=None)          # uses data/bom.db
svc.storage()                          # SQLiteStorage
svc.project_service()                  # ProjectService
svc.bom_service_ro()                   # BOMService without supplier (reads, copy, diff, export, add_part_manual)
svc.bom_service()                      # BOMService with LCSCSupplier (starts Playwright browser)
svc.supplier()                         # LCSCSupplier directly
svc.lion_supplier()                    # LionSupplier (httpx, no browser)
svc.settings_service()                 # SettingsService
svc.get_active_suppliers()             # → list of active supplier instances per settings
svc.close()                            # closes storage + stops browser
```

`bom_service()` starts Playwright on first call — only use it when you need LCSC search/fetch. Use `bom_service_ro()` for everything else including manual price adds.

### `ProjectService` (`core/project_service.py`)

```python
create_project(name, description) → Project
list_projects()                   → list[Project]
get_project(name_or_id)           → Project        # accepts str name or UUID
delete_project(project_id)        → None
create_version(project_id, version_name, notes) → ProjectVersion
get_version(version_id)           → ProjectVersion
list_versions(project_id)         → list[ProjectVersion]
```

### `BOMService` (`core/bom_service.py`)

```python
search_parts(query)                                               → list[PartResult]
search_parts_all(query, suppliers)                                → list[(supplier_name, PartResult)]  # static, parallel
add_part(version_id, user_part_name, quantity, ref, supplier_pn)  → BOMItem  # needs supplier
add_part_manual(version_id, user_part_name, quantity, ref, unit_price, currency) → BOMItem  # no supplier
remove_part(version_id, item_id)                                  → None
update_quantity(version_id, item_id, new_qty)                     → BOMItem
get_bom(version_id)                                               → BOMSummary
export_bom(version_id, format, output_dir, filename)              → Path
copy_version(source_version_id, new_version_name, notes)          → ProjectVersion
diff_versions(version_a_id, version_b_id)                         → VersionDiff
add_source_to_item(version_id, item_id, source: SupplierSource)   → BOMItem  ← added v0.3.0
use_source(version_id, item_id, alt_index: int)                   → BOMItem  ← added v0.3.0
```

`search_parts_all()` uses `ThreadPoolExecutor` — safe to call from a worker thread.

`VersionDiff` has: `.added`, `.removed`, `.changed` (list of `(old, new)` tuples), `.is_identical`

**`add_source_to_item(version_id, item_id, source)`** — appends a `SupplierSource` to `item.alt_sources` and persists.

**`use_source(version_id, item_id, alt_index)`** — promotes `alt_sources[alt_index]` (0-based) to primary supplier. The old primary is pushed into `alt_sources`. Recalculates `unit_price` and `total_price` from the new primary's price breaks.

**`_best_unit_price_for_qty(price_breaks: list, quantity: int)`** — module-level helper; accepts any list with `.min_quantity` / `.unit_price` attributes (works with both `PriceBreak` domain objects and `PriceBreakInfo` supplier objects).

---

## Multi-Source Supplier Grouping (v0.3.0)

Each BOM item can hold multiple supplier sources. The first (primary) source drives cost calculations. Alternatives are stored in `item.alt_sources` and can be promoted to primary at any time.

### Commands (CLI and TUI)

```
bom sources <project> <version> <item-id>
    Show primary + all alt sources for an item.
    Output: table with [primary] / [alt N] rows, supplier PN, MPN, unit price.
    Hint printed at bottom: "Use bom use-source ... N to activate alt N"

bom add-source <project> <version> <item-id> [--query TEXT] [--manual PRICE]
    Add an alternative source to an existing item.
    --manual PRICE   → add immediately without searching (e.g. "600000 IRR" or "0.50 USD")
    (no flags)       → search all active suppliers, pick from list, confirm, add as alt

bom use-source <project> <version> <item-id> <N>
    Promote alt source N (1-based, matching bom sources output) to primary.
    Old primary is pushed into alt_sources. Price recalculated from new primary's breaks.
```

### `bom list` alt indicator

When `len(item.alt_sources) > 0`, the Supplier PN column shows:
```
C2913202 (+2 alt)
```

### TUI state machine for `bom add-source`

`_cmd_bom_add_source` reuses the existing `pick_part_multi` → `confirm_add` flow with extra data injected into `PendingInteraction`:

```python
PendingInteraction(
    kind="pick_part_multi",
    data={
        ...,
        "mode": "add_source",          # signals add-source path
        "target_item_id": item.id,     # item to append to
    }
)
```

When `mode == "add_source"` is present, `_bom_add_persist` calls `add_source_to_item()` instead of creating a new `BOMItem`. The `mode` and `target_item_id` are propagated from `pick_part_multi` into `confirm_add` by the `_handle_pending_worker` transition.

---

## TUI (`interfaces/tui.py`)

Built with [Textual](https://textual.textualize.io/) (v8+).

### Layout

```
Header
├── ProjectPanel (35% height) — Tree of projects/versions with item counts + costs
├── RichLog (remaining) — scrollable command output
└── CommandBar (3 lines) — ">" prompt + CommandInput
Footer
```

### Critical: Async/Sync bridge

`LCSCSupplier` uses `sync_playwright` (blocking). Textual runs on asyncio. **All service calls must run in a worker thread:**

```python
@work(thread=True, exclusive=False, name="cmd")
def my_worker(self) -> None:
    # Service calls here — safe to block
    result = svc.project_service().list_projects()
    # UI updates must use call_from_thread
    self.call_from_thread(self.some_ui_method, result)
```

Never call Textual widget methods directly from a worker thread. Always use `self.call_from_thread(method, args)`.

### Multi-step command flow (`bom add`)

The `bom add` command is a state machine managed via `PendingInteraction`:

```
Search all active suppliers (parallel)
  ↓
PendingInteraction(kind="pick_part_multi")  — numbered table [LCSC]/[LION] + M=manual
  ↓ user picks a number
  ├─ supplier result → fetch PartDetail → show price breaks (with manufacturer in title)
  │    ↓
  │  PendingInteraction(kind="confirm_add")
  │    ↓ y → _build_item() from PartDetail → storage.add_item() → refresh tree
  │
  └─ M (manual) → PendingInteraction(kind="manual_price")
         ↓ "600000 IRR" or "0.50 USD"
       PendingInteraction(kind="confirm_manual_add")
         ↓ y → bom_service_ro().add_part_manual() → refresh tree
```

`self._pending: Optional[PendingInteraction]` is the state holder. When set, the next input submission is routed to `_handle_pending_worker` instead of `_execute_command_worker`.

**Critical:** After a supplier result is confirmed, `_bom_add_persist` calls `_build_item()` from `bom_service.py` directly and then `svc.storage().add_item()` — it does **NOT** call `svc.bom_service().add_part()` (which would re-run Playwright). The `PartDetail` is cached in `pending.data["detail"]`.

### Adding a new TUI command

1. Write a handler function with this signature:
   ```python
   def _cmd_mycommand(
       args: list[str], flags: dict[str, str],
       svc: _Services, print_fn: PrintFn, err_fn: ErrFn, refresh_fn: RefreshFn
   ) -> Optional[PendingInteraction]:
       ...
   ```
2. Add it to `COMMANDS` dict: `("mygroup", "myverb"): _cmd_mycommand`
3. Call `print_fn(rich_table_or_str)` for output, `err_fn(msg)` for errors
4. Call `refresh_fn()` after any mutation (triggers tree reload)
5. Return `PendingInteraction(...)` if the command needs a follow-up input, else `None`

---

## CLI (`interfaces/cli.py`)

Click-based. **Do not modify this file** unless fixing a bug — the TUI reuses `_Services` from it.

### Command structure

```
bom [--db PATH]
  project
    create <name> [--description TEXT]
    list
    delete <name> [--yes]
  version
    create <project> <version> [--notes TEXT]
    list <project>
    delete <project> <version> [--yes]
    copy <project> <source> <new> [--notes TEXT]
  bom
    list <project> <version>                          # Supplier PN shows "+N alt" when sources exist
    add <project> <version> <part> --qty N [--ref D]  # multi-supplier search
    remove <project> <version> <item-id> [--yes]
    update-qty <project> <version> <item-id> <new-qty>
    cost <project> <version> [--boards N]
    export <project> <version> [--format csv|xlsx] [--output-dir DIR]
    diff <project> <version-a> <version-b>
    sources <project> <version> <item-id>             # show primary + alt sources
    add-source <project> <version> <item-id>          # add alt source [--query TEXT] [--manual PRICE]
    use-source <project> <version> <item-id> <N>      # promote alt N (1-based) to primary
  settings
    show                              # all settings + warnings
    rate [VALUE]                      # get or set USD→IRR rate
    fetch-rate                        # auto-fetch from open.er-api.com
    suppliers [lcsc|lion|all]         # get or set active suppliers
```

---

## Storage (`storage/sqlite.py`)

SQLite with `sqlite3` stdlib. Foreign keys enabled. All IDs stored as `TEXT` (UUID strings).

**Tables:**
- `projects` — id, name, description, created_at, updated_at
- `project_versions` — id, project_id (FK→projects CASCADE), version_name, notes, created_at
- `bom_items` — id, version_id (FK→project_versions CASCADE), all BOMItem fields; `price_breaks` stored as JSON; `currency TEXT DEFAULT 'USD'`; `alt_sources TEXT DEFAULT '[]'` (JSON array of SupplierSource objects)
- `part_cache` — supplier_pn → JSON data, fetched_at (24-hour TTL to avoid re-scraping); keyed as `"lion:LION-123"` or `"lcsc:C701342"`
- `settings` — key TEXT PRIMARY KEY, value TEXT (usd_to_irr_rate, active_suppliers, rate_last_fetched)

**Migrations** — `_migrate()` runs on every open and is idempotent:
- v0.2.0: adds `currency TEXT NOT NULL DEFAULT 'USD'` to `bom_items`
- v0.3.0: adds `alt_sources TEXT NOT NULL DEFAULT '[]'` to `bom_items`

Each migration is wrapped in `try/except` so it silently skips if the column already exists.

**`StorageProtocol`** (`storage/base.py`) defines the interface including `get_setting(key)` / `set_setting(key, value)`. To add a new storage backend, implement this protocol.

---

## Supplier Protocol (`suppliers/base.py`)

```python
class SupplierProtocol(Protocol):
    name: str                                         # "lcsc", "lion", etc.
    def search(self, query: str) -> list[PartResult]: ...
    def get_part(self, part_number: str) -> PartDetail: ...
    def close(self) -> None: ...

class PartDetail(PartResult):
    price_breaks: list[PriceBreakInfo]
    stock: int
    datasheet_url: Optional[str]
    manufacturer: str                   # now populated for Lion results
    currency: str = "USD"              # "USD" for LCSC, "IRR" for Lion
    .best_unit_price(quantity) → Optional[Decimal]
```

To add a new supplier: implement `SupplierProtocol`, set `name`, wire into `_Services.get_active_suppliers()`.

`PartDetail.best_unit_price(quantity)` — returns lowest applicable unit price for a given quantity from price breaks.

Parts are cached in `part_cache` SQLite table for 24 hours (keyed as `"{supplier_name}:{supplier_pn}"`).

---

## Exception Hierarchy

```
BOMManagerError          (base — catch this for all domain errors)
├── ProjectNotFoundError
├── VersionNotFoundError
├── ItemNotFoundError
├── SupplierLookupError
└── ExportError

SupplierError            (base — from suppliers/base.py)
├── PartNotFoundError
├── SupplierNetworkError
└── SupplierParseError
```

Always catch specific exceptions in command handlers. Never swallow `BOMManagerError` silently — surface it to the user.

**Important:** `SupplierNetworkError` covers both true network failures *and* HTTP timeouts. Do not catch it and re-raise as `PartNotFoundError` — a timeout does not mean the part is missing.

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `textual` | ≥0.50.0 | TUI framework |
| `rich` | ≥13.0.0 | Terminal tables, colors, formatting |
| `click` | ≥8.0.0 | CLI framework |
| `pydantic` | ≥2.0.0 | Data model validation |
| `playwright` | ≥1.40.0 | Headless Chromium for LCSC scraping |
| `httpx` | ≥0.27.0 | HTTP client — Lion Electronic scraping + exchange rate fetch |
| `selectolax` | ≥0.3.21 | Fast HTML parser — Lion product page scraping |
| `curl_cffi` | ≥0.7.0 | HTTP with antibot features (reserved) |
| `openpyxl` | ≥3.1.0 | Excel export |

---

## Common Patterns

### Resolving a project + version in handlers

```python
from bom_manager.core.exceptions import ProjectNotFoundError, VersionNotFoundError

try:
    project = svc.project_service().get_project(project_name)   # by name or UUID
    versions = svc.project_service().list_versions(project.id)
    ver = next((v for v in versions if v.version_name == version_name), None)
    if ver is None:
        raise VersionNotFoundError(f"{version_name!r} not found")
except (ProjectNotFoundError, VersionNotFoundError) as exc:
    err_fn(str(exc))
    return None
```

### Resolving a BOM item by ID prefix

```python
items = svc.storage().list_items_by_version(version_id)
matched = [i for i in items if str(i.id).startswith(prefix)]
# check len(matched) == 1
```

### Formatting prices

```python
# Legacy TUI helper (USD only)
def _fmt_price(price: Optional[Decimal], *, dash: str = "—") -> str:
    return f"${price:.4f}" if price is not None else dash

# Multi-currency — use for bom list display
from bom_manager.core.currency import fmt_price, fmt_irr, fmt_usd
rate = svc.settings_service().get_rate()
fmt_price(unit_price, item.currency, rate=rate)
# → "IRR 25,148,340  ($41.91)" for IRR items
# → "$0.1234  (IRR 74,040)" for USD items
```

### Building Rich tables for output

```python
from rich import box
from rich.table import Table

tbl = Table(box=box.ROUNDED, show_lines=True, pad_edge=True)
tbl.add_column("Name", style="bold cyan", no_wrap=True)
tbl.add_column("Value", justify="right")
tbl.add_row("MyProject", "v1.0")
print_fn(tbl)   # in TUI handlers; console.print(tbl) in CLI
```

### Price breaks table title with manufacturer

When displaying a part detail confirmation, include manufacturer in the title if available:

```python
mfr_part = f"  ·  [dim]{detail.manufacturer}[/]" if detail.manufacturer else ""
pb_tbl = Table(
    title=(
        f"[bold]{detail.mpn}[/]{mfr_part}  ·  "
        f"Stock: [{stock_color}]{detail.stock:,}[/]  ·  "
        f"[dim]{detail.currency}[/]"
    ),
    box=box.SIMPLE, show_edge=False, pad_edge=True,
)
```

### Building a SupplierSource from a PartDetail

```python
from bom_manager.core.models import PriceBreak, SupplierSource

price_breaks = [
    PriceBreak(min_quantity=pb.min_quantity, unit_price=pb.unit_price)
    for pb in detail.price_breaks
]
source = SupplierSource(
    supplier=supplier_name,
    supplier_part_number=detail.supplier_pn or None,
    supplier_url=detail.url or None,
    matched_mpn=detail.mpn or None,
    unit_price=detail.best_unit_price(qty),
    price_breaks=price_breaks,
    currency=detail.currency,
)
svc.bom_service_ro().add_source_to_item(version_id, item_id, source)
```

---

## What NOT to do

- **Don't call `svc.bom_service()`** (starts Playwright) unless the command needs LCSC search or fetch. Use `svc.bom_service_ro()` for reads, copy, diff, export, update-qty, remove, manual add, and all multi-source operations.
- **Don't call `add_part()` from the TUI's `confirm_add` handler** — the `PartDetail` is already fetched and cached in `pending.data["detail"]`. Use `_build_item()` + `storage.add_item()` directly to avoid re-scraping.
- **Don't call widget methods from worker threads** in the TUI — use `call_from_thread`.
- **Don't modify `cli.py`** for TUI-only features — keep the CLI working independently.
- **Don't add new storage tables** without updating `StorageProtocol` in `storage/base.py`.
- **Don't use `sys.exit()`** in TUI command handlers — raise or call `err_fn` instead.
- **Don't trust the auto-fetched IRR rate for Lion pricing** — `open.er-api.com` returns the official interbank rate; Lion Electronic uses the market rate. Always surface the warning.
- **Don't catch `SupplierNetworkError` and re-raise as `PartNotFoundError`** — a network timeout or connection reset does not mean the part doesn't exist. Surface the real error to the user.
- **Don't use the Farsi ﷼ symbol** for IRR display — use `fmt_irr()` which outputs `IRR {amount}` in plain ASCII.

---

## Changelog

### v0.3.0 (2026-02-24)

**IRR display fix**
- `fmt_irr()` now returns `IRR {amount:,.0f}` instead of `﷼{amount:,.0f}`. The Farsi rial symbol caused rendering issues in many terminals.
- `parse_manual_price()` error message updated to list accepted formats: `USD`, `$`, `IRR`, `rial`.

**Multi-source supplier grouping**
- New `SupplierSource` model (`core/models.py`) — frozen Pydantic model holding a single supplier's data for a part.
- `BOMItem.alt_sources: list[SupplierSource]` — list of alternative sources; empty by default.
- Storage migration adds `alt_sources TEXT NOT NULL DEFAULT '[]'` column to `bom_items`.
- `BOMService.add_source_to_item()` — appends a `SupplierSource` to an item's `alt_sources`.
- `BOMService.use_source()` — promotes an alt source to primary, pushes old primary into alts, recalculates price.
- `_best_unit_price_for_qty()` refactored to accept a `list` directly (duck-typed; works with both `PriceBreak` and `PriceBreakInfo`).
- CLI: new `bom sources`, `bom add-source`, `bom use-source` commands.
- TUI: new `_cmd_bom_sources`, `_cmd_bom_add_source`, `_cmd_bom_use_source` handlers; `_bom_add_persist` updated to handle `mode="add_source"`.
- `bom list` shows `+N alt` indicator in Supplier PN column when alt sources exist.

**Lion Electronic bug fixes**
- Increased `httpx` timeout from 20s to 40s — product pages (SSR PHP) are significantly slower than the JSON search API.
- Added retry loop (2 attempts, 4s delay) for transient network errors in `get_part()`.
- Fixed misleading error: `SupplierNetworkError` (timeout) was being caught and re-raised as `PartNotFoundError`. Now only HTTP 404/410 raises `PartNotFoundError`; all other failures raise `SupplierNetworkError`.
- Added `_parse_manufacturer(tree)` — extracts manufacturer from `.detail-row` HTML structure. `.detail-label` holds the value (e.g. `"Diodes Incorporated"`) and `.detail-value` holds the key name `"Manufacture"` (inverted naming in Lion's HTML).
- Added `_parse_datasheet_url(tree)` — extracts datasheet link from product page.
- Added `Referer: https://lionelectronic.ir/products` header to reduce server-side throttling.
- Increased polite delays: `_MIN_DELAY = 2.0s`, `_MAX_DELAY = 4.0s` (was 1.0–2.5s).
- Price-breaks confirmation table now shows manufacturer in title: `AZ1117CR2-3.3TRG1  ·  Diodes Incorporated  ·  Stock: 850  ·  IRR` (both CLI and TUI).
