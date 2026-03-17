"""
Workspace manager for hot-pluggable workspace support.

This module provides a central manager to handle workspace switching
at runtime, including reloading all dependent components.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from nanobot.config.paths import get_workspace_path
from nanobot.utils.helpers import ensure_dir, sync_workspace_templates

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.config.schema import Config


class WorkspaceManager:
    """
    Manages workspace switching and component reinitialization.

    Handles the lifecycle of workspaces including:
    - Tracking current workspace
    - Validating workspace paths
    - Syncing templates to new workspaces
    - Reinitializing dependent components
    - Managing workspace metadata
    """

    def __init__(self, config: Config):
        self.config = config
        self._current_workspace: Path | None = None
        self._on_switch_callbacks: list[Callable[[Path, Path], Any]] = []
        self._workspace_metadata: dict[str, dict[str, Any]] = {}

    @property
    def current_workspace(self) -> Path:
        """Get the current workspace path."""
        if self._current_workspace is None:
            self._current_workspace = self._resolve_path(self.config.agents.defaults.workspace)
            self._initialize_workspace(self._current_workspace)
        return self._current_workspace

    def _resolve_path(self, path_str: str) -> Path:
        """Resolve workspace path from string."""
        return get_workspace_path(path_str)

    def _initialize_workspace(self, workspace: Path) -> None:
        """Initialize a new workspace by syncing templates."""
        logger.info(f"Initializing workspace: {workspace}")
        ensure_dir(workspace)
        # Sync templates to the new workspace
        sync_workspace_templates(workspace, silent=True)
        # Create necessary subdirectories
        (workspace / "skills").mkdir(exist_ok=True)
        (workspace / "memory").mkdir(exist_ok=True)
        (workspace / "sessions").mkdir(exist_ok=True)

    def register_switch_callback(self, callback: Callable[[Path, Path], Any]) -> None:
        """
        Register a callback to be called when workspace switches.

        Args:
            callback: Function that takes old_workspace and new_workspace as arguments
        """
        self._on_switch_callbacks.append(callback)

    def switch_workspace(self, new_workspace_path: str | Path) -> bool:
        """
        Switch to a new workspace.

        Args:
            new_workspace_path: Path to the new workspace

        Returns:
            bool: True if switch was successful, False otherwise
        """
        try:
            new_workspace = self._resolve_path(str(new_workspace_path))
            old_workspace = self.current_workspace

            if new_workspace == old_workspace:
                logger.warning(f"Already in workspace: {new_workspace}")
                return False

            logger.info(f"Switching workspace from {old_workspace} to {new_workspace}")

            # Initialize the new workspace if it doesn't exist
            self._initialize_workspace(new_workspace)

            # Call all registered callbacks
            for callback in self._on_switch_callbacks:
                try:
                    callback(old_workspace, new_workspace)
                except Exception as e:
                    logger.error(f"Error in workspace switch callback: {e}")

            # Update current workspace reference
            self._current_workspace = new_workspace

            logger.success(f"Successfully switched to workspace: {new_workspace}")
            return True

        except Exception as e:
            logger.error(f"Failed to switch workspace: {e}")
            return False

    def list_workspaces(self) -> list[dict[str, Any]]:
        """
        List all known workspaces.

        Returns:
            List of workspace info dictionaries
        """
        workspaces = []

        # Add default workspace
        default_path = self._resolve_path(self.config.agents.defaults.workspace)
        workspaces.append({
            "path": str(default_path),
            "name": "default",
            "is_current": default_path == self.current_workspace,
            "exists": default_path.exists()
        })

        # Add other known workspaces from metadata
        for path_str, metadata in self._workspace_metadata.items():
            path = Path(path_str)
            if path != default_path:
                workspaces.append({
                    "path": path_str,
                    "name": metadata.get("name", path.name),
                    "is_current": path == self.current_workspace,
                    "exists": path.exists()
                })

        return workspaces

    def get_workspace_info(self, workspace_path: str | Path | None = None) -> dict[str, Any]:
        """
        Get detailed information about a workspace.

        Args:
            workspace_path: Path to the workspace (None for current)

        Returns:
            dict: Workspace information
        """
        if workspace_path is None:
            workspace_path = self.current_workspace
        else:
            workspace_path = self._resolve_path(str(workspace_path))

        # Check workspace structure
        skills_dir = workspace_path / "skills"
        memory_dir = workspace_path / "memory"
        sessions_dir = workspace_path / "sessions"

        return {
            "path": str(workspace_path),
            "exists": workspace_path.exists(),
            "is_current": workspace_path == self.current_workspace,
            "structure": {
                "skills": skills_dir.exists(),
                "memory": memory_dir.exists(),
                "sessions": sessions_dir.exists()
            },
            "stats": {
                "skills_count": len(list(skills_dir.glob("*"))) if skills_dir.exists() else 0,
                "memory_files": len(list(memory_dir.glob("*.md"))) if memory_dir.exists() else 0,
                "sessions_count": len(list(sessions_dir.glob("*.jsonl"))) if sessions_dir.exists() else 0
            }
        }

    def add_workspace_metadata(self, workspace_path: str | Path, metadata: dict[str, Any]) -> None:
        """
        Add metadata for a workspace.

        Args:
            workspace_path: Path to the workspace
            metadata: Metadata dictionary (e.g. {"name": "my-workspace"})
        """
        path_str = str(self._resolve_path(str(workspace_path)))
        if path_str not in self._workspace_metadata:
            self._workspace_metadata[path_str] = {}
        self._workspace_metadata[path_str].update(metadata)

    def cleanup(self) -> None:
        """Cleanup resources when shutting down."""
        self._on_switch_callbacks.clear()
        self._workspace_metadata.clear()
