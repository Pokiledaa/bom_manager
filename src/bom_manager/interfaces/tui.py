"""Textual TUI for BOM Manager.

Layout
------
┌─ BOM Manager v0.1.0 ─────────────────────────────────────────────┐
│ PROJECTS                                                           │
│  ▶ MyProject                                                      │
│     ├─ v1.0  · 12 items · $45.23                                 │
│     └─ v2.0  · 8 items  · $32.00                                 │
├────────────────────────────────────────────────────────────────────┤
│ > project list                                                     │
│ ╭─────────────────────────────────────────╮                       │
│ │ Name          │ Versions │ Created      │                       │
│ │ MyProject     │    2     │ 2024-01-15   │                       │
│ ╰─────────────────────────────────────────╯                       │
├────────────────────────────────────────────────────────────────────┤
│ > _                                                                │
└────────────────────────────────────────────────────────────────────┘

Usage
-----
  bom          → launches TUI
  bom --cli    → delegates to the original Click CLI
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable, Literal, Optional
from uuid import UUID

from rich import box
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Footer,
    Header,
    Input,
    RichLog,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

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
    VersionNotFoundError,
)
from bom_manager.core.models import BOMItem, BOMSummary, Project, ProjectVersion, SupplierSource

# Re-use the service container from cli.py unchanged
from bom_manager.interfaces.cli import _Services

# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────

VERSION = "0.1.0"

WELCOME = (
    f"[bold cyan]BOM Manager[/] v{VERSION}  —  Terminal User Interface\n"
    "[dim]Type commands at the bottom. Use [bold]help[/bold] to see all commands.\n"
    "Use [bold]↑ ↓[/bold] for command history.  "
    "Press [bold]Ctrl+C[/bold] to quit.[/]\n"
)


@dataclass
class TreeNodeData:
    kind: Literal["project", "version"]
    project_id: UUID
    project_name: str
    version_id: Optional[UUID] = None
    version_name: Optional[str] = None
    item_count: int = 0
    total_cost: Optional[Decimal] = None


@dataclass
class PendingInteraction:
    """Holds state between multi-step commands (e.g. bom add search → pick → confirm)."""
    kind: Literal[
        "pick_part_multi",
        "manual_price",
        "confirm_manual_add",
        "confirm_add",
        "confirm_delete",
        "confirm_version_delete",
    ]
    data: dict = field(default_factory=dict)
    prompt: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Custom widgets
# ─────────────────────────────────────────────────────────────────────────────

class CommandInput(Input):
    """Input widget with arrow-key command history."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._history_idx: int = -1
        self._draft: str = ""

    def on_key(self, event) -> None:  # type: ignore[override]
        if event.key == "up":
            event.stop()
            if not self._history:
                return
            if self._history_idx == -1:
                self._draft = self.value
                self._history_idx = len(self._history) - 1
            elif self._history_idx > 0:
                self._history_idx -= 1
            self.value = self._history[self._history_idx]
            self.cursor_position = len(self.value)

        elif event.key == "down":
            event.stop()
            if self._history_idx == -1:
                return
            if self._history_idx < len(self._history) - 1:
                self._history_idx += 1
                self.value = self._history[self._history_idx]
            else:
                self._history_idx = -1
                self.value = self._draft
            self.cursor_position = len(self.value)

    def push_history(self, cmd: str) -> None:
        if cmd and (not self._history or self._history[-1] != cmd):
            self._history.append(cmd)
        self._history_idx = -1
        self._draft = ""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers shared by command handlers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_price(price: Optional[Decimal], *, dash: str = "—") -> str:
    return f"${price:.4f}" if price is not None else dash


def _best_price_at(price_breaks, quantity: int) -> Optional[Decimal]:
    if not price_breaks:
        return None
    eligible = [pb for pb in price_breaks if pb.min_quantity <= quantity]
    pool = eligible if eligible else price_breaks
    return min(pool, key=lambda pb: pb.unit_price).unit_price


def _resolve_project(svc: _Services, name: str) -> Project:
    return svc.project_service().get_project(name)


def _resolve_version(
    svc: _Services, project_name: str, version_name: str
) -> tuple[Project, ProjectVersion]:
    project = _resolve_project(svc, project_name)
    versions = svc.project_service().list_versions(project.id)
    version = next((v for v in versions if v.version_name == version_name), None)
    if version is None:
        raise VersionNotFoundError(
            f"Version {version_name!r} not found in project {project_name!r}"
        )
    return project, version


def _resolve_item(svc: _Services, version_id: UUID, prefix: str) -> BOMItem:
    items = svc.storage().list_items_by_version(version_id)
    matched = [i for i in items if str(i.id).startswith(prefix)]
    if not matched:
        raise ItemNotFoundError(f"Item {prefix!r} not found in this version")
    if len(matched) > 1:
        raise ItemNotFoundError(
            f"Prefix {prefix!r} matches {len(matched)} items — use more characters"
        )
    return matched[0]


def _format_version_label(
    version_name: str, item_count: int, total_cost: Optional[Decimal]
) -> str:
    cost_str = f"${total_cost:.2f}" if total_cost else "—"
    return f"[bold]{version_name}[/]  [dim]· {item_count} item{'s' if item_count != 1 else ''} · {cost_str}[/]"


# ─────────────────────────────────────────────────────────────────────────────
# Command parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_command(raw: str) -> tuple[list[str], dict[str, str]]:
    """
    Tokenise ``raw`` using shell quoting rules.

    Returns (positional_args, flags_dict).

    Example
    -------
    ``"bom add proj v1 ESP32 --qty 5 --ref U1"``
    → ``(["bom", "add", "proj", "v1", "ESP32"], {"qty": "5", "ref": "U1"})``
    """
    try:
        tokens = shlex.split(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Parse error: {exc}") from exc

    positionals: list[str] = []
    flags: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:]
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                flags[key] = tokens[i + 1]
                i += 2
            else:
                flags[key] = "true"
                i += 1
        elif tok.startswith("-") and len(tok) == 2:
            # short flags: -y, -q 5
            key = tok[1:]
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                flags[key] = tokens[i + 1]
                i += 2
            else:
                flags[key] = "true"
                i += 1
        else:
            positionals.append(tok)
            i += 1

    return positionals, flags


# ─────────────────────────────────────────────────────────────────────────────
# Command handlers
# Each receives: args (positionals after the verb), flags, svc, print_fn, err_fn, refresh_fn
# and returns Optional[PendingInteraction] to trigger a multi-step flow.
# ─────────────────────────────────────────────────────────────────────────────

PrintFn = Callable[[object], None]   # accepts Rich renderables or str
ErrFn = Callable[[str], None]
RefreshFn = Callable[[], None]


# ── project commands ──────────────────────────────────────────────────────────

