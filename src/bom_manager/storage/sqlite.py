"""SQLite-backed storage implementation for BOM Manager."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from bom_manager.core.models import BOMItem, PriceBreak, Project, ProjectVersion

_DEFAULT_DB_PATH = Path("data/bom.db")
_DEFAULT_CACHE_TTL = 24 * 60 * 60  # 24 hours in seconds

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_versions (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version_name TEXT NOT NULL,
    notes        TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bom_items (
    id                   TEXT PRIMARY KEY,
    version_id           TEXT NOT NULL REFERENCES project_versions(id) ON DELETE CASCADE,
    reference_designator TEXT NOT NULL,
    user_part_name       TEXT NOT NULL,
    matched_mpn          TEXT,
    supplier             TEXT,
    supplier_part_number TEXT,
    supplier_url         TEXT,
    quantity             INTEGER NOT NULL,
    unit_price           TEXT,
    price_breaks         TEXT NOT NULL DEFAULT '[]',
    total_price          TEXT
);

CREATE TABLE IF NOT EXISTS part_cache (
    supplier    TEXT NOT NULL,
    part_number TEXT NOT NULL,
    data_json   TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (supplier, part_number)
);
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dt_to_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _str_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _decimal_or_none(value: Optional[str]) -> Optional[Decimal]:
    return Decimal(value) if value is not None else None


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

class SQLiteStorage:
    """
    SQLite-backed implementation of StorageProtocol.

    Creates the database file and tables automatically on first use.
    All operations are synchronous and use Python's stdlib sqlite3 module.
    """

    def __init__(
        self,
        db_path: Path | str = _DEFAULT_DB_PATH,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL,
    ) -> None:
        self._db_path = Path(db_path)
        self._cache_ttl = cache_ttl_seconds
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_DDL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteStorage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def create_project(self, project: Project) -> Project:
        self._conn.execute(
            """
            INSERT INTO projects (id, name, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(project.id),
                project.name,
                project.description,
                _dt_to_str(project.created_at),
                _dt_to_str(project.updated_at),
            ),
        )
        self._conn.commit()
        return project

    def get_project(self, project_id: UUID) -> Optional[Project]:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id = ?", (str(project_id),)
        ).fetchone()
        return _row_to_project(row) if row else None

    def list_projects(self) -> list[Project]:
        rows = self._conn.execute(
            "SELECT * FROM projects ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_project(r) for r in rows]

    def delete_project(self, project_id: UUID) -> bool:
        cur = self._conn.execute(
            "DELETE FROM projects WHERE id = ?", (str(project_id),)
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Versions
    # ------------------------------------------------------------------

    def create_version(self, version: ProjectVersion) -> ProjectVersion:
        self._conn.execute(
            """
            INSERT INTO project_versions (id, project_id, version_name, notes, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(version.id),
                str(version.project_id),
                version.version_name,
                version.notes,
                _dt_to_str(version.created_at),
            ),
        )
        self._conn.commit()
        return version

    def get_version(self, version_id: UUID) -> Optional[ProjectVersion]:
        row = self._conn.execute(
            "SELECT * FROM project_versions WHERE id = ?", (str(version_id),)
        ).fetchone()
        return _row_to_version(row) if row else None

    def list_versions_by_project(self, project_id: UUID) -> list[ProjectVersion]:
        rows = self._conn.execute(
            "SELECT * FROM project_versions WHERE project_id = ? ORDER BY created_at DESC",
            (str(project_id),),
        ).fetchall()
        return [_row_to_version(r) for r in rows]

    def delete_version(self, version_id: UUID) -> bool:
        cur = self._conn.execute(
            "DELETE FROM project_versions WHERE id = ?", (str(version_id),)
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # BOM Items
    # ------------------------------------------------------------------

    def add_item(self, item: BOMItem) -> BOMItem:
        self._conn.execute(
            """
            INSERT INTO bom_items (
                id, version_id, reference_designator, user_part_name,
                matched_mpn, supplier, supplier_part_number, supplier_url,
                quantity, unit_price, price_breaks, total_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _item_to_row(item),
        )
        self._conn.commit()
        return item

    def update_item(self, item: BOMItem) -> BOMItem:
        cur = self._conn.execute(
            """
            UPDATE bom_items SET
                version_id           = ?,
                reference_designator = ?,
                user_part_name       = ?,
                matched_mpn          = ?,
                supplier             = ?,
                supplier_part_number = ?,
                supplier_url         = ?,
                quantity             = ?,
                unit_price           = ?,
                price_breaks         = ?,
                total_price          = ?
            WHERE id = ?
            """,
            _item_to_row(item)[1:] + (_item_to_row(item)[0],),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"BOMItem {item.id} not found")
        return item

    def remove_item(self, item_id: UUID) -> bool:
        cur = self._conn.execute(
            "DELETE FROM bom_items WHERE id = ?", (str(item_id),)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_items_by_version(self, version_id: UUID) -> list[BOMItem]:
        rows = self._conn.execute(
            "SELECT * FROM bom_items WHERE version_id = ? ORDER BY reference_designator",
            (str(version_id),),
        ).fetchall()
        return [_row_to_item(r) for r in rows]

    # ------------------------------------------------------------------
    # Supplier part cache
    # ------------------------------------------------------------------

    def get_cached_part(
        self,
        supplier: str,
        part_number: str,
        *,
        max_age_seconds: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT data_json, fetched_at FROM part_cache WHERE supplier = ? AND part_number = ?",
            (supplier, part_number),
        ).fetchone()
        if row is None:
            return None

        fetched_at = _str_to_dt(row["fetched_at"])
        ttl = max_age_seconds if max_age_seconds is not None else self._cache_ttl
        age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        if age > ttl:
            return None

        return json.loads(row["data_json"])

    def cache_part(
        self,
        supplier: str,
        part_number: str,
        data: dict[str, Any],
        *,
        fetched_at: Optional[datetime] = None,
    ) -> None:
        ts = fetched_at or datetime.now(timezone.utc)
        self._conn.execute(
            """
            INSERT INTO part_cache (supplier, part_number, data_json, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(supplier, part_number) DO UPDATE SET
                data_json  = excluded.data_json,
                fetched_at = excluded.fetched_at
            """,
            (supplier, part_number, json.dumps(data), _dt_to_str(ts)),
        )
        self._conn.commit()


# ---------------------------------------------------------------------------
# Row <-> Model converters
# ---------------------------------------------------------------------------

def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        id=UUID(row["id"]),
        name=row["name"],
        description=row["description"],
        created_at=_str_to_dt(row["created_at"]),
        updated_at=_str_to_dt(row["updated_at"]),
    )


def _row_to_version(row: sqlite3.Row) -> ProjectVersion:
    return ProjectVersion(
        id=UUID(row["id"]),
        project_id=UUID(row["project_id"]),
        version_name=row["version_name"],
        notes=row["notes"],
        created_at=_str_to_dt(row["created_at"]),
    )


def _row_to_item(row: sqlite3.Row) -> BOMItem:
    price_breaks_raw: list[dict[str, Any]] = json.loads(row["price_breaks"])
    price_breaks = [
        PriceBreak(
            min_quantity=pb["min_quantity"],
            unit_price=Decimal(pb["unit_price"]),
        )
        for pb in price_breaks_raw
    ]
    return BOMItem(
        id=UUID(row["id"]),
        version_id=UUID(row["version_id"]),
        reference_designator=row["reference_designator"],
        user_part_name=row["user_part_name"],
        matched_mpn=row["matched_mpn"],
        supplier=row["supplier"],
        supplier_part_number=row["supplier_part_number"],
        supplier_url=row["supplier_url"],
        quantity=row["quantity"],
        unit_price=_decimal_or_none(row["unit_price"]),
        price_breaks=price_breaks,
        total_price=_decimal_or_none(row["total_price"]),
    )


def _item_to_row(item: BOMItem) -> tuple[Any, ...]:
    price_breaks_json = json.dumps(
        [
            {"min_quantity": pb.min_quantity, "unit_price": str(pb.unit_price)}
            for pb in item.price_breaks
        ]
    )
    return (
        str(item.id),
        str(item.version_id),
        item.reference_designator,
        item.user_part_name,
        item.matched_mpn,
        item.supplier,
        item.supplier_part_number,
        item.supplier_url,
        item.quantity,
        str(item.unit_price) if item.unit_price is not None else None,
        price_breaks_json,
        str(item.total_price) if item.total_price is not None else None,
    )
