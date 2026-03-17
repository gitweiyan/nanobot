"""
Workspace tools for managing hot-pluggable workspaces.

These tools allow switching, listing, and managing workspaces at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.workspace.manager import WorkspaceManager


class SwitchWorkspaceTool(Tool):
    """
    Switch to a different workspace.

    This tool allows switching the agent's current workspace at runtime,
    which affects where skills, memory, and sessions are stored and accessed.
    """

    @property
    def name(self) -> str:
        return "switch_workspace"

    @property
    def description(self) -> str:
        return "Switch to a different workspace"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the new workspace (absolute or relative)"
                },
                "name": {
                    "type": "string",
                    "description": "Optional name for the workspace (for future reference)"
                }
            },
            "required": ["path"],
            "additionalProperties": False
        }

    def __init__(self, workspace_manager: WorkspaceManager):
        self.workspace_manager = workspace_manager

    async def execute(self, **params: Any) -> str:
        """
        Switch to a new workspace.

        Args:
            params: Dictionary containing:
                - path: Path to the new workspace
                - name: Optional workspace name

        Returns:
            str: Result of the switch operation
        """
        path = params.get("path")
        name = params.get("name")

        if not path:
            return "Error: Path parameter is required"

        if self.workspace_manager.switch_workspace(path):
            if name:
                self.workspace_manager.add_workspace_metadata(path, {"name": name})
            current_path = self.workspace_manager.current_workspace
            return f"Successfully switched to workspace: {current_path}"
        else:
            return "Error: Failed to switch workspace"


class ListWorkspacesTool(Tool):
    """List all known workspaces including the current one."""

    @property
    def name(self) -> str:
        return "list_workspaces"

    @property
    def description(self) -> str:
        return "List all known workspaces including the current one"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False
        }

    def __init__(self, workspace_manager: WorkspaceManager):
        self.workspace_manager = workspace_manager

    async def execute(self, **params: Any) -> str:
        """
        List all known workspaces.

        Args:
            params: Empty dictionary (no parameters required)

        Returns:
            str: Formatted list of workspaces
        """
        workspaces = self.workspace_manager.list_workspaces()

        if not workspaces:
            return "No workspaces configured"

        lines = []
        for ws in workspaces:
            current = " (current)" if ws["is_current"] else ""
            lines.append(f"- {ws['path']} ({ws['name']}){current}")

        return "\n".join(lines)


class GetWorkspaceInfoTool(Tool):
    """Get detailed information about the current or specified workspace."""

    @property
    def name(self) -> str:
        return "get_workspace_info"

    @property
    def description(self) -> str:
        return "Get detailed information about the current or specified workspace"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Optional path to the workspace (defaults to current)"
                }
            },
            "additionalProperties": False
        }

    def __init__(self, workspace_manager: WorkspaceManager):
        self.workspace_manager = workspace_manager

    async def execute(self, **params: Any) -> str:
        """
        Get workspace information.

        Args:
            params: Dictionary containing:
                - path: Optional path to the workspace

        Returns:
            str: Detailed workspace information
        """
        path = params.get("path")
        info = self.workspace_manager.get_workspace_info(path)

        lines = [
            f"Workspace Path: {info['path']}",
            f"Is Current: {info['is_current']}",
            f"Exists: {info['exists']}",
        ]

        if info["exists"]:
            structure = info["structure"]
            stats = info["stats"]
            lines.extend([
                "",
                "Structure:",
                f"  Skills Directory: {structure['skills']}",
                f"  Memory Directory: {structure['memory']}",
                f"  Sessions Directory: {structure['sessions']}",
                "",
                "Statistics:",
                f"  Number of Skills: {stats['skills_count']}",
                f"  Memory Files: {stats['memory_files']}",
                f"  Sessions: {stats['sessions_count']}",
            ])

        return "\n".join(lines)
