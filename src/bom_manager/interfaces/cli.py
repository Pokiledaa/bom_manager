"""Click CLI for BOM Manager."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Optional
from uuid import UUID

import click
from rich import box
from rich.console import Console
from rich.prompt import Confirm, IntPrompt
from rich.table import Table

from bom_manager.core.currency import (
    fmt_amount,
    fmt_irr,
    fmt_price,
    fmt_usd,
    irr_to_usd,
    parse_manual_price,
    usd_to_irr,
)
from bom_manager.core.exceptions import (
    BOMManagerError,
    ItemNotFoundError,
    ProjectNotFoundError,
    SupplierLookupError,
    VersionNotFoundError,
)
from bom_manager.core.models import BOMItem, BOMSummary, Project, ProjectVersion, SupplierSource

console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Service container  (lazy DI — browser/supplier only started for bom add)
# ---------------------------------------------------------------------------

class _Services:
    """
    Lazy-initialised holder for all services.

    Storage opens on first use.  The Playwright browser is only started
    when a supplier call is required (i.e. the ``bom add`` command).
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path
        self._storage = None
        self._bm = None
        self._supplier = None
        self._lion = None
        self._project_svc = None
        self._bom_svc = None
        self._bom_svc_ro = None
        self._settings_svc = None

    # ── storage ───────────────────────────────────────────────────────────

    def storage(self):
        if self._storage is None:
            from bom_manager.storage.sqlite import SQLiteStorage
            kwargs = {"db_path": Path(self._db_path)} if self._db_path else {}
            self._storage = SQLiteStorage(**kwargs)
        return self._storage

    # ── supplier (Playwright browser) ────────────────────────────────────

    def supplier(self):
        if self._supplier is None:
            from bom_manager.suppliers.lcsc import BrowserManager, LCSCSupplier
            self._bm = BrowserManager()
            self._bm.start()
            self._supplier = LCSCSupplier(
                storage=self.storage(), browser_manager=self._bm
            )
        return self._supplier

    # ── services ──────────────────────────────────────────────────────────

    def project_service(self):
        if self._project_svc is None:
            from bom_manager.core.project_service import ProjectService
            self._project_svc = ProjectService(self.storage())
        return self._project_svc

    def bom_service(self):
        if self._bom_svc is None:
            from bom_manager.core.bom_service import BOMService
            self._bom_svc = BOMService(self.storage(), self.supplier())
        return self._bom_svc

    def bom_service_ro(self):
        """BOM service without supplier — for copy, diff, and other read operations."""
        if self._bom_svc_ro is None:
            from bom_manager.core.bom_service import BOMService
            self._bom_svc_ro = BOMService(self.storage())
        return self._bom_svc_ro

    # ── Lion Electronic supplier ───────────────────────────────────────────

    def lion_supplier(self):
        if self._lion is None:
            from bom_manager.suppliers.lion import LionSupplier
            self._lion = LionSupplier(storage=self.storage())
        return self._lion

    # ── Settings service ───────────────────────────────────────────────────

    def settings_service(self):
        if self._settings_svc is None:
            from bom_manager.core.settings_service import SettingsService
            self._settings_svc = SettingsService(self.storage())
        return self._settings_svc

    # ── Active suppliers ───────────────────────────────────────────────────

    def get_active_suppliers(self) -> list:
        """Return list of active supplier instances based on settings."""
        names = self.settings_service().get_active_suppliers()
        out = []
        for name in names:
            if name == "lcsc":
                out.append(self.supplier())
            elif name == "lion":
                out.append(self.lion_supplier())
        return out

    # ── cleanup ───────────────────────────────────────────────────────────

    def close(self) -> None:
        for obj, method in [
            (self._storage, "close"),
            (self._bm, "stop"),
            (self._lion, "close"),
        ]:
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _die(msg: str) -> None:
    """Print an error and exit with code 1."""
    err_console.print(f"[bold red]Error:[/bold red] {msg}")
    sys.exit(1)


def _resolve_project(svc: _Services, name: str) -> Project:
    try:
        return svc.project_service().get_project(name)
    except ProjectNotFoundError:
        _die(f"Project {name!r} not found")


def _resolve_version(
    svc: _Services, project_name: str, version_name: str
) -> tuple[Project, ProjectVersion]:
    project = _resolve_project(svc, project_name)
    versions = svc.project_service().list_versions(project.id)
    version = next((v for v in versions if v.version_name == version_name), None)
    if version is None:
        _die(f"Version {version_name!r} not found in project {project_name!r}")
    return project, version


def _resolve_item(svc: _Services, version_id: UUID, prefix: str) -> BOMItem:
    """Find a BOM item by full UUID or by unique prefix."""
    items = svc.storage().list_items_by_version(version_id)
    matched = [i for i in items if str(i.id).startswith(prefix)]
    if not matched:
        _die(f"Item {prefix!r} not found in this version")
    if len(matched) > 1:
        _die(f"Prefix {prefix!r} matches {len(matched)} items — use more characters")
    return matched[0]


def _fmt_price(price: Optional[Decimal], *, dash: str = "—") -> str:
    return f"${price:.4f}" if price is not None else dash


def _best_price_at(price_breaks, quantity: int) -> Optional[Decimal]:
    """Pick the lowest applicable unit price for *quantity* from price_breaks."""
    if not price_breaks:
        return None
    eligible = [pb for pb in price_breaks if pb.min_quantity <= quantity]
    pool = eligible if eligible else price_breaks
    return min(pool, key=lambda pb: pb.unit_price).unit_price


# ---------------------------------------------------------------------------
# Root CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.option(
    "--db",
    default=None,
    metavar="PATH",
    help="Path to SQLite database (default: data/bom.db)",
    envvar="BOM_DB",
)
@click.pass_context
def cli(ctx: click.Context, db: Optional[str]) -> None:
    """BOM Manager — track and price electronics Bills of Materials."""
    ctx.ensure_object(dict)
    services = _Services(db_path=db)
    ctx.obj = services
    ctx.call_on_close(services.close)


# ---------------------------------------------------------------------------
# project commands
# ---------------------------------------------------------------------------

@cli.group()
def project() -> None:
    """Create and manage projects."""


@project.command("create")
@click.argument("name")
@click.option("--description", "-d", default=None, metavar="TEXT", help="Short description")
@click.pass_obj
def project_create(svc: _Services, name: str, description: Optional[str]) -> None:
    """Create a new project."""
    p = svc.project_service().create_project(name, description)
    console.print(
        f"[green]✓[/] Created project [bold cyan]{p.name}[/]  "
        f"[dim]id={str(p.id)[:8]}[/]"
    )


