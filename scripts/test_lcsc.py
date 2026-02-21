"""Smoke-test: search LCSC for ESP32-S3-WROOM and print results + price breaks."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow running directly without pip install
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from rich import box
from rich.console import Console
from rich.table import Table

from bom_manager.storage.sqlite import SQLiteStorage
from bom_manager.suppliers.base import SupplierError
from bom_manager.suppliers.lcsc import BrowserManager, LCSCSupplier

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(name)s  %(message)s")

QUERY = "stm32f103c8t6"
console = Console()


def main() -> None:
    console.print(f"\n[bold cyan]Searching LCSC for:[/] [yellow]{QUERY}[/]\n")

    with SQLiteStorage() as storage, BrowserManager() as bm:
        supplier = LCSCSupplier(storage=storage, browser_manager=bm)

        # ── Search ────────────────────────────────────────────────────────────
        try:
            results = supplier.search(QUERY)
        except SupplierError as exc:
            console.print(f"[bold red]Search failed:[/] {exc}")
            sys.exit(1)

        if not results:
            console.print("[yellow]No results found.[/]")
            return

        tbl = Table(
            "MPN", "LCSC #", "Manufacturer", "Description",
            title=f"Search: '{QUERY}'  ({len(results)} results)",
            box=box.ROUNDED,
            show_lines=True,
        )
        for r in results:
            tbl.add_row(r.mpn, r.supplier_pn, r.manufacturer, r.description[:72])
        console.print(tbl)

        # ── Detail + price breaks ─────────────────────────────────────────────
        first = next((r for r in results if r.supplier_pn), None)
        if not first:
            return

        console.print(
            f"\n[bold cyan]Fetching detail for:[/] "
            f"[yellow]{first.supplier_pn}[/]  ({first.mpn})\n"
        )

        try:
            detail = supplier.get_part(first.supplier_pn)
        except SupplierError as exc:
            console.print(f"[bold red]Detail fetch failed:[/] {exc}")
            return

        console.print(f"  [bold]MPN:[/]          {detail.mpn}")
        console.print(f"  [bold]LCSC #:[/]       {detail.supplier_pn}")
        console.print(f"  [bold]Manufacturer:[/] {detail.manufacturer}")
        console.print(f"  [bold]Stock:[/]        {detail.stock:,}")
        console.print(f"  [bold]URL:[/]          {detail.url}")
        if detail.datasheet_url:
            console.print(f"  [bold]Datasheet:[/]   {detail.datasheet_url}")

        if detail.price_breaks:
            pb_tbl = Table(
                "Min Qty", "Unit Price (USD)",
                title="Price Breaks",
                box=box.SIMPLE,
            )
            for pb in detail.price_breaks:
                pb_tbl.add_row(f"{pb.min_quantity:,}", f"${pb.unit_price:.4f}")
            console.print(pb_tbl)
        else:
            console.print("\n  [dim]No price break data found.[/]")

        console.print()

        supplier.stop()


if __name__ == "__main__":
    main()
