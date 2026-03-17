"""
Instance manager for hot-pluggable nanobot instances.

This module provides functionality to switch between complete nanobot instances
at runtime, including configuration, cron jobs, history, media, and workspace.
Each instance is a self-contained directory similar to ~/.nanobot.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from nanobot.config.loader import load_config, save_config, set_config_path
from nanobot.config.schema import Config
from nanobot.utils.helpers import ensure_dir, sync_workspace_templates

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


class InstanceManager:
    """
    Manages complete nanobot instances, allowing runtime switching.

    Each instance is a self-contained directory with the following structure:
    instance-root/
    ├── config.json          # Configuration file
    ├── workspace/           # User workspace (skills, memory, sessions)
    ├── cron/                # Cron job storage
    ├── history/             # CLI history
    ├── logs/                # Log files
    ├── media/               # Media files (images, downloads)
    └── bridge/              # WhatsApp bridge installation (optional)
    """

    _instance: InstanceManager | None = None

    def __new__(cls, *args, **kwargs):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, instance_path: Path | str | None = None):
        if hasattr(self, "_initialized") and self._initialized:
            return

        self._initialized = True
        self._current_instance: Path | None = None
        self._current_config: Config | None = None
        self._on_switch_callbacks: list[Callable[[Path | None, Path], Any]] = []
        self._instance_metadata: dict[str, dict[str, Any]] = {}

        # Initialize with default instance or provided path
        if instance_path:
            self.switch_instance(instance_path)
        else:
            self._current_instance = Path.home() / ".nanobot"
            self._current_config = load_config()

    @classmethod
    def get(cls) -> InstanceManager:
        """Get the singleton instance."""
        if cls._instance is None:
            cls._instance = InstanceManager()
        return cls._instance

    @property
    def current_instance(self) -> Path:
        """Get the current instance root path."""
        if self._current_instance is None:
            self._current_instance = Path.home() / ".nanobot"
        return self._current_instance

    @property
    def current_config(self) -> Config:
        """Get the current instance configuration."""
        if self._current_config is None:
            self._current_config = load_config()
        return self._current_config

    def get_path(self, subpath: str | Path) -> Path:
        """
        Get a path relative to the current instance root.

        Args:
            subpath: Subpath within the instance directory

        Returns:
            Absolute path to the subdirectory/file
        """
        return ensure_dir(self.current_instance / subpath)

    def _initialize_instance(self, instance_path: Path) -> None:
        """
        Initialize a new instance directory with required structure.

        Args:
            instance_path: Path to the new instance root
        """
        logger.info(f"Initializing instance at: {instance_path}")

        # Create directory structure
        ensure_dir(instance_path)
        ensure_dir(instance_path / "workspace")
        ensure_dir(instance_path / "cron")
        ensure_dir(instance_path / "history")
        ensure_dir(instance_path / "logs")
        ensure_dir(instance_path / "media")

        # Create default config if it doesn't exist
        config_path = instance_path / "config.json"
        if not config_path.exists():
            config = Config()
            # Set workspace to be within the instance directory
            config.agents.defaults.workspace = str(instance_path / "workspace")
            save_config(config, config_path)
            logger.info(f"Created default config at: {config_path}")

        # Sync workspace templates
        sync_workspace_templates(instance_path / "workspace", silent=True)

        logger.success(f"Instance initialized: {instance_path}")

    def register_switch_callback(self, callback: Callable[[Path | None, Path], Any]) -> None:
        """
        Register a callback to be called when instance switches.

        Args:
            callback: Function that takes old_instance_path and new_instance_path as arguments
        """
        self._on_switch_callbacks.append(callback)

    def switch_instance(self, new_instance_path: str | Path, initialize: bool = True) -> bool:
        """
        Switch to a different instance.

        Args:
            new_instance_path: Path to the new instance root directory
            initialize: Whether to initialize the instance if it doesn't exist

        Returns:
            bool: True if switch was successful, False otherwise
        """
        try:
            new_instance = Path(new_instance_path).expanduser().resolve()
            old_instance = self._current_instance

            if new_instance == old_instance:
                logger.warning(f"Already using instance: {new_instance}")
                return False

            logger.info(f"Switching instance from {old_instance} to {new_instance}")

            # Validate or initialize the new instance
            if not new_instance.exists():
                if initialize:
                    self._initialize_instance(new_instance)
                else:
                    logger.error(f"Instance does not exist: {new_instance}")
                    return False

            # Update global config path
            config_path = new_instance / "config.json"
            if not config_path.exists():
                logger.error(f"Config file not found in instance: {config_path}")
                return False

            set_config_path(config_path)

            # Load new configuration
            self._current_config = load_config(config_path)
            self._current_instance = new_instance

            # Call all registered callbacks
            for callback in self._on_switch_callbacks:
                try:
                    callback(old_instance, new_instance)
                except Exception as e:
                    logger.error(f"Error in instance switch callback: {e}")

            logger.success(f"Successfully switched to instance: {new_instance}")
            return True

        except Exception as e:
            logger.error(f"Failed to switch instance: {e}")
            return False

    def create_instance(self, instance_path: str | Path, name: str | None = None) -> Path | None:
        """
        Create a new instance.

        Args:
            instance_path: Path to the new instance root
            name: Optional name for the instance

        Returns:
            Path to the created instance, or None if failed
        """
        try:
            instance_path = Path(instance_path).expanduser().resolve()

            if instance_path.exists():
                logger.warning(f"Instance already exists: {instance_path}")
                return instance_path

            self._initialize_instance(instance_path)

            if name:
                self.add_instance_metadata(instance_path, {"name": name})

            return instance_path

        except Exception as e:
            logger.error(f"Failed to create instance: {e}")
            return None

    def list_instances(self) -> list[dict[str, Any]]:
        """
        List all known instances.

        Returns:
            List of instance info dictionaries
        """
        instances = []

        # Add default instance
        default_path = Path.home() / ".nanobot"
        instances.append({
            "path": str(default_path),
            "name": self._instance_metadata.get(str(default_path), {}).get("name", "default"),
            "is_current": default_path == self.current_instance,
            "exists": default_path.exists()
        })

        # Add other known instances from metadata
        for path_str, metadata in self._instance_metadata.items():
            path = Path(path_str)
            if path != default_path:
                instances.append({
                    "path": path_str,
                    "name": metadata.get("name", path.name),
                    "is_current": path == self.current_instance,
                    "exists": path.exists()
                })

        return instances

    def get_instance_info(self, instance_path: str | Path | None = None) -> dict[str, Any]:
        """
        Get detailed information about an instance.

        Args:
            instance_path: Path to the instance (None for current)

        Returns:
            dict: Instance information
        """
        if instance_path is None:
            instance_path = self.current_instance
        else:
            instance_path = Path(instance_path).expanduser().resolve()

        # Check instance structure
        config_path = instance_path / "config.json"
        workspace_dir = instance_path / "workspace"
        cron_dir = instance_path / "cron"
        history_dir = instance_path / "history"
        logs_dir = instance_path / "logs"
        media_dir = instance_path / "media"

        return {
            "path": str(instance_path),
            "exists": instance_path.exists(),
            "is_current": instance_path == self.current_instance,
            "config_exists": config_path.exists(),
            "structure": {
                "workspace": workspace_dir.exists(),
                "cron": cron_dir.exists(),
                "history": history_dir.exists(),
                "logs": logs_dir.exists(),
                "media": media_dir.exists()
            },
            "stats": {
                "workspace_skills": len(list(workspace_dir.glob("skills/*"))) if workspace_dir.exists() else 0,
                "cron_jobs": len(list(cron_dir.glob("*.json"))) if cron_dir.exists() else 0,
                "log_files": len(list(logs_dir.glob("*.log"))) if logs_dir.exists() else 0,
                "media_files": len(list(media_dir.glob("*.*"))) if media_dir.exists() else 0
            }
        }

    def add_instance_metadata(self, instance_path: str | Path, metadata: dict[str, Any]) -> None:
        """
        Add metadata for an instance.

        Args:
            instance_path: Path to the instance
            metadata: Metadata dictionary (e.g. {"name": "my-instance"})
        """
        path_str = str(Path(instance_path).expanduser().resolve())
        if path_str not in self._instance_metadata:
            self._instance_metadata[path_str] = {}
        self._instance_metadata[path_str].update(metadata)

    def cleanup(self) -> None:
        """Cleanup resources when shutting down."""
        self._on_switch_callbacks.clear()
        self._instance_metadata.clear()
        self.__class__._instance = None
