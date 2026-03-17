"""
Instance management tools for hot-pluggable nanobot instances.

These tools allow switching between complete nanobot instances at runtime,
including configuration, cron jobs, history, and workspace.
"""

from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.instance.manager import InstanceManager


class SwitchInstanceTool(Tool):
    """
    Switch to a different nanobot instance.

    This tool allows switching the entire nanobot runtime environment at runtime,
    including configuration, cron jobs, history, media, and workspace.
    """

    @property
    def name(self) -> str:
        return "switch_instance"

    @property
    def description(self) -> str:
        return "Switch to a different nanobot instance (complete environment including config, cron, history, and workspace"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the instance root directory"
                },
                "name": {
                    "type": "string",
                    "description": "Optional name for the instance (for future reference)"
                },
                "initialize": {
                    "type": "boolean",
                    "description": "Whether to initialize the instance if it doesn't exist (default: true)"
                }
            },
            "required": ["path"],
            "additionalProperties": False
        }

    def __init__(self, instance_manager: InstanceManager | None = None):
        self.instance_manager = instance_manager or InstanceManager.get()

    async def execute(self, **params: Any) -> str:
        """
        Switch to a new instance.

        Args:
            params: Dictionary containing:
                - path: Path to the instance root directory
                - name: Optional instance name
                - initialize: Whether to create the instance if it doesn't exist

        Returns:
            str: Result of the switch operation
        """
        path = params.get("path")
        name = params.get("name")
        initialize = params.get("initialize", True)

        if not path:
            return "Error: Path parameter is required"

        if self.instance_manager.switch_instance(path, initialize=initialize):
            if name:
                self.instance_manager.add_instance_metadata(path, {"name": name})
            current_path = self.instance_manager.current_instance
            return f"Successfully switched to instance: {current_path}"
        else:
            return "Error: Failed to switch instance"


class ListInstancesTool(Tool):
    """List all known nanobot instances."""

    @property
    def name(self) -> str:
        return "list_instances"

    @property
    def description(self) -> str:
        return "List all known nanobot instances including the current one"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False
        }

    def __init__(self, instance_manager: InstanceManager | None = None):
        self.instance_manager = instance_manager or InstanceManager.get()

    async def execute(self, **params: Any) -> str:
        """
        List all known instances.

        Args:
            params: Empty dictionary (no parameters required)

        Returns:
            str: Formatted list of instances
        """
        instances = self.instance_manager.list_instances()

        if not instances:
            return "No instances configured"

        lines = []
        for instance in instances:
            current = " (current)" if instance["is_current"] else ""
            lines.append(f"- {instance['path']} ({instance['name']}){current}")

        return "\n".join(lines)


class GetInstanceInfoTool(Tool):
    """Get detailed information about the current or specified instance."""

    @property
    def name(self) -> str:
        return "get_instance_info"

    @property
    def description(self) -> str:
        return "Get detailed information about the current or specified instance"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Optional path to the instance (defaults to current)"
                }
            },
            "additionalProperties": False
        }
    }
    def __init__(self, instance_manager: InstanceManager | None = None):
        self.instance_manager = instance_manager or InstanceManager.get()

    async def execute(self, **params: Any) -> str:
        """
        Get instance information.

        Args:
            params: Dictionary containing:
                - path: Optional path to the instance

        Returns:
            str: Detailed instance information
        """
        path = params.get("path")
        info = self.instance_manager.get_instance_info(path)

        lines = [
            f"Instance Path: {info['path']}",
            f"Is Current: {info['is_current']}",
            f"Exists: {info['exists']}",
            f"Config Exists: {info['config_exists']}",
        ]

        if info["exists"]:
            structure = info["structure"]
            stats = info["stats"]
            lines.extend([
                "",
                "Structure:",
                f"  Workspace: {structure['workspace']}",
                f"  Cron Directory: {structure['cron']}",
                f"  History Directory: {structure['history']}",
                f"  Logs Directory: {structure['logs']}",
                f"  Media Directory: {structure['media']}",
                "",
                "Statistics:",
                f"  Workspace Skills: {stats['workspace_skills']}",
                f"  Cron Jobs: {stats['cron_jobs']}",
                f"  Log Files: {stats['log_files']}",
                f"  Media Files: {stats['media_files']}",
            ])

        return "\n".join(lines)


class CreateInstanceTool(Tool):
    """Create a new nanobot instance."""

    @property
    def name(self) -> str:
        return "create_instance"

    @property
    def description(self) -> str:
        return "Create a new nanobot instance with full directory structure and default config"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the new instance root directory"
                },
                "name": {
                    "type": "string",
                    "description": "Optional name for the instance"
                }
            },
            "required": ["path"],
            "additionalProperties": False
        }

    def __init__(self, instance_manager: InstanceManager | None = None):
        self.instance_manager = instance_manager or InstanceManager.get()

    async def execute(self, **params: Any) -> str:
        """
        Create a new instance.

        Args:
            params: Dictionary containing:
                - path: Path to the new instance root directory
                - name: Optional instance name

        Returns:
            str: Result of the create operation
        """
        path = params.get("path")
        name = params.get("name")

        if not path:
            return "Error: Path parameter is required"

        instance_path = self.instance_manager.create_instance(path, name=name)
        if instance_path:
            return f"Successfully created instance at: {instance_path}"
        else:
            return "Error: Failed to create instance"
