from pathlib import Path
from unittest.mock import MagicMock

from nanobot.config.paths import (
    get_bridge_install_dir,
    get_cli_history_path,
    get_cron_dir,
    get_data_dir,
    get_legacy_sessions_dir,
    get_logs_dir,
    get_media_dir,
    get_runtime_subdir,
    get_workspace_path,
)


def test_runtime_dirs_follow_config_path(monkeypatch, tmp_path: Path) -> None:
    instance_path = tmp_path / "instance-a"
    
    mock_instance = MagicMock()
    mock_instance.current_instance = instance_path
    mock_instance.get_path.side_effect = lambda name: instance_path / name
    
    monkeypatch.setattr("nanobot.config.paths.InstanceManager.get", lambda: mock_instance)

    assert get_data_dir() == instance_path
    assert get_runtime_subdir("cron") == instance_path / "cron"
    assert get_cron_dir() == instance_path / "cron"
    assert get_logs_dir() == instance_path / "logs"


def test_media_dir_supports_channel_namespace(monkeypatch, tmp_path: Path) -> None:
    instance_path = tmp_path / "instance-b"

    mock_instance = MagicMock()
    mock_instance.current_instance = instance_path
    mock_instance.get_path.side_effect = lambda name: instance_path / name
    
    monkeypatch.setattr("nanobot.config.paths.InstanceManager.get", lambda: mock_instance)

    assert get_media_dir() == instance_path / "media"
    assert get_media_dir("telegram") == instance_path / "media" / "telegram"


def test_shared_and_legacy_paths_remain_global() -> None:
    assert get_cli_history_path() == Path.home() / ".nanobot" / "history" / "cli_history"
    assert get_bridge_install_dir() == Path.home() / ".nanobot" / "bridge"
    assert get_legacy_sessions_dir() == Path.home() / ".nanobot" / "sessions"


def test_workspace_path_is_explicitly_resolved() -> None:
    assert get_workspace_path() == Path.home() / ".nanobot" / "workspace"
    assert get_workspace_path("~/custom-workspace") == Path.home() / "custom-workspace"
