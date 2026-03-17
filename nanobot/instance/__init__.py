"""
Hot-pluggable instance management for nanobot.

This module provides functionality to run and switch between multiple
complete nanobot instances at runtime, each with its own configuration,
cron jobs, history, media, and workspace.
"""

from nanobot.instance.manager import InstanceManager

__all__ = ["InstanceManager"]
