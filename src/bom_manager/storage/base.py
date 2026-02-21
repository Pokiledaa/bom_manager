"""Abstract storage protocol for BOM Manager."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Protocol, runtime_checkable
from uuid import UUID

from bom_manager.core.models import BOMItem, Project, ProjectVersion


@runtime_checkable
class StorageProtocol(Protocol):
    """
    Defines the full persistence contract for BOM Manager.

    All methods that return a single entity return None when not found.
    All list methods return an empty list when there are no results.
    """

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def create_project(self, project: Project) -> Project:
        """Persist a new project and return it."""
        ...

    def get_project(self, project_id: UUID) -> Optional[Project]:
        """Return the project with the given id, or None."""
        ...

    def list_projects(self) -> list[Project]:
        """Return all projects ordered by created_at descending."""
        ...

    def delete_project(self, project_id: UUID) -> bool:
        """
        Delete the project and all its versions/items.
        Returns True if a row was deleted, False if not found.
        """
        ...

    # ------------------------------------------------------------------
    # Versions
    # ------------------------------------------------------------------

    def create_version(self, version: ProjectVersion) -> ProjectVersion:
        """Persist a new version and return it."""
        ...

    def get_version(self, version_id: UUID) -> Optional[ProjectVersion]:
        """Return the version with the given id, or None."""
        ...

    def list_versions_by_project(self, project_id: UUID) -> list[ProjectVersion]:
        """Return all versions for a project ordered by created_at descending."""
        ...

    def delete_version(self, version_id: UUID) -> bool:
        """
        Delete the version and all its BOM items.
        Returns True if a row was deleted, False if not found.
        """
        ...

    # ------------------------------------------------------------------
    # BOM Items
    # ------------------------------------------------------------------

    def add_item(self, item: BOMItem) -> BOMItem:
        """Persist a new BOM item and return it."""
        ...

    def update_item(self, item: BOMItem) -> BOMItem:
        """
        Overwrite all fields of an existing BOM item.
        Raises KeyError if the item does not exist.
        """
        ...

    def remove_item(self, item_id: UUID) -> bool:
        """
        Remove a BOM item by id.
        Returns True if a row was deleted, False if not found.
        """
        ...

    def list_items_by_version(self, version_id: UUID) -> list[BOMItem]:
        """Return all BOM items for a version ordered by reference_designator."""
        ...

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
        """
        Return the cached part data dict for (supplier, part_number), or None.

        If *max_age_seconds* is given it overrides the backend's default TTL.
        Returns None when the entry is absent or expired.
        """
        ...

    def cache_part(
        self,
        supplier: str,
        part_number: str,
        data: dict[str, Any],
        *,
        fetched_at: Optional[datetime] = None,
    ) -> None:
        """
        Store *data* for (supplier, part_number).

        *fetched_at* defaults to the current UTC time when omitted.
        Upserts: overwrites any existing entry for the same key.
        """
        ...