@project.command("list")
@click.pass_obj
def project_list(svc: _Services) -> None:
    """List all projects."""
    projects = svc.project_service().list_projects()
    if not projects:
        console.print("[dim]No projects yet.  Use [bold]bom project create[/bold] to get started.[/]")
        return

    tbl = Table(box=box.ROUNDED, show_lines=False, pad_edge=True, highlight=True)
    tbl.add_column("Name", style="bold cyan", no_wrap=True)
    tbl.add_column("Description")
    tbl.add_column("Created", style="dim", justify="right")
    tbl.add_column("ID", style="dim")

    for p in projects:
        tbl.add_row(
            p.name,
            p.description or "[dim]—[/]",
            p.created_at.strftime("%Y-%m-%d"),
            str(p.id)[:8],
        )
    console.print(tbl)


@project.command("delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_obj
def project_delete(svc: _Services, name: str, yes: bool) -> None:
    """Delete a project and all its versions and BOM items."""
    project = _resolve_project(svc, name)
    if not yes:
        if not Confirm.ask(
            f"Delete project [bold]{project.name}[/] and [bold red]all[/] its data?",
            default=False,
        ):
            console.print("[dim]Aborted.[/]")
            return
    svc.project_service().delete_project(project.id)
    console.print(f"[green]✓[/] Deleted project [bold]{project.name}[/]")


# ---------------------------------------------------------------------------
# version commands
# ---------------------------------------------------------------------------

@cli.group()
def version() -> None:
    """Create and manage project versions."""


@version.command("create")
@click.argument("project_name")
@click.argument("version_name")
@click.option("--notes", "-n", default=None, metavar="TEXT", help="Change notes")
@click.pass_obj
def version_create(
    svc: _Services,
    project_name: str,
    version_name: str,
    notes: Optional[str],
) -> None:
    """Create a new BOM version for a project."""
    project = _resolve_project(svc, project_name)
    v = svc.project_service().create_version(project.id, version_name, notes)
    console.print(
        f"[green]✓[/] Created version [bold]{v.version_name}[/] "
        f"for project [bold cyan]{project.name}[/]  [dim]id={str(v.id)[:8]}[/]"
    )


@version.command("list")
@click.argument("project_name")
@click.pass_obj
def version_list(svc: _Services, project_name: str) -> None:
    """List all versions of a project."""
    project = _resolve_project(svc, project_name)
    versions = svc.project_service().list_versions(project.id)

    if not versions:
        console.print(f"[dim]No versions for {project.name!r}.[/]")
        return

    tbl = Table(
        title=f"Versions — [bold cyan]{project.name}[/]",
        box=box.ROUNDED, show_lines=False, pad_edge=True,
    )
    tbl.add_column("Version", style="bold")
    tbl.add_column("Notes")
    tbl.add_column("Created", style="dim", justify="right")
    tbl.add_column("ID", style="dim")

    for v in versions:
        tbl.add_row(
            v.version_name,
            v.notes or "[dim]—[/]",
            v.created_at.strftime("%Y-%m-%d"),
            str(v.id)[:8],
        )
    console.print(tbl)


@version.command("delete")
@click.argument("project_name")
@click.argument("version_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_obj
def version_delete(
    svc: _Services,
    project_name: str,
    version_name: str,
    yes: bool,
) -> None:
    """Delete a BOM version and all its items."""
    _, ver = _resolve_version(svc, project_name, version_name)

    if not yes:
        if not Confirm.ask(
            f"Delete version [bold]{ver.version_name}[/] and [bold red]all[/] its BOM items?",
            default=False,
        ):
            console.print("[dim]Aborted.[/]")
            return

    svc.storage().delete_version(ver.id)
    console.print(
        f"[green]✓[/] Deleted version [bold]{ver.version_name}[/] "
        f"[dim]({str(ver.id)[:8]})[/]"
    )


@version.command("copy")
@click.argument("project_name")
@click.argument("source_version")
@click.argument("new_version")
@click.option("--notes", "-n", default=None, metavar="TEXT", help="Notes for the new version")
@click.pass_obj
def version_copy(
    svc: _Services,
    project_name: str,
    source_version: str,
    new_version: str,
    notes: Optional[str],
) -> None:
    """Copy a BOM version to a new version (all items are duplicated)."""
    project, src_ver = _resolve_version(svc, project_name, source_version)

    try:
        new_ver = svc.bom_service_ro().copy_version(
            src_ver.id, new_version, notes=notes
        )
    except BOMManagerError as exc:
        _die(str(exc))

    items = svc.storage().list_items_by_version(new_ver.id)
    console.print(
        f"[green]✓[/] Copied [bold]{project.name}[/] / [bold]{src_ver.version_name}[/] "
        f"→ [bold]{new_ver.version_name}[/]  "
        f"[dim]({len(items)} item{'s' if len(items) != 1 else ''} copied · "
        f"id={str(new_ver.id)[:8]})[/]"
    )


# ---------------------------------------------------------------------------
# bom commands
# ---------------------------------------------------------------------------

@cli.group()
def bom() -> None:
    """Manage Bills of Materials."""


# ── bom add ──────────────────────────────────────────────────────────────────

@bom.command("add")
@click.argument("project_name")
@click.argument("version_name")
@click.argument("part_name")
@click.option("--qty", "-q", required=True, type=int, help="Quantity required per board")
@click.option("--ref", "-r", default=None, metavar="DESIGNATOR",
              help="PCB reference designator (e.g. U1, C3,C4).  Defaults to part name.")
@click.pass_obj
def bom_add(
    svc: _Services,
    project_name: str,
    version_name: str,
    part_name: str,
    qty: int,
    ref: Optional[str],
) -> None:
    """Search LCSC, pick a part, and add it to a BOM version."""
    if qty < 1:
        _die("--qty must be >= 1")

    _, ver = _resolve_version(svc, project_name, version_name)
    ref = ref or part_name
    rate = svc.settings_service().get_rate()

    # ── Search all active suppliers ────────────────────────────────────────
    active = svc.get_active_suppliers()
    supplier_labels = " + ".join(s.name.upper() for s in active) if active else "—"
    console.print(f'\nSearching [bold cyan]{supplier_labels}[/] for [bold yellow]"{part_name}"[/bold yellow]...')

    from bom_manager.core.bom_service import BOMService
    combined, search_failures = BOMService.search_parts_all(part_name, active)

    for sup_name, err_msg in search_failures:
        console.print(f"[yellow]⚠ {sup_name.upper()} search failed:[/] [dim]{err_msg}[/]")

    if not combined:
        console.print("[yellow]No results found from any active supplier.[/]")
        console.print("[dim]You can still add a manual price with [bold]bom add[/bold] and select 'M'.[/]")
        combined = []

    # Limit to first 5 per supplier
    from collections import defaultdict
    per_supplier: dict[str, list] = defaultdict(list)
    for sup_name, r in combined:
        if len(per_supplier[sup_name]) < 5:
            per_supplier[sup_name].append((sup_name, r))
    combined_limited = [item for items in per_supplier.values() for item in items]

    # ── Results table ─────────────────────────────────────────────────────
    tbl = Table(box=box.ROUNDED, show_lines=False, pad_edge=True, highlight=True)
    tbl.add_column("#", style="bold", width=3, justify="right")
    tbl.add_column("Src", style="dim", width=5, no_wrap=True)
    tbl.add_column("MPN", style="cyan", no_wrap=True)
    tbl.add_column("Supplier PN", style="dim", no_wrap=True)
    tbl.add_column("Manufacturer")
    tbl.add_column("Description")

    for i, (sup_name, r) in enumerate(combined_limited, 1):
        src_label = f"[cyan]LCSC[/]" if sup_name.lower() == "lcsc" else f"[yellow]LION[/]"
        tbl.add_row(
            str(i),
            src_label,
            r.mpn,
            r.supplier_pn,
            r.manufacturer or "—",
            r.description[:55] if r.description else "—",
        )

    # Add manual entry option
    manual_num = len(combined_limited) + 1
    tbl.add_row(
        str(manual_num),
        "[dim]—[/]",
        "[dim]Manual price entry[/]",
        "—", "—", "—",
    )
    console.print(tbl)

    # ── Interactive selection ─────────────────────────────────────────────
    total_choices = len(combined_limited) + 1
    choice = IntPrompt.ask(
        f"Select part [{manual_num} = manual price]",
        choices=[str(i) for i in range(1, total_choices + 1)],
        show_choices=False,
    )

    # ── Manual price entry ────────────────────────────────────────────────
    if choice == manual_num:
        console.print("[dim]Enter price (e.g. [bold]600000 IRR[/bold] or [bold]0.50 USD[/bold]):[/]")
        while True:
            raw_price = click.prompt("Price")
            try:
                unit_price, currency = parse_manual_price(raw_price)
                break
            except ValueError as exc:
                console.print(f"[red]{exc}[/]  Try again.")

        line_total = unit_price * Decimal(qty)
        rate_note = (
            f" [dim]({fmt_irr(usd_to_irr(unit_price, rate))})[/]" if currency == "USD"
            else f" [dim]({fmt_usd(irr_to_usd(unit_price, rate))})[/]"
        )
        console.print(
            f"  Quantity [bold]{qty}[/]  ·  "
            f"Unit price [bold green]{fmt_amount(unit_price, currency)}[/]{rate_note}  ·  "
            f"Line total [bold]{fmt_amount(line_total, currency)}[/]"
        )
        if not Confirm.ask("\nAdd to BOM?", default=True):
            console.print("[dim]Aborted.[/]")
            return
        try:
            item = svc.bom_service_ro().add_part_manual(
                version_id=ver.id,
                user_part_name=part_name,
                quantity=qty,
                reference_designator=ref,
                unit_price=unit_price,
                currency=currency,
            )
        except BOMManagerError as exc:
            _die(str(exc))
        console.print(
            f"\n[green]✓[/] Added [bold cyan]{item.user_part_name}[/] × {item.quantity}  "
            f"@ [green]{fmt_amount(item.unit_price, item.currency)}[/]  "
            f"[dim](manual)[/]"
        )
        return

    selected_supplier_name, selected = combined_limited[choice - 1]

    # ── Determine which supplier instance to use ──────────────────────────
    supplier_instance = None
    for s in active:
        if s.name.lower() == selected_supplier_name.lower():
            supplier_instance = s
            break
    if supplier_instance is None:
        _die(f"Supplier {selected_supplier_name!r} not available")

    # ── Fetch full detail ─────────────────────────────────────────────────
    console.print(f"\nFetching detail for [bold]{selected.supplier_pn}[/]...")
    try:
        detail = supplier_instance.get_part(selected.supplier_pn)
    except Exception as exc:
        _die(f"Failed to fetch part detail: {exc}")

    # ── Price breaks table ────────────────────────────────────────────────
    if detail.price_breaks:
        mfr_part = f"  ·  [dim]{detail.manufacturer}[/]" if detail.manufacturer else ""
        pb_tbl = Table(
            title=(
                f"[bold]{detail.mpn}[/]{mfr_part}  ·  "
                f"Stock: [{'green' if detail.stock > 0 else 'red'}]{detail.stock:,}[/]  ·  "
                f"[dim]{detail.currency}[/]"
            ),
            box=box.SIMPLE, show_edge=False, pad_edge=True,
        )
        pb_tbl.add_column("Min Qty", justify="right")
        pb_tbl.add_column("Unit Price", justify="right", style="green")
        pb_tbl.add_column("Converted", justify="right", style="dim")
        for pb in detail.price_breaks:
            native = fmt_amount(pb.unit_price, detail.currency)
            if detail.currency == "IRR":
                converted = fmt_usd(irr_to_usd(pb.unit_price, rate))
            else:
                converted = fmt_irr(usd_to_irr(pb.unit_price, rate))
            pb_tbl.add_row(f"{pb.min_quantity:,}+", native, converted)
        console.print(pb_tbl)

    unit_price = detail.best_unit_price(qty)
    line_total = unit_price * Decimal(qty) if unit_price is not None else None

    console.print(
        f"  Quantity [bold]{qty}[/]  ·  "
        f"Unit price [bold green]{fmt_price(unit_price, detail.currency, rate=rate)}[/]  ·  "
        f"Line total [bold]{fmt_price(line_total, detail.currency, rate=rate)}[/]"
    )

    if not Confirm.ask("\nAdd to BOM?", default=True):
        console.print("[dim]Aborted.[/]")
        return

    # ── Add to BOM ────────────────────────────────────────────────────────
    try:
        item = svc.bom_service_ro().add_part(
            version_id=ver.id,
            user_part_name=part_name,
            quantity=qty,
            reference_designator=ref,
            supplier_pn=selected.supplier_pn,
        )
    except BOMManagerError as exc:
        _die(str(exc))

    # bom_service_ro() doesn't have a supplier; use the selected supplier instance directly
    # to actually fetch and persist the part
    from bom_manager.core.bom_service import BOMService as _BOMService
    from bom_manager.core.models import BOMItem as _BOMItem, PriceBreak as _PB
    from decimal import Decimal as _D

    price_breaks = [_PB(min_quantity=pb.min_quantity, unit_price=pb.unit_price) for pb in detail.price_breaks]
    up = detail.best_unit_price(qty)
    tp = up * _D(qty) if up is not None else None
    item = _BOMItem(
        version_id=ver.id,
        reference_designator=ref,
        user_part_name=part_name,
        matched_mpn=detail.mpn or None,
        supplier=selected_supplier_name,
        supplier_part_number=detail.supplier_pn or None,
        supplier_url=detail.url or None,
        quantity=qty,
        unit_price=up,
        price_breaks=price_breaks,
        total_price=tp,
        currency=detail.currency,
    )
    item = svc.storage().add_item(item)

    console.print(
        f"\n[green]✓[/] Added [bold cyan]{item.matched_mpn or item.user_part_name}[/] × {item.quantity}  "
        f"@ [green]{fmt_price(item.unit_price, item.currency, rate=rate)}[/]  "
        f"[dim]({item.supplier_part_number or 'manual'})[/]"
    )


# ── bom list ─────────────────────────────────────────────────────────────────

@bom.command("list")
@click.argument("project_name")
@click.argument("version_name")
@click.pass_obj
def bom_list(svc: _Services, project_name: str, version_name: str) -> None:
    """Show the full BOM with prices and totals."""
    project, ver = _resolve_version(svc, project_name, version_name)

    try:
        summary = svc.bom_service().get_bom(ver.id)
    except VersionNotFoundError as exc:
        _die(str(exc))

    if not summary.items:
        console.print(
            f"[dim]BOM for [bold]{project.name}[/bold] / "
            f"[bold]{ver.version_name}[/bold] is empty.  "
            f"Use [bold]bom add[/bold] to add parts.[/]"
        )
        return

    rate = svc.settings_service().get_rate()

    tbl = Table(
        title=f"[bold cyan]{project.name}[/]  /  [bold]{ver.version_name}[/]",
        box=box.ROUNDED, show_lines=True, pad_edge=True,
    )
    tbl.add_column("Ref", style="dim", no_wrap=True)
    tbl.add_column("Part Name")
    tbl.add_column("MPN", style="cyan", no_wrap=True)
    tbl.add_column("Supplier", style="dim", no_wrap=True)
    tbl.add_column("Supplier PN", style="dim", no_wrap=True)
    tbl.add_column("Qty", justify="right")
    tbl.add_column("Unit Price", justify="right", style="green")
    tbl.add_column("Line Total", justify="right", style="bold")
    tbl.add_column("Item ID", style="dim")

    for item in summary.items:
        unit_price = item.effective_unit_price()
        total = item.calculate_total()
        pn_display = item.supplier_part_number or "—"
        if item.alt_sources:
            pn_display += f" [dim](+{len(item.alt_sources)} alt)[/]"
        tbl.add_row(
            item.reference_designator,
            item.user_part_name,
            item.matched_mpn or "—",
            item.supplier or "manual",
            pn_display,
            str(item.quantity),
            fmt_price(unit_price, item.currency, rate=rate) if unit_price else "—",
            fmt_price(total, item.currency, rate=rate) if total else "—",
            str(item.id)[:8],
        )

    console.print(tbl)

    # ── Dual-currency totals ──────────────────────────────────────────────
    usd_total = Decimal("0")
    irr_total = Decimal("0")
    for item in summary.items:
        t = item.calculate_total()
        if t is None:
            continue
        if item.currency == "IRR":
            irr_total += t
            usd_total += irr_to_usd(t, rate)
        else:
            usd_total += t
            irr_total += usd_to_irr(t, rate)

    console.print(
        f"  {summary.item_count} item{'s' if summary.item_count != 1 else ''}  ·  "
        f"Total: [bold green]{fmt_usd(usd_total)}[/]  [dim]/[/]  [bold yellow]{fmt_irr(irr_total)}[/]  "
        f"[dim](rate: 1 USD = {rate:,.0f} IRR)[/]"
    )


# ── bom remove ───────────────────────────────────────────────────────────────

@bom.command("remove")
@click.argument("project_name")
@click.argument("version_name")
@click.argument("item_id")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_obj
def bom_remove(
    svc: _Services,
    project_name: str,
    version_name: str,
    item_id: str,
    yes: bool,
) -> None:
    """Remove a part from a BOM version (use item ID prefix from bom list)."""
    _, ver = _resolve_version(svc, project_name, version_name)
    item = _resolve_item(svc, ver.id, item_id)

    if not yes:
        if not Confirm.ask(
            f"Remove [bold]{item.matched_mpn or item.user_part_name}[/] "
            f"(ref [bold]{item.reference_designator}[/]) from BOM?",
            default=False,
        ):
            console.print("[dim]Aborted.[/]")
            return

    svc.bom_service().remove_part(ver.id, item.id)
    console.print(
        f"[green]✓[/] Removed [bold]{item.matched_mpn or item.user_part_name}[/] "
        f"[dim]({str(item.id)[:8]})[/]"
    )


# ── bom update-qty ───────────────────────────────────────────────────────────

@bom.command("update-qty")
@click.argument("project_name")
@click.argument("version_name")
@click.argument("item_id")
@click.argument("new_qty", type=int)
@click.pass_obj
def bom_update_qty(
    svc: _Services,
    project_name: str,
    version_name: str,
    item_id: str,
    new_qty: int,
) -> None:
    """Update the quantity of a BOM item (recalculates price tier automatically)."""
    if new_qty < 1:
        _die("new_qty must be >= 1")

    _, ver = _resolve_version(svc, project_name, version_name)
    item = _resolve_item(svc, ver.id, item_id)

    old_qty = item.quantity
    old_price = item.effective_unit_price()

    try:
        updated = svc.bom_service().update_quantity(ver.id, item.id, new_qty)
    except BOMManagerError as exc:
        _die(str(exc))

    new_price = updated.effective_unit_price()
    tier_note = ""
    if old_price is not None and new_price is not None and old_price != new_price:
        direction = "[green]↓[/]" if new_price < old_price else "[yellow]↑[/]"
        tier_note = f"  {direction} price tier changed"

    console.print(
        f"[green]✓[/] [bold]{updated.matched_mpn or updated.user_part_name}[/]  "
        f"qty [dim]{old_qty}[/] → [bold]{new_qty}[/]  "
        f"@ [green]{_fmt_price(new_price)}[/]  "
        f"line total [bold]{_fmt_price(updated.total_price)}[/]"
        f"{tier_note}"
    )


# ── bom export ───────────────────────────────────────────────────────────────

@bom.command("export")
@click.argument("project_name")
@click.argument("version_name")
@click.option(
    "--format", "fmt",
    default="csv",
    type=click.Choice(["csv", "xlsx"], case_sensitive=False),
    show_default=True,
    help="Output format",
)
@click.option("--output-dir", "-o", default=None, metavar="DIR", help="Output directory")
@click.pass_obj
def bom_export(
    svc: _Services,
    project_name: str,
    version_name: str,
    fmt: str,
    output_dir: Optional[str],
) -> None:
    """Export the BOM to a file."""
    project, ver = _resolve_version(svc, project_name, version_name)

    out_dir = Path(output_dir) if output_dir else None
    stem = f"{project.name}_{ver.version_name}".replace(" ", "_")

    try:
        path = svc.bom_service().export_bom(
            ver.id, format=fmt, output_dir=out_dir, filename=stem
        )
    except BOMManagerError as exc:
        _die(str(exc))

    console.print(
        f"[green]✓[/] Exported [bold]{project.name}[/] / [bold]{ver.version_name}[/]  "
        f"→  [cyan]{path}[/]"
    )


# ── bom cost ─────────────────────────────────────────────────────────────────

@bom.command("cost")
@click.argument("project_name")
@click.argument("version_name")
@click.option("--boards", "-n", default=100, type=int, show_default=True,
              help="Number of boards to compare against 1-board pricing")
@click.pass_obj
def bom_cost(
    svc: _Services,
    project_name: str,
    version_name: str,
    boards: int,
) -> None:
    """Compare per-board cost: 1 board vs N boards (shows bulk price tier savings)."""
    if boards < 2:
        _die("--boards must be >= 2 (use bom list for single-board cost)")

    project, ver = _resolve_version(svc, project_name, version_name)

    try:
        summary = svc.bom_service().get_bom(ver.id)
    except VersionNotFoundError as exc:
        _die(str(exc))

    if not summary.items:
        console.print("[dim]BOM is empty.[/]")
        return

    # ── Per-item calculations ─────────────────────────────────────────────
    # Each row holds: (item, unit_1, total_1, unit_n, total_n, line_n)
    rows: list[tuple] = []
    pb_total_1 = Decimal("0")   # 1-board grand total
    pb_total_n = Decimal("0")   # N-board grand total

    for item in summary.items:
        qty_per_board = item.quantity

        # 1 board — unit price for qty_per_board units
        unit_1 = _best_price_at(item.price_breaks, qty_per_board)
        total_1 = unit_1 * Decimal(qty_per_board) if unit_1 is not None else None

        # N boards — unit price for qty_per_board × N units (hits better tiers)
        qty_n = qty_per_board * boards
        unit_n = _best_price_at(item.price_breaks, qty_n)
        line_n = unit_n * Decimal(qty_n) if unit_n is not None else None
        per_board_n = line_n / Decimal(boards) if line_n is not None else None

        if total_1 is not None:
            pb_total_1 += total_1
        if per_board_n is not None:
            pb_total_n += per_board_n

        rows.append((item, qty_per_board, unit_1, total_1, qty_n, unit_n, line_n, per_board_n))

    # ── Comparison table ──────────────────────────────────────────────────
    tbl = Table(
        title=(
            f"[bold cyan]{project.name}[/]  /  [bold]{ver.version_name}[/]  ·  "
            f"Cost comparison: [bold]1[/] vs [bold]{boards:,}[/] boards"
        ),
        box=box.ROUNDED,
        show_lines=True,
        pad_edge=True,
    )

    # Columns: part info | ── 1 board ── | ── N boards ──── | saving
    tbl.add_column("Ref",     style="dim",   no_wrap=True)
    tbl.add_column("MPN",     style="cyan",  no_wrap=True)
    # 1-board group
    tbl.add_column("Qty",     justify="right", header_style="white")
    tbl.add_column("Unit @1", justify="right", style="white",        header_style="white")
    tbl.add_column("Total @1",justify="right", style="white",        header_style="white")
    # N-board group
    tbl.add_column(f"Qty×{boards:,}", justify="right", header_style="bold yellow")
    tbl.add_column(f"Unit @{boards:,}", justify="right", style="green", header_style="bold yellow")
    tbl.add_column(f"Total @{boards:,}", justify="right", style="green", header_style="bold yellow")
    # Saving
    tbl.add_column("Save/ea", justify="right", style="bold green", header_style="bold green")

    for (item, qty_per_board, unit_1, total_1, qty_n, unit_n, line_n, per_board_n) in rows:
        # Per-unit saving when buying for N boards vs 1 board
        if unit_1 is not None and unit_n is not None and unit_n < unit_1:
            saving = unit_1 - unit_n
            pct = int(saving / unit_1 * 100)
            saving_str = f"[green]-${saving:.4f}[/] [dim]({pct}%)[/]"
        elif unit_1 is not None and unit_n is not None:
            saving_str = "[dim]—[/]"
        else:
            saving_str = "[dim]?[/]"

        tbl.add_row(
            item.reference_designator,
            item.matched_mpn or item.user_part_name,
            # 1 board
            str(qty_per_board),
            _fmt_price(unit_1),
            _fmt_price(total_1),
            # N boards
            f"{qty_n:,}",
            _fmt_price(unit_n),
            _fmt_price(line_n),
            # saving
            saving_str,
        )

    console.print(tbl)

    # ── Summary section ───────────────────────────────────────────────────
    grand_total_n = pb_total_n * Decimal(boards)

    if pb_total_1 > 0:
        saved_per_board = pb_total_1 - pb_total_n
        pct_saved = int(saved_per_board / pb_total_1 * 100)
        cheaper = saved_per_board > 0
    else:
        saved_per_board = Decimal("0")
        pct_saved = 0
        cheaper = False

    console.print()
    summary_tbl = Table(box=box.SIMPLE, show_header=False, pad_edge=True)
    summary_tbl.add_column("label",  style="dim",        min_width=28)
    summary_tbl.add_column("1 board",  justify="right",  style="white",       min_width=12)
    summary_tbl.add_column(f"{boards:,} boards", justify="right", style="bold green", min_width=14)

    summary_tbl.add_row(
        "Per-board cost",
        f"${pb_total_1:.4f}",
        f"${pb_total_n:.4f}",
    )
    summary_tbl.add_row(
        f"Total ({boards:,} boards)",
        f"${pb_total_1 * boards:.4f}",
        f"${grand_total_n:.4f}",
    )

    console.print(summary_tbl)

    if cheaper:
        console.print(
            f"  [bold green]You save ${saved_per_board:.4f}/board  ({pct_saved}% cheaper)[/]"
            f"  when building [bold]{boards:,}[/] boards instead of 1.\n"
        )
    else:
        console.print(
            f"  [dim]No price tier improvement at {boards:,} boards for this BOM.[/]\n"
        )


# ── bom diff ──────────────────────────────────────────────────────────────────

@bom.command("diff")
@click.argument("project_name")
@click.argument("version_a")
@click.argument("version_b")
@click.pass_obj
def bom_diff(
    svc: _Services,
    project_name: str,
    version_a: str,
    version_b: str,
) -> None:
    """Show what changed between two BOM versions.

    \b
    Color coding:
      green  — part added in VERSION_B
      red    — part removed (was in VERSION_A)
      yellow — part changed (quantity, price, or reference)
    """
    project, ver_a = _resolve_version(svc, project_name, version_a)
    _, ver_b = _resolve_version(svc, project_name, version_b)

    try:
        diff = svc.bom_service_ro().diff_versions(ver_a.id, ver_b.id)
    except BOMManagerError as exc:
        _die(str(exc))

    if diff.is_identical:
        console.print(
            f"[dim]Versions [bold]{version_a}[/] and [bold]{version_b}[/] "
            f"are identical — no differences found.[/]"
        )
        return

    tbl = Table(
        title=(
            f"[bold cyan]{project.name}[/]  ·  "
            f"[bold]{version_a}[/] → [bold]{version_b}[/]"
        ),
        box=box.ROUNDED,
        show_lines=True,
        pad_edge=True,
    )
    tbl.add_column("",       width=2, no_wrap=True)          # status icon
    tbl.add_column("Ref",    style="dim",   no_wrap=True)
    tbl.add_column("MPN",    style="cyan",  no_wrap=True)
    tbl.add_column("LCSC #", style="dim",   no_wrap=True)
    tbl.add_column("Qty",    justify="right")
    tbl.add_column("Unit Price", justify="right")
    tbl.add_column("Changes")

    def _row_added(item):
        tbl.add_row(
            "[bold green]+[/]",
            item.reference_designator,
            item.matched_mpn or item.user_part_name,
            item.supplier_part_number or "—",
            str(item.quantity),
            _fmt_price(item.effective_unit_price()),
            "[green]added[/]",
            style="green",
        )

    def _row_removed(item):
        tbl.add_row(
            "[bold red]-[/]",
            item.reference_designator,
            item.matched_mpn or item.user_part_name,
            item.supplier_part_number or "—",
            str(item.quantity),
            _fmt_price(item.effective_unit_price()),
            "[red]removed[/]",
            style="red",
        )

    def _row_changed(old, new):
        changes = []
        if old.quantity != new.quantity:
            changes.append(f"qty {old.quantity}→{new.quantity}")
        if old.reference_designator != new.reference_designator:
            changes.append(f"ref {old.reference_designator}→{new.reference_designator}")
        if old.user_part_name != new.user_part_name:
            changes.append(f"name changed")
        price_old = old.effective_unit_price()
        price_new = new.effective_unit_price()
        if price_old != price_new:
            changes.append(f"price {_fmt_price(price_old)}→{_fmt_price(price_new)}")

        tbl.add_row(
            "[bold yellow]~[/]",
            new.reference_designator,
            new.matched_mpn or new.user_part_name,
            new.supplier_part_number or "—",
            str(new.quantity),
            _fmt_price(new.effective_unit_price()),
            "[yellow]" + ", ".join(changes) + "[/]",
            style="yellow",
        )

    for item in diff.removed:
        _row_removed(item)
    for item in diff.added:
        _row_added(item)
    for old, new in diff.changed:
        _row_changed(old, new)

    console.print(tbl)

    parts = []
    if diff.added:
        parts.append(f"[green]{len(diff.added)} added[/]")
    if diff.removed:
        parts.append(f"[red]{len(diff.removed)} removed[/]")
    if diff.changed:
        parts.append(f"[yellow]{len(diff.changed)} changed[/]")
    console.print("  " + "  ·  ".join(parts))


# ── bom sources ──────────────────────────────────────────────────────────────

@bom.command("sources")
@click.argument("project_name")
@click.argument("version_name")
@click.argument("item_id")
@click.pass_obj
def bom_sources(
    svc: _Services,
    project_name: str,
    version_name: str,
    item_id: str,
) -> None:
    """Show all supplier sources for a BOM item (primary + alternatives)."""
    _, ver = _resolve_version(svc, project_name, version_name)
    item = _resolve_item(svc, ver.id, item_id)
    rate = svc.settings_service().get_rate()

    tbl = Table(
        title=(
            f"Sources — [bold cyan]{item.matched_mpn or item.user_part_name}[/]  "
            f"[dim](id={str(item.id)[:8]})[/]"
        ),
        box=box.ROUNDED,
        show_lines=False,
        pad_edge=True,
    )
    tbl.add_column("", style="dim", width=10, no_wrap=True)
    tbl.add_column("Supplier", style="cyan", no_wrap=True)
    tbl.add_column("Supplier PN", style="dim", no_wrap=True)
    tbl.add_column("MPN", style="cyan", no_wrap=True)
    tbl.add_column("Unit Price", justify="right", style="green")

    # Primary source
    primary_price = item.effective_unit_price()
    tbl.add_row(
        "[bold green][primary][/]",
        item.supplier or "manual",
        item.supplier_part_number or "—",
        item.matched_mpn or "—",
        fmt_price(primary_price, item.currency, rate=rate) if primary_price else "—",
    )

    for i, src in enumerate(item.alt_sources, 1):
        alt_price = (
            _best_price_at(src.price_breaks, item.quantity)
            if src.price_breaks
            else src.unit_price
        )
        tbl.add_row(
            f"[dim][alt {i}][/]",
            src.supplier,
            src.supplier_part_number or "—",
            src.matched_mpn or "—",
            fmt_price(alt_price, src.currency, rate=rate) if alt_price else "—",
        )

    console.print(tbl)
    if item.alt_sources:
        console.print(
            f"[dim]Use [bold]bom use-source {project_name} {version_name} "
            f"{str(item.id)[:8]} <N>[/bold] to activate an alt source (N=1-based).[/]"
        )


# ── bom add-source ────────────────────────────────────────────────────────────

@bom.command("add-source")
@click.argument("project_name")
@click.argument("version_name")
@click.argument("item_id")
@click.option("--query", "-q", default=None, metavar="TEXT",
              help="Search query (defaults to item's part name)")
@click.option("--manual", "-m", default=None, metavar="PRICE",
              help="Manual price (e.g. '600000 IRR' or '0.50 USD')")
@click.pass_obj
def bom_add_source(
    svc: _Services,
    project_name: str,
    version_name: str,
    item_id: str,
    query: Optional[str],
    manual: Optional[str],
) -> None:
    """Add an alternative supplier source to an existing BOM item."""
    _, ver = _resolve_version(svc, project_name, version_name)
    item = _resolve_item(svc, ver.id, item_id)
    rate = svc.settings_service().get_rate()

    if manual is not None:
        # Manual price source
        try:
            unit_price, currency = parse_manual_price(manual)
        except ValueError as exc:
            _die(str(exc))

        source = SupplierSource(
            supplier="manual",
            unit_price=unit_price,
            currency=currency,
        )
        svc.bom_service_ro().add_source_to_item(ver.id, item.id, source)
        console.print(
            f"[green]✓[/] Added manual alt source to "
            f"[bold cyan]{item.matched_mpn or item.user_part_name}[/]  "
            f"@ [green]{fmt_amount(unit_price, currency)}[/]"
        )
        return

    # Supplier search
    search_query = query or item.user_part_name
    active = svc.get_active_suppliers()
    if not active:
        _die("No active suppliers configured.  Use 'settings suppliers' to enable one.")

    supplier_labels = " + ".join(s.name.upper() for s in active)
    console.print(
        f'\nSearching [bold cyan]{supplier_labels}[/] for [bold yellow]"{search_query}"[/bold yellow]...'
    )

    from bom_manager.core.bom_service import BOMService
    combined, search_failures = BOMService.search_parts_all(search_query, active)

    for sup_name, err_msg in search_failures:
        console.print(f"[yellow]⚠ {sup_name.upper()} search failed:[/] [dim]{err_msg}[/]")

    from collections import defaultdict
    per_supplier: dict[str, list] = defaultdict(list)
    for sup_name, r in combined:
        if len(per_supplier[sup_name]) < 5:
            per_supplier[sup_name].append((sup_name, r))
    combined_limited = [entry for entries in per_supplier.values() for entry in entries]

    if not combined_limited:
        console.print("[yellow]No results found.[/]")
        return

    tbl = Table(box=box.ROUNDED, show_lines=False, pad_edge=True, highlight=True)
    tbl.add_column("#", style="bold", width=3, justify="right")
    tbl.add_column("Src", style="dim", width=5, no_wrap=True)
    tbl.add_column("MPN", style="cyan", no_wrap=True)
    tbl.add_column("Supplier PN", style="dim", no_wrap=True)
    tbl.add_column("Manufacturer")
    tbl.add_column("Description")

    for i, (sup_name, r) in enumerate(combined_limited, 1):
        src_label = "[cyan]LCSC[/]" if sup_name.lower() == "lcsc" else "[yellow]LION[/]"
        tbl.add_row(
            str(i), src_label, r.mpn, r.supplier_pn,
            r.manufacturer or "—",
            r.description[:55] if r.description else "—",
        )
    console.print(tbl)

    choice = IntPrompt.ask(
        f"Select source to add [1-{len(combined_limited)}]",
        choices=[str(i) for i in range(1, len(combined_limited) + 1)],
        show_choices=False,
    )

    selected_supplier_name, selected = combined_limited[choice - 1]
    supplier_instance = next(
        (s for s in active if s.name.lower() == selected_supplier_name.lower()), None
    )
    if supplier_instance is None:
        _die(f"Supplier {selected_supplier_name!r} not available")

    console.print(f"\nFetching detail for [bold]{selected.supplier_pn}[/]...")
    try:
        detail = supplier_instance.get_part(selected.supplier_pn)
    except Exception as exc:
        _die(f"Failed to fetch part detail: {exc}")

    if detail.price_breaks:
        mfr_part = f"  ·  [dim]{detail.manufacturer}[/]" if detail.manufacturer else ""
        pb_tbl = Table(
            title=(
                f"[bold]{detail.mpn}[/]{mfr_part}  ·  "
                f"Stock: [{'green' if detail.stock > 0 else 'red'}]{detail.stock:,}[/]  ·  "
                f"[dim]{detail.currency}[/]"
            ),
            box=box.SIMPLE, show_edge=False, pad_edge=True,
        )
        pb_tbl.add_column("Min Qty", justify="right")
        pb_tbl.add_column("Unit Price", justify="right", style="green")
        pb_tbl.add_column("Converted", justify="right", style="dim")
        for pb in detail.price_breaks:
            native = fmt_amount(pb.unit_price, detail.currency)
            converted = (
                fmt_usd(irr_to_usd(pb.unit_price, rate))
                if detail.currency == "IRR"
                else fmt_irr(usd_to_irr(pb.unit_price, rate))
            )
            pb_tbl.add_row(f"{pb.min_quantity:,}+", native, converted)
        console.print(pb_tbl)

    unit_price = detail.best_unit_price(item.quantity)
    line_total = unit_price * Decimal(item.quantity) if unit_price is not None else None
    console.print(
        f"  Quantity [bold]{item.quantity}[/]  ·  "
        f"Unit price [bold green]{fmt_price(unit_price, detail.currency, rate=rate)}[/]  ·  "
        f"Line total [bold]{fmt_price(line_total, detail.currency, rate=rate)}[/]"
    )

    if not Confirm.ask("\nAdd as alt source?", default=True):
        console.print("[dim]Aborted.[/]")
        return

    from bom_manager.core.models import PriceBreak as _PB
    price_breaks = [_PB(min_quantity=pb.min_quantity, unit_price=pb.unit_price) for pb in detail.price_breaks]
    source = SupplierSource(
        supplier=selected_supplier_name,
        supplier_part_number=detail.supplier_pn or None,
        supplier_url=detail.url or None,
        matched_mpn=detail.mpn or None,
        unit_price=unit_price,
        price_breaks=price_breaks,
        currency=detail.currency,
    )
    svc.bom_service_ro().add_source_to_item(ver.id, item.id, source)
    console.print(
        f"\n[green]✓[/] Added [bold cyan]{detail.mpn or selected.supplier_pn}[/] "
        f"as alt source  "
        f"@ [green]{fmt_price(unit_price, detail.currency, rate=rate)}[/]  "
        f"[dim]({selected_supplier_name})[/]"
    )


# ── bom use-source ────────────────────────────────────────────────────────────

@bom.command("use-source")
@click.argument("project_name")
@click.argument("version_name")
@click.argument("item_id")
@click.argument("n", type=int)
@click.pass_obj
def bom_use_source(
    svc: _Services,
    project_name: str,
    version_name: str,
    item_id: str,
    n: int,
) -> None:
    """Promote alt source N (1-based) to primary for a BOM item."""
    _, ver = _resolve_version(svc, project_name, version_name)
    item = _resolve_item(svc, ver.id, item_id)
    rate = svc.settings_service().get_rate()

    if not item.alt_sources:
        _die("This item has no alternative sources.  Use 'bom add-source' first.")

    if not (1 <= n <= len(item.alt_sources)):
        _die(
            f"N must be between 1 and {len(item.alt_sources)} "
            f"(use 'bom sources' to list available sources)"
        )

    try:
        updated = svc.bom_service_ro().use_source(ver.id, item.id, n - 1)
    except (ValueError, Exception) as exc:
        _die(str(exc))

    new_price = updated.effective_unit_price()
    console.print(
        f"[green]✓[/] Now using [bold cyan]{updated.supplier}[/] "
        f"([bold]{updated.supplier_part_number or '—'}[/]) as primary source  "
        f"@ [green]{fmt_price(new_price, updated.currency, rate=rate) if new_price else '—'}[/]"
    )


# ---------------------------------------------------------------------------
# settings commands
# ---------------------------------------------------------------------------

@cli.group()
def settings() -> None:
    """View and configure BOM Manager settings (exchange rate, suppliers)."""


@settings.command("show")
@click.pass_obj
def settings_show(svc: _Services) -> None:
    """Show all current settings."""
    svc_settings = svc.settings_service()
    all_s = svc_settings.all_settings()
    rate = svc_settings.get_rate()
    fetched = svc_settings.rate_last_fetched()
    active = svc_settings.get_active_suppliers()

    tbl = Table(box=box.ROUNDED, show_lines=False, pad_edge=True)
    tbl.add_column("Setting", style="bold cyan", no_wrap=True)
    tbl.add_column("Value")
    tbl.add_column("Note", style="dim")

    tbl.add_row(
        "usd_to_irr_rate",
        f"{rate:,.0f}",
        f"fetched {fetched.strftime('%Y-%m-%d %H:%M UTC') if fetched else 'never (manual)'}",
    )
    tbl.add_row(
        "active_suppliers",
        ", ".join(active),
        "lcsc / lion / all",
    )
    console.print(tbl)
    console.print(
        "[dim]⚠  The auto-fetched rate is the official interbank rate.  "
        "Lion Electronic uses the market rate — verify with [bold]settings rate <value>[/bold].[/]"
    )


@settings.command("rate")
@click.argument("value", required=False, type=float)
@click.pass_obj
def settings_rate(svc: _Services, value: Optional[float]) -> None:
    """Show or set the USD→IRR exchange rate.

    Run without VALUE to show the current rate.
    Provide VALUE (e.g. 650000) to update it.
    """
    svc_settings = svc.settings_service()
    if value is None:
        rate = svc_settings.get_rate()
        fetched = svc_settings.rate_last_fetched()
        console.print(
            f"Current rate: [bold green]1 USD = {rate:,.0f} IRR[/]  "
            f"[dim](last fetched: {fetched.strftime('%Y-%m-%d') if fetched else 'never'})[/]"
        )
    else:
        rate = Decimal(str(value))
        svc_settings.set_rate(rate)
        console.print(f"[green]✓[/] Rate set: [bold]1 USD = {rate:,.0f} IRR[/]")


@settings.command("fetch-rate")
@click.pass_obj
def settings_fetch_rate(svc: _Services) -> None:
    """Auto-fetch the USD→IRR rate from open.er-api.com.

    \b
    WARNING: This returns the official interbank rate.
    In Iran, the market (bazaar) rate used by Lion Electronic may differ
    significantly. Always verify and override with 'settings rate <value>'
    if needed.
    """
    console.print("Fetching live USD→IRR rate from open.er-api.com...")
    rate = svc.settings_service().fetch_live_rate()
    if rate is None:
        err_console.print("[bold red]Error:[/] Failed to fetch rate. Check your internet connection.")
    else:
        console.print(f"[green]✓[/] Live rate: [bold]1 USD = {rate:,.0f} IRR[/]")
        console.print(
            "[yellow]⚠  This is the official interbank rate.  "
            "If Lion prices use the market rate, override with "
            "[bold]settings rate <value>[/bold].[/]"
        )


@settings.command("suppliers")
@click.argument("value", required=False, type=click.Choice(["lcsc", "lion", "all"], case_sensitive=False))
@click.pass_obj
def settings_suppliers(svc: _Services, value: Optional[str]) -> None:
    """Show or set active suppliers (lcsc / lion / all).

    Run without VALUE to show current active suppliers.
    """
    svc_settings = svc.settings_service()
    if value is None:
        active = svc_settings.get_active_suppliers()
        console.print(f"Active suppliers: [bold cyan]{', '.join(active)}[/]")
    else:
        svc_settings.set_active_suppliers(value.lower())
        active = svc_settings.get_active_suppliers()
        console.print(f"[green]✓[/] Active suppliers: [bold cyan]{', '.join(active)}[/]")
