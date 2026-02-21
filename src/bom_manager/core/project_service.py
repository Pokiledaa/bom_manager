"""Project and version management service."""

from __future__ import annotations

import logging
from typing import Optional, Union
from uuid import UUID

from bom_manager.core.exceptions import ProjectNotFoundError, VersionNotFoundError
from bom_manager.core.models import Project, ProjectVersion
from bom_manager.storage.base import StorageProtocol

log = logging.getLogger(__name__)


def _try_parse_uuid(value: Union[str, UUID]) -> Optional[UUID]:
    """Return a UUID if *value* is already one or is a valid UUID string, else None."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


class ProjectService:
    """
    Manages projects and their versions.

    All persistence is delegated to the injected *storage* backend.

    Parameters
    ----------
    storage:
        Any object satisfying ``StorageProtocol``.
    """

    def __init__(self, storage: StorageProtocol) -> None:
        self._storage = storage

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def create_project(
        self,
        name: str,
        description: Optional[str] = None,
    ) -> Project:
        """
        Create and persist a new project.

        Parameters
        ----------
        name:
            Display name for the project (must be non-empty).
        description:
            Optional free-text description.

        Returns
        -------
        Project
            The newly created project with a fresh UUID and timestamps.
        """
        project = Project(name=name, description=description)
        created = self._storage.create_project(project)
        log.info("Created project %r (id=%s)", created.name, created.id)
        return created

    def list_projects(self) -> list[Project]:
        """Return all projects, newest first."""
        return self._storage.list_projects()

    def get_project(self, name_or_id: Union[str, UUID]) -> Project:
        """
        Return a project by UUID or by exact name.

        Raises
        ------
        ProjectNotFoundError
            If no project matches the given identifier.
        """
        uid = _try_parse_uuid(name_or_id)
        if uid is not None:
            project = self._storage.get_project(uid)
            if project is not None:
                return project
        else:
            # Scan by name (names are not guaranteed unique, return first match)
            for p in self._storage.list_projects():
                if p.name == str(name_or_id):
                    return p

        raise ProjectNotFoundError(f"Project {name_or_id!r} not found")

    def delete_project(self, name_or_id: Union[str, UUID]) -> None:
        """
        Delete a project and all its versions and BOM items (cascade).

        Raises
        ------
        ProjectNotFoundError
            If no project matches the given identifier.
        """
        project = self.get_project(name_or_id)
        self._storage.delete_project(project.id)
        log.info("Deleted project %r (id=%s)", project.name, project.id)

    # ------------------------------------------------------------------
    # Versions
    # ------------------------------------------------------------------

    def create_version(
        self,
        project_id: UUID,
        version_name: str,
        notes: Optional[str] = None,
    ) -> ProjectVersion:
        """
        Create a new version for an existing project.

        Parameters
        ----------
        project_id:
            UUID of the owning project.  The project must exist.
        version_name:
            Short label such as ``"v1"`` or ``"prototype-rev-A"``.
        notes:
            Optional free-text notes about what changed in this version.

        Raises
        ------
        ProjectNotFoundError
            If the project does not exist.
        """
        # Verify the project exists before creating the version
        if self._storage.get_project(project_id) is None:
            raise ProjectNotFoundError(f"Project {project_id} not found")

        version = ProjectVersion(
            project_id=project_id,
            version_name=version_name,
            notes=notes,
        )
        created = self._storage.create_version(version)
        log.info(
            "Created version %r for project %s (version_id=%s)",
            created.version_name, project_id, created.id,
        )
        return created

    def get_version(self, version_id: UUID) -> ProjectVersion:
        """
        Return a version by its UUID.

        Raises
        ------
        VersionNotFoundError
            If the version does not exist.
        """
        version = self._storage.get_version(version_id)
        if version is None:
            raise VersionNotFoundError(f"Version {version_id} not found")
        return version

    def list_versions(self, project_id: UUID) -> list[ProjectVersion]:
        """Return all versions for a project, newest first."""
        return self._storage.list_versions_by_project(project_id)
