"""
Hot-pluggable workspace management for nanobot.

This module provides functionality to switch between different workspaces
at runtime, allowing users to maintain separate environments with their
own skills, memory, and conversation sessions.
"""

from nanobot.workspace.manager import WorkspaceManager

__all__ = ["WorkspaceManager"]
