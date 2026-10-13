"""Reject invalid managed deployment layouts before contacting a host."""

from pathlib import Path

import pytest

from sing_box_manager.control_plane.deployment import validate_deployment
from sing_box_manager.deploy import DeploymentError, _build_remote
from sing_box_manager.settings import (
    ControlPlaneSettings,
    Settings,
    TrafficStatsSettings,
)


@pytest.mark.parametrize(
    "root", ["/home/operator/project", "/root/project", "/run/user/1000/project"]
)
def test_protected_home_roots_are_rejected(root):
    with pytest.raises(DeploymentError, match="protected home"):
        validate_deployment(root, Settings())


@pytest.mark.parametrize("config_root", [Path("/tmp/config"), Path("../config")])
def test_generated_files_cannot_escape_deploy_root(config_root):
    with pytest.raises(DeploymentError, match="parent traversal"):
        validate_deployment("/opt/sing-box-manager", Settings(config_root=config_root))


def test_skip_code_does_not_validate_control_plane_home_layout():
    settings = Settings(control_plane=ControlPlaneSettings(enabled=True))
    remote = _build_remote(
        "fixture-host",
        "/home/operator/project",
        settings,
        release_dir_name="fixture",
        manage_control_plane=False,
    )
    assert remote.root == "/home/operator/project"


@pytest.mark.parametrize("port", [5432, 19080, 19090])
def test_private_http_port_cannot_collide_with_database_or_stats(port):
    with pytest.raises(ValueError, match="must differ"):
        Settings(
            control_plane=ControlPlaneSettings(enabled=True, port=port),
            traffic_stats=TrafficStatsSettings(enabled=True),
        )
