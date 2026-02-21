"""Storage backends for persisting projects and BOMs."""

from bom_manager.storage.base import StorageProtocol
from bom_manager.storage.sqlite import SQLiteStorage

__all__ = ["StorageProtocol", "SQLiteStorage"]