def _cmd_project_create(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if not args:
        err_fn("Usage: project create <name> [--description TEXT]")
        return None
    name = args[0]
    description = flags.get("description") or flags.get("d")
    p = svc.project_service().create_project(name, description)
    print_fn(
        f"[green]✓[/] Created project [bold cyan]{p.name}[/]  [dim]id={str(p.id)[:8]}[/]"
    )
    refresh_fn()
    return None


def _cmd_project_list(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    projects = svc.project_service().list_projects()
    if not projects:
        print_fn("[dim]No projects yet.  Use [bold]project create <name>[/bold] to get started.[/]")
        return None

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
    print_fn(tbl)
    return None


def _cmd_project_delete(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if not args:
        err_fn("Usage: project delete <name> [--yes]")
        return None
    name = args[0]
    yes = flags.get("yes") == "true" or flags.get("y") == "true"
    try:
        project = _resolve_project(svc, name)
    except ProjectNotFoundError:
        err_fn(f"Project {name!r} not found")
        return None

    if yes:
        svc.project_service().delete_project(project.id)
        print_fn(f"[green]✓[/] Deleted project [bold]{project.name}[/]")
        refresh_fn()
        return None

    print_fn(
        f"[yellow]Delete project [bold]{project.name}[/] and [bold red]all[/] its data?[/]"
    )
    return PendingInteraction(
        kind="confirm_delete",
        data={"project_id": project.id, "project_name": project.name},
        prompt="Confirm delete? [y/n]: ",
    )


# ── version commands ──────────────────────────────────────────────────────────

def _cmd_version_create(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if len(args) < 2:
        err_fn("Usage: version create <project> <version> [--notes TEXT]")
        return None
    project_name, version_name = args[0], args[1]
    notes = flags.get("notes") or flags.get("n")
    try:
        project = _resolve_project(svc, project_name)
    except ProjectNotFoundError:
        err_fn(f"Project {project_name!r} not found")
        return None
    v = svc.project_service().create_version(project.id, version_name, notes)
    print_fn(
        f"[green]✓[/] Created version [bold]{v.version_name}[/] "
        f"for project [bold cyan]{project.name}[/]  [dim]id={str(v.id)[:8]}[/]"
    )
    refresh_fn()
    return None


def _cmd_version_list(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if not args:
        err_fn("Usage: version list <project>")
        return None
    project_name = args[0]
    try:
        project = _resolve_project(svc, project_name)
    except ProjectNotFoundError:
        err_fn(f"Project {project_name!r} not found")
        return None
    versions = svc.project_service().list_versions(project.id)
    if not versions:
        print_fn(f"[dim]No versions for [bold]{project.name}[/bold] yet.[/]")
        return None

    tbl = Table(
        title=f"Versions — [bold cyan]{project.name}[/]",
        box=box.ROUNDED,
        show_lines=False,
        pad_edge=True,
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
    print_fn(tbl)
    return None


def _cmd_version_delete(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if len(args) < 2:
        err_fn("Usage: version delete <project> <version> [--yes]")
        return None
    project_name, version_name = args[0], args[1]
    yes = flags.get("yes") == "true" or flags.get("y") == "true"
    try:
        _, ver = _resolve_version(svc, project_name, version_name)
    except (ProjectNotFoundError, VersionNotFoundError) as exc:
        err_fn(str(exc))
        return None

    if yes:
        svc.storage().delete_version(ver.id)
        print_fn(f"[green]✓[/] Deleted version [bold]{ver.version_name}[/] [dim]({str(ver.id)[:8]})[/]")
        refresh_fn()
        return None

    print_fn(
        f"[yellow]Delete version [bold]{ver.version_name}[/] and [bold red]all[/] its BOM items?[/]"
    )
    return PendingInteraction(
        kind="confirm_version_delete",
        data={"version_id": ver.id, "version_name": ver.version_name},
        prompt="Confirm delete? [y/n]: ",
    )


def _cmd_version_copy(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if len(args) < 3:
        err_fn("Usage: version copy <project> <source> <new> [--notes TEXT]")
        return None
    project_name, source_version, new_version = args[0], args[1], args[2]
    notes = flags.get("notes") or flags.get("n")
    try:
        project, src_ver = _resolve_version(svc, project_name, source_version)
    except (ProjectNotFoundError, VersionNotFoundError) as exc:
        err_fn(str(exc))
        return None

    new_ver = svc.bom_service_ro().copy_version(src_ver.id, new_version, notes=notes)
    items = svc.storage().list_items_by_version(new_ver.id)
    print_fn(
        f"[green]✓[/] Copied [bold]{project.name}[/] / [bold]{src_ver.version_name}[/] "
        f"→ [bold]{new_ver.version_name}[/]  "
        f"[dim]({len(items)} item{'s' if len(items) != 1 else ''} copied · id={str(new_ver.id)[:8]})[/]"
    )
    refresh_fn()
    return None


# ── bom commands ──────────────────────────────────────────────────────────────

def _cmd_bom_list(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if len(args) < 2:
        err_fn("Usage: bom list <project> <version>")
        return None
    try:
        project, ver = _resolve_version(svc, args[0], args[1])
    except (ProjectNotFoundError, VersionNotFoundError) as exc:
        err_fn(str(exc))
        return None

    summary = svc.bom_service_ro().get_bom(ver.id)
    rate = svc.settings_service().get_rate()

    if not summary.items:
        print_fn(
            f"[dim]BOM for [bold]{project.name}[/bold] / [bold]{ver.version_name}[/bold] is empty.  "
            f"Use [bold]bom add[/bold] to add parts.[/]"
        )
        return None

    tbl = Table(
        title=f"[bold cyan]{project.name}[/]  /  [bold]{ver.version_name}[/]",
        box=box.ROUNDED,
        show_lines=True,
        pad_edge=True,
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

    usd_total = Decimal("0")
    irr_total = Decimal("0")

    for item in summary.items:
        unit_price = item.effective_unit_price()
        total = item.calculate_total()
        if total:
            if item.currency == "IRR":
                irr_total += total
                usd_total += irr_to_usd(total, rate)
            else:
                usd_total += total
                irr_total += usd_to_irr(total, rate)
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

    print_fn(tbl)
    print_fn(
        f"  {summary.item_count} item{'s' if summary.item_count != 1 else ''}  ·  "
        f"Total: [bold green]{fmt_usd(usd_total)}[/]  [dim]/[/]  [bold yellow]{fmt_irr(irr_total)}[/]  "
        f"[dim](1 USD = {rate:,.0f} IRR)[/]"
    )
    return None


def _cmd_bom_add(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    """Step 1: Search all active suppliers and display combined results."""
    if len(args) < 3:
        err_fn("Usage: bom add <project> <version> <part> --qty N [--ref DESIGNATOR]")
        return None

    project_name, version_name, part_name = args[0], args[1], args[2]
    qty_str = flags.get("qty") or flags.get("q")
    if not qty_str:
        err_fn("--qty is required (e.g. --qty 5)")
        return None
    try:
        qty = int(qty_str)
    except ValueError:
        err_fn(f"--qty must be an integer, got {qty_str!r}")
        return None
    if qty < 1:
        err_fn("--qty must be >= 1")
        return None

    ref = flags.get("ref") or flags.get("r") or part_name

    try:
        _, ver = _resolve_version(svc, project_name, version_name)
    except (ProjectNotFoundError, VersionNotFoundError) as exc:
        err_fn(str(exc))
        return None

    active = svc.get_active_suppliers()
    sup_label = " + ".join(s.name.upper() for s in active) if active else "—"
    print_fn(f'\nSearching [bold cyan]{sup_label}[/] for [bold yellow]"{part_name}"[/bold yellow]...')

    from bom_manager.core.bom_service import BOMService
    combined, search_failures = BOMService.search_parts_all(part_name, active)

    # Surface any supplier errors to the user
    for sup_name, err_msg in search_failures:
        print_fn(f"[yellow]⚠ {sup_name.upper()} search failed:[/] [dim]{err_msg}[/]")

    # Limit to 5 per supplier
    from collections import defaultdict
    per_sup: dict[str, list] = defaultdict(list)
    for sn, r in combined:
        if len(per_sup[sn]) < 5:
            per_sup[sn].append((sn, r))
    combined_limited = [item for items in per_sup.values() for item in items]

    tbl = Table(box=box.ROUNDED, show_lines=False, pad_edge=True, highlight=True)
    tbl.add_column("#", style="bold", width=3, justify="right")
    tbl.add_column("Src", style="dim", width=5, no_wrap=True)
    tbl.add_column("MPN", style="cyan", no_wrap=True)
    tbl.add_column("Supplier PN", style="dim", no_wrap=True)
    tbl.add_column("Manufacturer")
    tbl.add_column("Description")

    for i, (sn, r) in enumerate(combined_limited, 1):
        src = "[cyan]LCSC[/]" if sn.lower() == "lcsc" else "[yellow]LION[/]"
        tbl.add_row(
            str(i), src, r.mpn, r.supplier_pn,
            r.manufacturer or "—",
            r.description[:50] if r.description else "—",
        )
    manual_num = len(combined_limited) + 1
    tbl.add_row(str(manual_num), "[dim]—[/]", "[dim]Manual price entry[/]", "—", "—", "—")
    print_fn(tbl)

    total = len(combined_limited) + 1
    return PendingInteraction(
        kind="pick_part_multi",
        data={
            "combined": combined_limited,
            "active_suppliers": active,
            "version_id": ver.id,
            "part_name": part_name,
            "qty": qty,
            "ref": ref,
            "manual_num": manual_num,
            "total": total,
        },
        prompt=f"Select [1-{total}] ({manual_num}=manual): ",
    )


def _bom_add_fetch(
    selected,
    supplier_name: str,
    supplier_instance,
    ver: ProjectVersion,
    part_name: str,
    qty: int,
    ref: str,
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    """Step 2: Fetch part detail and show price breaks."""
    print_fn(f"\nFetching detail for [bold]{selected.supplier_pn}[/]...")
    rate = svc.settings_service().get_rate()

    try:
        detail = supplier_instance.get_part(selected.supplier_pn)
    except Exception as exc:
        err_fn(f"Failed to fetch part detail: {exc}")
        return None

    if detail.price_breaks:
        stock_color = "green" if detail.stock > 0 else "red"
        mfr_part = f"  ·  [dim]{detail.manufacturer}[/]" if detail.manufacturer else ""
        pb_tbl = Table(
            title=(
                f"[bold]{detail.mpn}[/]{mfr_part}  ·  "
                f"Stock: [{stock_color}]{detail.stock:,}[/]  ·  "
                f"[dim]{detail.currency}[/]"
            ),
            box=box.SIMPLE,
            show_edge=False,
            pad_edge=True,
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
        print_fn(pb_tbl)

    unit_price = detail.best_unit_price(qty)
    line_total = unit_price * Decimal(qty) if unit_price is not None else None
    print_fn(
        f"  Quantity [bold]{qty}[/]  ·  "
        f"Unit price [bold green]{fmt_price(unit_price, detail.currency, rate=rate)}[/]  ·  "
        f"Line total [bold]{fmt_price(line_total, detail.currency, rate=rate)}[/]"
    )

    return PendingInteraction(
        kind="confirm_add",
        data={
            "detail": detail,
            "supplier_name": supplier_name,
            "version_id": ver.id,
            "part_name": part_name,
            "qty": qty,
            "ref": ref,
            "supplier_pn": selected.supplier_pn,
        },
        prompt="Add to BOM? [y/n]: ",
    )


def _bom_add_persist(
    pending: PendingInteraction,
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> None:
    """Step 3: Persist the part after confirmation (build BOMItem from cached PartDetail)."""
    d = pending.data
    detail = d["detail"]
    rate = svc.settings_service().get_rate()

    if d.get("mode") == "add_source":
        # Add as alt source to existing item
        try:
            from bom_manager.core.models import PriceBreak as _PB
            price_breaks = [_PB(min_quantity=pb.min_quantity, unit_price=pb.unit_price) for pb in detail.price_breaks]
            unit_price = detail.best_unit_price(d["qty"])
            source = SupplierSource(
                supplier=d["supplier_name"],
                supplier_part_number=detail.supplier_pn or None,
                supplier_url=detail.url or None,
                matched_mpn=detail.mpn or None,
                unit_price=unit_price,
                price_breaks=price_breaks,
                currency=getattr(detail, "currency", "USD"),
            )
            svc.bom_service_ro().add_source_to_item(d["version_id"], d["target_item_id"], source)
        except Exception as exc:
            err_fn(str(exc))
            return
        print_fn(
            f"\n[green]✓[/] Added [bold cyan]{detail.mpn or detail.supplier_pn}[/] "
            f"as alt source  "
            f"@ [green]{fmt_price(unit_price, getattr(detail, 'currency', 'USD'), rate=rate) if unit_price else '—'}[/]  "
            f"[dim]({d['supplier_name']})[/]"
        )
        refresh_fn()
        return

    # Normal add: create new BOMItem
    try:
        from bom_manager.core.bom_service import _build_item
        item = _build_item(
            version_id=d["version_id"],
            user_part_name=d["part_name"],
            quantity=d["qty"],
            reference_designator=d["ref"],
            detail=detail,
            supplier_name=d["supplier_name"],
        )
        saved = svc.storage().add_item(item)
    except Exception as exc:
        err_fn(str(exc))
        return

    unit_price = saved.effective_unit_price()
    print_fn(
        f"\n[green]✓[/] Added [bold cyan]{saved.matched_mpn or saved.user_part_name}[/] × {saved.quantity}  "
        f"@ [green]{fmt_price(unit_price, saved.currency, rate=rate) if unit_price else '—'}[/]  "
        f"[dim]({saved.supplier_part_number or saved.supplier})[/]"
    )
    refresh_fn()


def _cmd_bom_remove(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if len(args) < 3:
        err_fn("Usage: bom remove <project> <version> <item-id> [--yes]")
        return None
    yes = flags.get("yes") == "true" or flags.get("y") == "true"
    try:
        _, ver = _resolve_version(svc, args[0], args[1])
        item = _resolve_item(svc, ver.id, args[2])
    except (ProjectNotFoundError, VersionNotFoundError, ItemNotFoundError) as exc:
        err_fn(str(exc))
        return None

    if yes:
        svc.bom_service_ro().remove_part(ver.id, item.id)
        print_fn(
            f"[green]✓[/] Removed [bold]{item.matched_mpn or item.user_part_name}[/] "
            f"[dim]({str(item.id)[:8]})[/]"
        )
        refresh_fn()
        return None

    print_fn(
        f"[yellow]Remove [bold]{item.matched_mpn or item.user_part_name}[/] "
        f"(ref [bold]{item.reference_designator}[/]) from BOM?[/]"
    )
    return PendingInteraction(
        kind="confirm_delete",
        data={"bom_remove": True, "version_id": ver.id, "item_id": item.id,
              "item_name": item.matched_mpn or item.user_part_name},
        prompt="Confirm remove? [y/n]: ",
    )


def _cmd_bom_update_qty(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if len(args) < 4:
        err_fn("Usage: bom update-qty <project> <version> <item-id> <new-qty>")
        return None
    try:
        new_qty = int(args[3])
    except ValueError:
        err_fn(f"new-qty must be an integer, got {args[3]!r}")
        return None
    if new_qty < 1:
        err_fn("new-qty must be >= 1")
        return None

    try:
        _, ver = _resolve_version(svc, args[0], args[1])
        item = _resolve_item(svc, ver.id, args[2])
    except (ProjectNotFoundError, VersionNotFoundError, ItemNotFoundError) as exc:
        err_fn(str(exc))
        return None

    old_qty = item.quantity
    old_price = item.effective_unit_price()
    updated = svc.bom_service_ro().update_quantity(ver.id, item.id, new_qty)
    new_price = updated.effective_unit_price()

    tier_note = ""
    if old_price is not None and new_price is not None and old_price != new_price:
        direction = "[green]↓[/]" if new_price < old_price else "[yellow]↑[/]"
        tier_note = f"  {direction} price tier changed"

    print_fn(
        f"[green]✓[/] [bold]{updated.matched_mpn or updated.user_part_name}[/]  "
        f"qty [dim]{old_qty}[/] → [bold]{new_qty}[/]  "
        f"@ [green]{_fmt_price(new_price)}[/]  "
        f"line total [bold]{_fmt_price(updated.total_price)}[/]"
        f"{tier_note}"
    )
    refresh_fn()
    return None


def _cmd_bom_cost(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if len(args) < 2:
        err_fn("Usage: bom cost <project> <version> [--boards N]")
        return None

    boards_str = flags.get("boards") or flags.get("n") or "100"
    try:
        boards = int(boards_str)
    except ValueError:
        err_fn(f"--boards must be an integer, got {boards_str!r}")
        return None
    if boards < 2:
        err_fn("--boards must be >= 2")
        return None

    try:
        project, ver = _resolve_version(svc, args[0], args[1])
    except (ProjectNotFoundError, VersionNotFoundError) as exc:
        err_fn(str(exc))
        return None

    summary = svc.bom_service_ro().get_bom(ver.id)
    if not summary.items:
        print_fn("[dim]BOM is empty.[/]")
        return None

    rows: list[tuple] = []
    pb_total_1 = Decimal("0")
    pb_total_n = Decimal("0")

    for item in summary.items:
        qty_per_board = item.quantity
        unit_1 = _best_price_at(item.price_breaks, qty_per_board)
        total_1 = unit_1 * Decimal(qty_per_board) if unit_1 is not None else None
        qty_n = qty_per_board * boards
        unit_n = _best_price_at(item.price_breaks, qty_n)
        line_n = unit_n * Decimal(qty_n) if unit_n is not None else None
        per_board_n = line_n / Decimal(boards) if line_n is not None else None
        if total_1 is not None:
            pb_total_1 += total_1
        if per_board_n is not None:
            pb_total_n += per_board_n
        rows.append((item, qty_per_board, unit_1, total_1, qty_n, unit_n, line_n, per_board_n))

    tbl = Table(
        title=(
            f"[bold cyan]{project.name}[/]  /  [bold]{ver.version_name}[/]  ·  "
            f"Cost comparison: [bold]1[/] vs [bold]{boards:,}[/] boards"
        ),
        box=box.ROUNDED,
        show_lines=True,
        pad_edge=True,
    )
    tbl.add_column("Ref", style="dim", no_wrap=True)
    tbl.add_column("MPN", style="cyan", no_wrap=True)
    tbl.add_column("Qty", justify="right")
    tbl.add_column("Unit @1", justify="right")
    tbl.add_column("Total @1", justify="right")
    tbl.add_column(f"Qty×{boards:,}", justify="right", header_style="bold yellow")
    tbl.add_column(f"Unit @{boards:,}", justify="right", style="green", header_style="bold yellow")
    tbl.add_column(f"Total @{boards:,}", justify="right", style="green", header_style="bold yellow")
    tbl.add_column("Save/ea", justify="right", style="bold green")

    for (item, qty_per_board, unit_1, total_1, qty_n, unit_n, line_n, per_board_n) in rows:
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
            str(qty_per_board),
            _fmt_price(unit_1),
            _fmt_price(total_1),
            f"{qty_n:,}",
            _fmt_price(unit_n),
            _fmt_price(line_n),
            saving_str,
        )

    print_fn(tbl)

    grand_total_n = pb_total_n * Decimal(boards)
    if pb_total_1 > 0:
        saved_per_board = pb_total_1 - pb_total_n
        pct_saved = int(saved_per_board / pb_total_1 * 100)
        cheaper = saved_per_board > 0
    else:
        saved_per_board = Decimal("0")
        pct_saved = 0
        cheaper = False

    summary_tbl = Table(box=box.SIMPLE, show_header=False, pad_edge=True)
    summary_tbl.add_column("label", style="dim", min_width=28)
    summary_tbl.add_column("1 board", justify="right", min_width=12)
    summary_tbl.add_column(f"{boards:,} boards", justify="right", style="bold green", min_width=14)
    summary_tbl.add_row("Per-board cost", f"${pb_total_1:.4f}", f"${pb_total_n:.4f}")
    summary_tbl.add_row(
        f"Total ({boards:,} boards)",
        f"${pb_total_1 * boards:.4f}",
        f"${grand_total_n:.4f}",
    )
    print_fn(summary_tbl)

    if cheaper:
        print_fn(
            f"  [bold green]You save ${saved_per_board:.4f}/board  ({pct_saved}% cheaper)[/]"
            f"  when building [bold]{boards:,}[/] boards instead of 1."
        )
    else:
        print_fn(f"  [dim]No price tier improvement at {boards:,} boards for this BOM.[/]")
    return None


def _cmd_bom_export(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if len(args) < 2:
        err_fn("Usage: bom export <project> <version> [--format csv|xlsx] [--output-dir DIR]")
        return None
    fmt = flags.get("format") or "csv"
    if fmt not in ("csv", "xlsx"):
        err_fn(f"--format must be csv or xlsx, got {fmt!r}")
        return None
    output_dir_str = flags.get("output-dir") or flags.get("o")
    out_dir = Path(output_dir_str) if output_dir_str else None

    try:
        project, ver = _resolve_version(svc, args[0], args[1])
    except (ProjectNotFoundError, VersionNotFoundError) as exc:
        err_fn(str(exc))
        return None

    stem = f"{project.name}_{ver.version_name}".replace(" ", "_")
    try:
        path = svc.bom_service_ro().export_bom(ver.id, format=fmt, output_dir=out_dir, filename=stem)
    except BOMManagerError as exc:
        err_fn(str(exc))
        return None

    print_fn(
        f"[green]✓[/] Exported [bold]{project.name}[/] / [bold]{ver.version_name}[/]  "
        f"→  [cyan]{path}[/]"
    )
    return None


def _cmd_bom_diff(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if len(args) < 3:
        err_fn("Usage: bom diff <project> <version-a> <version-b>")
        return None

    try:
        project, ver_a = _resolve_version(svc, args[0], args[1])
        _, ver_b = _resolve_version(svc, args[0], args[2])
    except (ProjectNotFoundError, VersionNotFoundError) as exc:
        err_fn(str(exc))
        return None

    diff = svc.bom_service_ro().diff_versions(ver_a.id, ver_b.id)

    if diff.is_identical:
        print_fn(
            f"[dim]Versions [bold]{args[1]}[/] and [bold]{args[2]}[/] "
            f"are identical — no differences found.[/]"
        )
        return None

    tbl = Table(
        title=(
            f"[bold cyan]{project.name}[/]  ·  "
            f"[bold]{args[1]}[/] → [bold]{args[2]}[/]"
        ),
        box=box.ROUNDED,
        show_lines=True,
        pad_edge=True,
    )
    tbl.add_column("", width=2, no_wrap=True)
    tbl.add_column("Ref", style="dim", no_wrap=True)
    tbl.add_column("MPN", style="cyan", no_wrap=True)
    tbl.add_column("LCSC #", style="dim", no_wrap=True)
    tbl.add_column("Qty", justify="right")
    tbl.add_column("Unit Price", justify="right")
    tbl.add_column("Changes")

    for item in diff.removed:
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
    for item in diff.added:
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
    for old, new in diff.changed:
        changes = []
        if old.quantity != new.quantity:
            changes.append(f"qty {old.quantity}→{new.quantity}")
        if old.reference_designator != new.reference_designator:
            changes.append(f"ref {old.reference_designator}→{new.reference_designator}")
        if old.user_part_name != new.user_part_name:
            changes.append("name changed")
        p_old = old.effective_unit_price()
        p_new = new.effective_unit_price()
        if p_old != p_new:
            changes.append(f"price {_fmt_price(p_old)}→{_fmt_price(p_new)}")
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

    print_fn(tbl)
    parts = []
    if diff.added:
        parts.append(f"[green]{len(diff.added)} added[/]")
    if diff.removed:
        parts.append(f"[red]{len(diff.removed)} removed[/]")
    if diff.changed:
        parts.append(f"[yellow]{len(diff.changed)} changed[/]")
    print_fn("  " + "  ·  ".join(parts))
    return None


def _cmd_bom_sources(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if len(args) < 3:
        err_fn("Usage: bom sources <project> <version> <item-id>")
        return None
    try:
        _, ver = _resolve_version(svc, args[0], args[1])
        item = _resolve_item(svc, ver.id, args[2])
    except (ProjectNotFoundError, VersionNotFoundError, ItemNotFoundError) as exc:
        err_fn(str(exc))
        return None

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
    print_fn(tbl)
    if item.alt_sources:
        print_fn(
            f"[dim]Use [bold]bom use-source {args[0]} {args[1]} {args[2]} <N>[/bold] "
            "to activate an alt source (N=1-based).[/dim]"
        )
    return None


def _cmd_bom_use_source(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if len(args) < 4:
        err_fn("Usage: bom use-source <project> <version> <item-id> <N>")
        return None
    try:
        n = int(args[3])
    except ValueError:
        err_fn(f"N must be an integer, got {args[3]!r}")
        return None
    try:
        _, ver = _resolve_version(svc, args[0], args[1])
        item = _resolve_item(svc, ver.id, args[2])
    except (ProjectNotFoundError, VersionNotFoundError, ItemNotFoundError) as exc:
        err_fn(str(exc))
        return None

    if not item.alt_sources:
        err_fn("This item has no alternative sources.  Use 'bom add-source' first.")
        return None
    if not (1 <= n <= len(item.alt_sources)):
        err_fn(
            f"N must be between 1 and {len(item.alt_sources)} "
            "(use 'bom sources' to list available sources)"
        )
        return None

    try:
        updated = svc.bom_service_ro().use_source(ver.id, item.id, n - 1)
    except (ValueError, Exception) as exc:
        err_fn(str(exc))
        return None

    rate = svc.settings_service().get_rate()
    new_price = updated.effective_unit_price()
    print_fn(
        f"[green]✓[/] Now using [bold cyan]{updated.supplier}[/] "
        f"([bold]{updated.supplier_part_number or '—'}[/]) as primary source  "
        f"@ [green]{fmt_price(new_price, updated.currency, rate=rate) if new_price else '—'}[/]"
    )
    refresh_fn()
    return None


def _cmd_bom_add_source(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    """Step 1: Search all active suppliers for an alt source to add."""
    if len(args) < 3:
        err_fn("Usage: bom add-source <project> <version> <item-id> [--query TEXT] [--manual PRICE]")
        return None

    try:
        _, ver = _resolve_version(svc, args[0], args[1])
        item = _resolve_item(svc, ver.id, args[2])
    except (ProjectNotFoundError, VersionNotFoundError, ItemNotFoundError) as exc:
        err_fn(str(exc))
        return None

    manual_price = flags.get("manual") or flags.get("m")
    if manual_price is not None:
        try:
            unit_price, currency = parse_manual_price(manual_price)
        except ValueError as exc:
            err_fn(str(exc))
            return None
        source = SupplierSource(supplier="manual", unit_price=unit_price, currency=currency)
        try:
            svc.bom_service_ro().add_source_to_item(ver.id, item.id, source)
        except Exception as exc:
            err_fn(str(exc))
            return None
        rate = svc.settings_service().get_rate()
        print_fn(
            f"[green]✓[/] Added manual alt source to "
            f"[bold cyan]{item.matched_mpn or item.user_part_name}[/]  "
            f"@ [green]{fmt_price(unit_price, currency, rate=rate)}[/]"
        )
        refresh_fn()
        return None

    search_query = flags.get("query") or flags.get("q") or item.user_part_name
    active = svc.get_active_suppliers()
    if not active:
        err_fn("No active suppliers configured.  Use 'settings suppliers' to enable one.")
        return None

    sup_label = " + ".join(s.name.upper() for s in active)
    print_fn(f'\nSearching [bold cyan]{sup_label}[/] for [bold yellow]"{search_query}"[/bold yellow]...')

    from bom_manager.core.bom_service import BOMService
    combined = BOMService.search_parts_all(search_query, active)

    from collections import defaultdict
    per_sup: dict[str, list] = defaultdict(list)
    for sn, r in combined:
        if len(per_sup[sn]) < 5:
            per_sup[sn].append((sn, r))
    combined_limited = [entry for entries in per_sup.values() for entry in entries]

    if not combined_limited:
        print_fn("[yellow]No results found from any active supplier.[/]")
        return None

    tbl = Table(box=box.ROUNDED, show_lines=False, pad_edge=True, highlight=True)
    tbl.add_column("#", style="bold", width=3, justify="right")
    tbl.add_column("Src", style="dim", width=5, no_wrap=True)
    tbl.add_column("MPN", style="cyan", no_wrap=True)
    tbl.add_column("Supplier PN", style="dim", no_wrap=True)
    tbl.add_column("Manufacturer")
    tbl.add_column("Description")

    for i, (sn, r) in enumerate(combined_limited, 1):
        src = "[cyan]LCSC[/]" if sn.lower() == "lcsc" else "[yellow]LION[/]"
        tbl.add_row(
            str(i), src, r.mpn, r.supplier_pn,
            r.manufacturer or "—",
            r.description[:50] if r.description else "—",
        )
    print_fn(tbl)

    total = len(combined_limited)
    return PendingInteraction(
        kind="pick_part_multi",
        data={
            "combined": combined_limited,
            "active_suppliers": active,
            "version_id": ver.id,
            "part_name": item.user_part_name,
            "qty": item.quantity,
            "ref": item.reference_designator,
            "manual_num": total + 1,   # no manual option for add-source via search
            "total": total,
            "mode": "add_source",
            "target_item_id": item.id,
        },
        prompt=f"Select source [1-{total}]: ",
    )


def _cmd_settings_show(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    settings = svc.settings_service().all_settings()
    rate_fetched = svc.settings_service().rate_last_fetched()
    fetched_str = rate_fetched.strftime("%Y-%m-%d %H:%M UTC") if rate_fetched else "never"

    tbl = Table(
        title="[bold cyan]Settings[/]",
        box=box.ROUNDED,
        show_lines=False,
        pad_edge=True,
    )
    tbl.add_column("Key", style="bold cyan", no_wrap=True)
    tbl.add_column("Value")
    tbl.add_column("Note", style="dim")

    notes = {
        "usd_to_irr_rate": f"rate last auto-fetched: {fetched_str}",
        "active_suppliers": "lcsc | lion | all",
        "rate_last_fetched": "",
    }
    for key, value in settings.items():
        if key == "rate_last_fetched":
            continue
        tbl.add_row(key, value, notes.get(key, ""))
    print_fn(tbl)
    print_fn(
        "[dim][yellow]⚠[/yellow]  The auto-fetched rate is the official interbank rate. "
        "Lion Electronic uses the market rate — override with [bold]settings rate <VALUE>[/bold] if needed.[/dim]"
    )
    return None


def _cmd_settings_rate(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if not args:
        rate = svc.settings_service().get_rate()
        rate_fetched = svc.settings_service().rate_last_fetched()
        fetched_str = rate_fetched.strftime("%Y-%m-%d %H:%M UTC") if rate_fetched else "never"
        print_fn(
            f"  Current USD → IRR rate: [bold green]{rate:,.0f}[/]  "
            f"[dim](auto-fetched: {fetched_str})[/]\n"
            f"  Use [bold]settings rate <VALUE>[/bold] to override."
        )
        return None

    try:
        new_rate = Decimal(args[0].replace(",", ""))
    except Exception:
        err_fn(f"Invalid rate {args[0]!r} — must be a number (e.g. 650000)")
        return None

    try:
        svc.settings_service().set_rate(new_rate)
    except ValueError as exc:
        err_fn(str(exc))
        return None

    print_fn(f"[green]✓[/] USD → IRR rate set to [bold green]{new_rate:,.0f}[/]")
    return None


def _cmd_settings_fetch_rate(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    print_fn("[dim]Fetching live USD → IRR rate from open.er-api.com...[/]")
    rate = svc.settings_service().fetch_live_rate()
    if rate is None:
        err_fn("Failed to fetch live rate. Check your internet connection.")
        return None
    print_fn(
        f"[green]✓[/] Live rate fetched: [bold green]1 USD = {rate:,.0f} IRR[/]\n"
        "[dim][yellow]⚠[/yellow]  This is the official interbank rate. "
        "Lion Electronic uses the market rate — override with [bold]settings rate <VALUE>[/bold] if needed.[/dim]"
    )
    return None


def _cmd_settings_suppliers(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    if not args:
        active = svc.settings_service().get_active_suppliers()
        print_fn(
            f"  Active suppliers: [bold cyan]{', '.join(s.upper() for s in active)}[/]\n"
            "  Use [bold]settings suppliers lcsc|lion|all[/bold] to change."
        )
        return None

    value = args[0].strip().lower()
    try:
        svc.settings_service().set_active_suppliers(value)
    except ValueError as exc:
        err_fn(str(exc))
        return None

    active = svc.settings_service().get_active_suppliers()
    print_fn(
        f"[green]✓[/] Active suppliers: [bold cyan]{', '.join(s.upper() for s in active)}[/]"
    )
    return None


def _cmd_help(
    args: list[str],
    flags: dict[str, str],
    svc: _Services,
    print_fn: PrintFn,
    err_fn: ErrFn,
    refresh_fn: RefreshFn,
) -> Optional[PendingInteraction]:
    tbl = Table(
        title="[bold cyan]BOM Manager[/] — Available Commands",
        box=box.ROUNDED,
        show_lines=False,
        pad_edge=True,
    )
    tbl.add_column("Command", style="bold cyan", no_wrap=True)
    tbl.add_column("Description")

    rows = [
        ("project create <name>",                    "Create a new project  [dim][--description TEXT][/]"),
        ("project list",                              "List all projects"),
        ("project delete <name>",                    "Delete a project and all its data  [dim][--yes][/]"),
        ("version create <project> <version>",       "Create a new BOM version  [dim][--notes TEXT][/]"),
        ("version list <project>",                   "List versions of a project"),
        ("version delete <project> <version>",       "Delete a BOM version  [dim][--yes][/]"),
        ("version copy <project> <src> <new>",       "Copy a version  [dim][--notes TEXT][/]"),
        ("bom list <project> <version>",             "Show all items in a BOM"),
        ("bom add <project> <version> <part>",       "Search all active suppliers  [dim]--qty N [--ref D][/]"),
        ("bom remove <project> <version> <item-id>","Remove a part from a BOM  [dim][--yes][/]"),
        ("bom update-qty <p> <v> <id> <qty>",        "Update quantity and recalculate price tier"),
        ("bom cost <project> <version>",             "Compare per-board cost  [dim][--boards N][/]"),
        ("bom export <project> <version>",           "Export BOM to CSV/XLSX  [dim][--format csv|xlsx][/]"),
        ("bom diff <project> <v-a> <v-b>",           "Show differences between two versions"),
        ("bom sources <project> <version> <id>",     "Show primary + alt supplier sources for an item"),
        ("bom add-source <p> <v> <id>",              "Add alt source  [dim][--query TEXT] [--manual PRICE][/]"),
        ("bom use-source <p> <v> <id> <N>",          "Promote alt source N (1-based) to primary"),
        ("settings show",                            "Show all settings (rate, suppliers)"),
        ("settings rate [VALUE]",                    "Show or set the USD → IRR exchange rate"),
        ("settings fetch-rate",                      "Auto-fetch live rate from open.er-api.com"),
        ("settings suppliers [lcsc|lion|all]",       "Show or change active supplier(s)"),
        ("help",                                     "Show this help"),
        ("clear",                                    "Clear the output log"),
    ]
    for cmd, desc in rows:
        tbl.add_row(cmd, desc)
    print_fn(tbl)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch table
# ─────────────────────────────────────────────────────────────────────────────

COMMANDS: dict[tuple[str, ...], Callable] = {
    ("project", "create"):      _cmd_project_create,
    ("project", "list"):        _cmd_project_list,
    ("project", "delete"):      _cmd_project_delete,
    ("version", "create"):      _cmd_version_create,
    ("version", "list"):        _cmd_version_list,
    ("version", "delete"):      _cmd_version_delete,
    ("version", "copy"):        _cmd_version_copy,
    ("bom", "list"):            _cmd_bom_list,
    ("bom", "add"):             _cmd_bom_add,
    ("bom", "remove"):          _cmd_bom_remove,
    ("bom", "update-qty"):      _cmd_bom_update_qty,
    ("bom", "cost"):            _cmd_bom_cost,
    ("bom", "export"):          _cmd_bom_export,
    ("bom", "diff"):            _cmd_bom_diff,
    ("bom", "sources"):         _cmd_bom_sources,
    ("bom", "add-source"):      _cmd_bom_add_source,
    ("bom", "use-source"):      _cmd_bom_use_source,
    ("settings", "show"):       _cmd_settings_show,
    ("settings", "rate"):       _cmd_settings_rate,
    ("settings", "fetch-rate"): _cmd_settings_fetch_rate,
    ("settings", "suppliers"):  _cmd_settings_suppliers,
    ("help",):                  _cmd_help,
}


# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────

APP_CSS = """
Screen {
    layout: vertical;
}

#project-panel {
    height: 35%;
    min-height: 8;
    border: solid $primary;
    border-title-color: $text;
    border-title-align: left;
    padding: 0 1;
}

#project-tree {
    height: 1fr;
    background: transparent;
}

#output-log {
    height: 1fr;
    border: solid $surface-lighten-2;
    padding: 0 1;
    scrollbar-gutter: stable;
}

#command-bar {
    height: 3;
    border-top: solid $primary;
    background: $surface;
    align: left middle;
    padding: 0 1;
}

#prompt-label {
    width: auto;
    color: $primary;
    text-style: bold;
    padding-right: 1;
}

#command-input {
    width: 1fr;
    border: none;
    background: transparent;
    padding: 0;
}

Footer {
    height: 1;
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Main Application
# ─────────────────────────────────────────────────────────────────────────────

class BOMManagerApp(App):
    """BOM Manager TUI application."""

    CSS = APP_CSS
    TITLE = f"BOM Manager v{VERSION}"

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("ctrl+l", "clear_log", "Clear log"),
        Binding("f5", "refresh_tree", "Refresh tree"),
        Binding("escape", "cancel_pending", "Cancel", show=False),
    ]

    def __init__(self, db_path: Optional[str] = None) -> None:
        super().__init__()
        self._svc = _Services(db_path=db_path)
        self._pending: Optional[PendingInteraction] = None

    # ── Layout ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            with Vertical(id="project-panel"):
                yield Tree("Projects", id="project-tree")
            yield RichLog(id="output-log", highlight=True, markup=True, wrap=True)
            with Horizontal(id="command-bar"):
                yield Static(">", id="prompt-label")
                yield CommandInput(
                    placeholder="Type a command…  (help for list)",
                    id="command-input",
                )
        yield Footer()

    # ── Startup ──────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        log = self.query_one("#output-log", RichLog)
        log.write(WELCOME)

        panel = self.query_one("#project-panel")
        panel.border_title = "Projects"

        self._load_tree_worker()
        self.query_one(CommandInput).focus()

    # ── Tree loading ─────────────────────────────────────────────────────────

    @work(thread=True, exclusive=False, name="load-tree")
    def _load_tree_worker(self) -> None:
        """Runs in a thread — reads all projects/versions from SQLite."""
        try:
            projects = self._svc.project_service().list_projects()
            tree_data = []
            for p in projects:
                versions = self._svc.project_service().list_versions(p.id)
                version_data = []
                for v in versions:
                    try:
                        summary = self._svc.bom_service_ro().get_bom(v.id)
                        item_count = summary.item_count
                        total_cost = summary.total_cost
                    except Exception:
                        item_count = 0
                        total_cost = None
                    version_data.append((v, item_count, total_cost))
                tree_data.append((p, version_data))
            self.call_from_thread(self._populate_tree, tree_data)
        except Exception as exc:
            self.call_from_thread(self._append_error, f"Tree load failed: {exc}")

    def _populate_tree(self, tree_data: list) -> None:
        """Runs on the event loop — rebuilds the Tree widget."""
        tree = self.query_one("#project-tree", Tree)
        tree.clear()
        tree.root.label = f"[dim]{len(tree_data)} project{'s' if len(tree_data) != 1 else ''}[/]"
        tree.root.expand()

        if not tree_data:
            tree.root.add_leaf(
                "[dim]No projects yet.  Type [bold]project create <name>[/bold] to start.[/]"
            )
            return

        for (project, versions) in tree_data:
            node_data = TreeNodeData(
                kind="project",
                project_id=project.id,
                project_name=project.name,
            )
            project_node = tree.root.add(
                f"[bold cyan]{project.name}[/]  "
                f"[dim]({len(versions)} version{'s' if len(versions) != 1 else ''})[/]",
                data=node_data,
                expand=True,
            )

            if not versions:
                project_node.add_leaf("[dim]no versions yet[/]")
            else:
                for (v, count, cost) in versions:
                    ver_data = TreeNodeData(
                        kind="version",
                        project_id=project.id,
                        project_name=project.name,
                        version_id=v.id,
                        version_name=v.version_name,
                        item_count=count,
                        total_cost=cost,
                    )
                    label = _format_version_label(v.version_name, count, cost)
                    project_node.add_leaf(label, data=ver_data)

    def _schedule_tree_refresh(self) -> None:
        """Called from worker threads — schedules tree reload on the event loop."""
        self.call_from_thread(self._load_tree_worker)

    # ── Command input ────────────────────────────────────────────────────────

    @on(Input.Submitted, "#command-input")
    def on_command_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        inp = self.query_one(CommandInput)
        inp.value = ""

        if not raw:
            return

        inp.push_history(raw)

        log = self.query_one("#output-log", RichLog)
        log.write(f"[dim cyan]>[/] [bold]{raw}[/]")

        # Disable input while processing
        inp.disabled = True

        if self._pending is not None:
            self._handle_pending_worker(raw)
        else:
            self._execute_command_worker(raw)

    # ── Command execution (threaded) ─────────────────────────────────────────

    @work(thread=True, exclusive=False, name="cmd")
    def _execute_command_worker(self, raw: str) -> None:
        """Parse and dispatch a fresh command in a background thread."""
        try:
            if raw.lower() in ("clear",):
                self.call_from_thread(self.action_clear_log)
                return
            if raw.lower() in ("quit", "exit"):
                self.call_from_thread(self.action_quit)
                return

            positionals, flags = parse_command(raw)
            if not positionals:
                return

            # Look up handler: try 2-token key first, then 1-token
            key2 = tuple(positionals[:2])
            key1 = tuple(positionals[:1])
            handler = COMMANDS.get(key2) or COMMANDS.get(key1)

            if handler is None:
                self.call_from_thread(
                    self._append_error,
                    f"Unknown command: [bold]{positionals[0]}[/].  Type [bold]help[/bold] to see all commands.",
                )
                return

            # Args passed to handler: everything after the verb(s)
            n_keys = 2 if COMMANDS.get(key2) else 1
            handler_args = positionals[n_keys:]

            result = handler(
                handler_args,
                flags,
                self._svc,
                self._safe_print,
                self._safe_err,
                self._schedule_tree_refresh,
            )

            if result is not None:
                self.call_from_thread(self._set_pending, result)

        except Exception as exc:
            self.call_from_thread(self._append_error, f"Unexpected error: {exc}")
        finally:
            self.call_from_thread(self._re_enable_input)

    @work(thread=True, exclusive=False, name="pending")
    def _handle_pending_worker(self, response: str) -> None:
        """Handle the user's response to a pending multi-step interaction."""
        pending = self._pending
        if pending is None:
            self.call_from_thread(self._re_enable_input)
            return

        try:
            kind = pending.kind

            if kind == "pick_part_multi":
                d = pending.data
                try:
                    choice = int(response.strip())
                except ValueError:
                    self.call_from_thread(self._append_error, "Please enter a number.")
                    self.call_from_thread(self._keep_pending, pending)
                    return

                total = d["total"]
                manual_num = d["manual_num"]
                if not (1 <= choice <= total):
                    self.call_from_thread(
                        self._append_error,
                        f"Please enter a number between 1 and {total}.",
                    )
                    self.call_from_thread(self._keep_pending, pending)
                    return

                if choice == manual_num:
                    # User wants manual price entry
                    self._safe_print(
                        f"\n[dim]Enter price for [bold]{d['part_name']}[/bold] "
                        f"(e.g. [bold]600000 IRR[/bold] or [bold]0.50 USD[/bold]):[/]"
                    )
                    self.call_from_thread(
                        self._set_pending,
                        PendingInteraction(
                            kind="manual_price",
                            data={
                                "version_id": d["version_id"],
                                "part_name": d["part_name"],
                                "qty": d["qty"],
                                "ref": d["ref"],
                            },
                            prompt="Price (e.g. 600000 IRR or 0.50 USD): ",
                        )
                    )
                    return

                # Supplier result selected
                combined = d["combined"]
                supplier_name, selected_result = combined[choice - 1]
                active_suppliers = d["active_suppliers"]
                supplier_instance = next(
                    (s for s in active_suppliers if s.name.lower() == supplier_name.lower()), None
                )
                if supplier_instance is None:
                    self.call_from_thread(
                        self._append_error,
                        f"Supplier {supplier_name!r} no longer available."
                    )
                    self.call_from_thread(self._clear_pending)
                    return

                ver = self._svc.project_service().get_version(d["version_id"])
                next_pending = _bom_add_fetch(
                    selected_result,
                    supplier_name,
                    supplier_instance,
                    ver,
                    d["part_name"],
                    d["qty"],
                    d["ref"],
                    self._svc,
                    self._safe_print,
                    self._safe_err,
                    self._schedule_tree_refresh,
                )
                if next_pending is not None:
                    # Propagate add_source mode through to confirm_add
                    if d.get("mode") == "add_source" and next_pending.kind == "confirm_add":
                        next_pending.data["mode"] = "add_source"
                        next_pending.data["target_item_id"] = d["target_item_id"]
                        next_pending.prompt = "Add as alt source? [y/n]: "
                    self.call_from_thread(self._set_pending, next_pending)
                else:
                    self.call_from_thread(self._clear_pending)

            elif kind == "manual_price":
                d = pending.data
                try:
                    unit_price, currency = parse_manual_price(response.strip())
                except ValueError as exc:
                    self.call_from_thread(
                        self._append_error,
                        f"Could not parse price: {exc}. "
                        "Try formats like [bold]600000 IRR[/bold] or [bold]0.50 USD[/bold]."
                    )
                    self.call_from_thread(self._keep_pending, pending)
                    return

                rate = self._svc.settings_service().get_rate()
                qty = d["qty"]
                line_total = unit_price * Decimal(qty)
                self._safe_print(
                    f"\n  [bold]{d['part_name']}[/]  × {qty}  "
                    f"@ [bold green]{fmt_price(unit_price, currency, rate=rate)}[/]  "
                    f"→ line total [bold]{fmt_price(line_total, currency, rate=rate)}[/]"
                )
                self.call_from_thread(
                    self._set_pending,
                    PendingInteraction(
                        kind="confirm_manual_add",
                        data={
                            "version_id": d["version_id"],
                            "part_name": d["part_name"],
                            "qty": qty,
                            "ref": d["ref"],
                            "unit_price": unit_price,
                            "currency": currency,
                        },
                        prompt="Add to BOM? [y/n]: ",
                    )
                )

            elif kind == "confirm_manual_add":
                r = response.strip().lower()
                if r in ("y", "yes"):
                    d = pending.data
                    try:
                        saved = self._svc.bom_service_ro().add_part_manual(
                            version_id=d["version_id"],
                            user_part_name=d["part_name"],
                            quantity=d["qty"],
                            reference_designator=d["ref"],
                            unit_price=d["unit_price"],
                            currency=d["currency"],
                        )
                        rate = self._svc.settings_service().get_rate()
                        unit_price = saved.effective_unit_price()
                        self._safe_print(
                            f"\n[green]✓[/] Added [bold cyan]{saved.user_part_name}[/] × {saved.quantity}  "
                            f"@ [green]{fmt_price(unit_price, saved.currency, rate=rate) if unit_price else '—'}[/]  "
                            f"[dim](manual)[/]"
                        )
                        self._schedule_tree_refresh()
                    except Exception as exc:
                        self.call_from_thread(self._append_error, str(exc))
                else:
                    self._safe_print("[dim]Aborted.[/]")
                self.call_from_thread(self._clear_pending)

            elif kind == "confirm_add":
                r = response.strip().lower()
                if r in ("y", "yes"):
                    _bom_add_persist(
                        pending,
                        self._svc,
                        self._safe_print,
                        self._safe_err,
                        self._schedule_tree_refresh,
                    )
                else:
                    self._safe_print("[dim]Aborted.[/]")
                self.call_from_thread(self._clear_pending)

            elif kind == "confirm_delete":
                r = response.strip().lower()
                if r in ("y", "yes"):
                    d = pending.data
                    if d.get("bom_remove"):
                        self._svc.bom_service_ro().remove_part(d["version_id"], d["item_id"])
                        self._safe_print(f"[green]✓[/] Removed [bold]{d['item_name']}[/]")
                        self._schedule_tree_refresh()
                    else:
                        self._svc.project_service().delete_project(d["project_id"])
                        self._safe_print(f"[green]✓[/] Deleted project [bold]{d['project_name']}[/]")
                        self._schedule_tree_refresh()
                else:
                    self._safe_print("[dim]Aborted.[/]")
                self.call_from_thread(self._clear_pending)

            elif kind == "confirm_version_delete":
                r = response.strip().lower()
                if r in ("y", "yes"):
                    d = pending.data
                    self._svc.storage().delete_version(d["version_id"])
                    self._safe_print(f"[green]✓[/] Deleted version [bold]{d['version_name']}[/]")
                    self._schedule_tree_refresh()
                else:
                    self._safe_print("[dim]Aborted.[/]")
                self.call_from_thread(self._clear_pending)

        except Exception as exc:
            self.call_from_thread(self._append_error, f"Error: {exc}")
            self.call_from_thread(self._clear_pending)
        finally:
            self.call_from_thread(self._re_enable_input)

    # ── Thread-safe UI callbacks ──────────────────────────────────────────────

    def _safe_print(self, renderable: object) -> None:
        """Thread-safe: write a Rich renderable to the output log."""
        self.call_from_thread(self._append_output, renderable)

    def _safe_err(self, msg: str) -> None:
        """Thread-safe: write an error message to the output log."""
        self.call_from_thread(self._append_error, msg)

    def _append_output(self, renderable: object) -> None:
        self.query_one("#output-log", RichLog).write(renderable)

    def _append_error(self, msg: str) -> None:
        self.query_one("#output-log", RichLog).write(f"[bold red]Error:[/] {msg}")

    def _re_enable_input(self) -> None:
        inp = self.query_one(CommandInput)
        inp.disabled = False
        inp.focus()

    def _set_pending(self, pending: PendingInteraction) -> None:
        self._pending = pending
        inp = self.query_one(CommandInput)
        inp.placeholder = pending.prompt
        inp.disabled = False
        inp.focus()

    def _keep_pending(self, pending: PendingInteraction) -> None:
        """Re-show the same pending prompt without re-enabling (already enabled by set_pending)."""
        self._pending = pending
        inp = self.query_one(CommandInput)
        inp.placeholder = pending.prompt
        inp.disabled = False
        inp.focus()

    def _clear_pending(self) -> None:
        self._pending = None
        inp = self.query_one(CommandInput)
        inp.placeholder = "Type a command…  (help for list)"

    # ── Tree click → auto-fill ────────────────────────────────────────────────

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if not isinstance(data, TreeNodeData):
            return
        if data.kind == "version":
            inp = self.query_one(CommandInput)
            inp.value = f"bom list {data.project_name} {data.version_name}"
            inp.cursor_position = len(inp.value)
            inp.focus()

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_clear_log(self) -> None:
        self.query_one("#output-log", RichLog).clear()

    def action_refresh_tree(self) -> None:
        self._load_tree_worker()

    def action_cancel_pending(self) -> None:
        if self._pending is not None:
            self._clear_pending()
            self._append_output("[dim]Cancelled.[/]")

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def on_unmount(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._svc.close)
