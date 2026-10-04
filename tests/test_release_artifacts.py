"""Tests for release artifact helper utilities."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sing_box_manager.release import artifacts
from sing_box_manager.settings import Settings


def test_resolve_release_dir_falls_back_to_legacy_versioned_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sing_box_manager.release import artifacts

    settings = Settings(sing_box_version="1.12.24")
    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)

    assert artifacts.resolve_release_dir(settings) == (
        tmp_path / "releases" / "sing-box-v1.12.24"
    )


def test_resolve_release_dir_prefers_persisted_release_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sing_box_manager.release import artifacts

    settings = Settings(sing_box_version="1.12.24")
    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)
    artifacts.write_release_info(
        tmp_path / settings.release_info_path,
        artifacts.ReleaseInfo(
            release_dir_name="20260401-9237d372_dirty",
            commit_sha="9237d372",
            dirty=True,
            date="20260401",
            upstream_version="1.12.24",
        ),
    )

    assert artifacts.resolve_release_dir(settings) == (
        tmp_path / "releases" / "20260401-9237d372_dirty"
    )


def test_resolve_deployment_host_reads_persisted_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sing_box_manager.release import artifacts

    settings = Settings(sing_box_version="1.12.24")
    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)
    artifacts.write_release_info(
        tmp_path / settings.release_info_path,
        artifacts.ReleaseInfo(
            release_dir_name="20260401-9237d372_dirty",
            commit_sha="9237d372",
            dirty=True,
            date="20260401",
            upstream_version="1.12.24",
            deployment_host="vpn.example.com",
        ),
    )

    assert artifacts.resolve_deployment_host(settings) == "vpn.example.com"


def test_build_release_info_uses_git_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sing_box_manager.release import artifacts

    class FrozenDatetime:
        @classmethod
        def now(cls):
            class _Now:
                def strftime(self, format_string: str) -> str:
                    assert format_string == "%Y%m%d"
                    return "20260401"

            return _Now()

    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs["cwd"] == tmp_path
        if command == ["git", "rev-parse", "--short=8", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="9237d372\n")
        if command == ["git", "status", "--short"]:
            return subprocess.CompletedProcess(command, 0, stdout=" M README.md\n")
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(artifacts, "datetime", FrozenDatetime)
    monkeypatch.setattr(artifacts.subprocess, "run", fake_run)

    release_info = artifacts.build_release_info("1.12.24", project_root=tmp_path)

    assert release_info == artifacts.ReleaseInfo(
        release_dir_name="20260401-9237d372_dirty",
        commit_sha="9237d372",
        dirty=True,
        date="20260401",
        upstream_version="1.12.24",
    )
    assert commands == [
        ["git", "rev-parse", "--short=8", "HEAD"],
        ["git", "status", "--short"],
    ]


def test_write_release_info_persists_upstream_version(tmp_path: Path) -> None:
    from sing_box_manager.release.artifacts import (
        ReleaseInfo,
        read_release_info,
        write_release_info,
    )

    release_info_path = tmp_path / "config" / "generated" / "release-info.json"
    release_info = ReleaseInfo(
        release_dir_name="20260401-9237d372",
        commit_sha="9237d372",
        dirty=False,
        date="20260401",
        upstream_version="1.12.24",
    )

    write_release_info(release_info_path, release_info)

    assert json.loads(release_info_path.read_text(encoding="utf-8")) == {
        "release_dir_name": "20260401-9237d372",
        "commit_sha": "9237d372",
        "dirty": False,
        "date": "20260401",
        "upstream_version": "1.12.24",
    }
    assert read_release_info(release_info_path) == release_info


def test_release_info_round_trips_client_build_id(tmp_path: Path) -> None:
    client_build_id = "d" * 64
    release_info = artifacts.ReleaseInfo(
        release_dir_name="rel-cafebabe1234",
        commit_sha="9237d372",
        dirty=False,
        date="20260401",
        upstream_version="1.13.19",
        release_id="c" * 64,
        client_build_id=client_build_id,
    )
    path = tmp_path / "release-info.json"

    artifacts.write_release_info(path, release_info)

    assert artifacts.read_release_info(path).client_build_id == client_build_id


def test_deployment_info_round_trips(tmp_path: Path) -> None:
    deployment_info = artifacts.DeploymentInfo(
        release_id="c" * 64,
        commit_sha="9237d372",
        dirty=False,
        deployed_at="2026-08-24T09:30:00Z",
        client_build_id="d" * 64,
    )
    path = tmp_path / "deployment-info.json"

    artifacts.write_deployment_info(path, deployment_info)

    assert artifacts.read_deployment_info(path) == deployment_info


def test_list_user_archives_returns_only_top_level_user_packages(
    tmp_path: Path,
) -> None:
    from sing_box_manager.release.artifacts import list_user_archives

    valid_tar = tmp_path / "sing-box-1.12.24-alice-linux-amd64.tar.gz"
    valid_zip = tmp_path / "sing-box-1.12.24-alice-windows-amd64.zip"
    other_user = tmp_path / "sing-box-1.12.24-bob-linux-amd64.tar.gz"
    server_archive = tmp_path / "sing-box-1.12.24-alice-server.tar.gz"
    upstream_archive = tmp_path / "sing-box-1.12.24-linux-amd64.tar.gz"
    nested_dir = tmp_path / "nested"
    nested_archive = nested_dir / "sing-box-1.12.24-alice-darwin-amd64.zip"

    valid_tar.write_text("alice tar", encoding="utf-8")
    valid_zip.write_text("alice zip", encoding="utf-8")
    other_user.write_text("bob tar", encoding="utf-8")
    server_archive.write_text("server tar", encoding="utf-8")
    upstream_archive.write_text("upstream tar", encoding="utf-8")
    nested_dir.mkdir()
    nested_archive.write_text("nested zip", encoding="utf-8")

    assert list_user_archives(tmp_path, "alice") == [valid_tar, valid_zip]


def test_list_user_archives_requires_exact_username_boundary(tmp_path: Path) -> None:
    from sing_box_manager.release.artifacts import list_user_archives

    known_usernames = ["alice", "linux", "alice-linux"]
    alice_archive = tmp_path / "sing-box-1.12.24-alice-linux-amd64.tar.gz"
    linux_archive = tmp_path / "sing-box-1.12.24-linux-linux-amd64.tar.gz"
    alice_linux_archive = tmp_path / "sing-box-1.12.24-alice-linux-linux-amd64.tar.gz"
    alice_archive.write_text("alice tar", encoding="utf-8")
    linux_archive.write_text("linux tar", encoding="utf-8")
    alice_linux_archive.write_text("alice-linux tar", encoding="utf-8")

    assert list_user_archives(tmp_path, "alice", known_usernames) == [alice_archive]
    assert list_user_archives(tmp_path, "linux", known_usernames) == [linux_archive]
    assert list_user_archives(tmp_path, "alice-linux", known_usernames) == [
        alice_linux_archive
    ]


def test_resolve_user_archive_returns_matching_archive_or_none(tmp_path: Path) -> None:
    from sing_box_manager.release.artifacts import resolve_user_archive

    valid_archive = tmp_path / "sing-box-1.12.24-alice-linux-amd64.tar.gz"
    valid_archive.write_text("alice tar", encoding="utf-8")
    (tmp_path / "sing-box-1.12.24-bob-linux-amd64.tar.gz").write_text(
        "bob tar", encoding="utf-8"
    )
    (tmp_path / "sing-box-1.12.24-alice-server.tar.gz").write_text(
        "server tar", encoding="utf-8"
    )

    assert resolve_user_archive(tmp_path, "alice", valid_archive.name) == valid_archive
    assert (
        resolve_user_archive(
            tmp_path, "alice", "sing-box-1.12.24-bob-linux-amd64.tar.gz"
        )
        is None
    )
    assert (
        resolve_user_archive(tmp_path, "alice", "sing-box-1.12.24-alice-server.tar.gz")
        is None
    )
    assert (
        resolve_user_archive(
            tmp_path, "alice", "../sing-box-1.12.24-alice-linux-amd64.tar.gz"
        )
        is None
    )


def test_resolve_user_archive_for_platform_returns_matching_archive(
    tmp_path: Path,
) -> None:
    from sing_box_manager.release.artifacts import resolve_user_archive_for_platform

    linux_archive = tmp_path / "sing-box-1.12.24-alice-linux-amd64.tar.gz"
    windows_archive = tmp_path / "sing-box-1.12.24-alice-windows-amd64.zip"
    android_archive = tmp_path / "sing-box-1.12.24-alice-android-arm64.zip"
    linux_archive.write_text("alice tar", encoding="utf-8")
    windows_archive.write_text("alice zip", encoding="utf-8")
    android_archive.write_text("alice android zip", encoding="utf-8")

    assert (
        resolve_user_archive_for_platform(tmp_path, "alice", "linux-amd64")
        == linux_archive
    )
    assert (
        resolve_user_archive_for_platform(tmp_path, "alice", "windows-amd64")
        == windows_archive
    )
    assert (
        resolve_user_archive_for_platform(tmp_path, "alice", "android-arm64")
        == android_archive
    )


def test_resolve_user_archive_for_platform_distinguishes_macos_architectures(
    tmp_path: Path,
) -> None:
    """darwin, linux, and android arm64 archives all share the arm64 suffix."""
    from sing_box_manager.release.artifacts import resolve_user_archive_for_platform

    darwin_arm = tmp_path / "sing-box-1.12.24-alice-darwin-arm64.tar.gz"
    darwin_amd = tmp_path / "sing-box-1.12.24-alice-darwin-amd64.tar.gz"
    linux_arm = tmp_path / "sing-box-1.12.24-alice-linux-arm64.tar.gz"
    darwin_arm.write_text("alice darwin arm", encoding="utf-8")
    darwin_amd.write_text("alice darwin amd", encoding="utf-8")
    linux_arm.write_text("alice linux arm", encoding="utf-8")

    assert (
        resolve_user_archive_for_platform(tmp_path, "alice", "darwin-arm64")
        == darwin_arm
    )
    assert (
        resolve_user_archive_for_platform(tmp_path, "alice", "darwin-amd64")
        == darwin_amd
    )
    assert (
        resolve_user_archive_for_platform(tmp_path, "alice", "linux-arm64") == linux_arm
    )


def test_is_user_archive_accepts_macos_archives() -> None:
    from sing_box_manager.release.artifacts import is_user_archive

    assert is_user_archive("sing-box-1.12.24-alice-darwin-arm64.tar.gz", "alice")
    assert is_user_archive("sing-box-1.12.24-alice-darwin-amd64.tar.gz", "alice")
    assert not is_user_archive("sing-box-1.12.24-bob-darwin-arm64.tar.gz", "alice")
    assert not is_user_archive("sing-box-1.12.24-darwin-arm64.tar.gz", "alice")


def test_resolve_user_archive_for_platform_returns_none_for_unknown_platform(
    tmp_path: Path,
) -> None:
    from sing_box_manager.release.artifacts import resolve_user_archive_for_platform

    (tmp_path / "sing-box-1.12.24-alice-linux-amd64.tar.gz").write_text(
        "alice tar", encoding="utf-8"
    )

    assert resolve_user_archive_for_platform(tmp_path, "alice", "darwin-amd64") is None


def test_resolve_user_archive_for_platform_returns_none_for_other_users_file(
    tmp_path: Path,
) -> None:
    from sing_box_manager.release.artifacts import resolve_user_archive_for_platform

    (tmp_path / "sing-box-1.12.24-bob-linux-arm64.tar.gz").write_text(
        "bob tar", encoding="utf-8"
    )

    assert resolve_user_archive_for_platform(tmp_path, "alice", "linux-arm64") is None


def test_resolve_user_archive_for_platform_requires_exact_username_boundary(
    tmp_path: Path,
) -> None:
    from sing_box_manager.release.artifacts import resolve_user_archive_for_platform

    known_usernames = ["alice", "linux", "alice-linux"]
    alice_archive = tmp_path / "sing-box-1.12.24-alice-linux-amd64.tar.gz"
    linux_archive = tmp_path / "sing-box-1.12.24-linux-linux-amd64.tar.gz"
    alice_linux_archive = tmp_path / "sing-box-1.12.24-alice-linux-linux-amd64.tar.gz"
    alice_archive.write_text("alice tar", encoding="utf-8")
    linux_archive.write_text("linux tar", encoding="utf-8")
    alice_linux_archive.write_text("alice-linux tar", encoding="utf-8")

    assert (
        resolve_user_archive_for_platform(
            tmp_path,
            "alice",
            "linux-amd64",
            known_usernames,
        )
        == alice_archive
    )
    assert (
        resolve_user_archive_for_platform(
            tmp_path,
            "linux",
            "linux-amd64",
            known_usernames,
        )
        == linux_archive
    )
    assert (
        resolve_user_archive_for_platform(
            tmp_path,
            "alice-linux",
            "linux-amd64",
            known_usernames,
        )
        == alice_linux_archive
    )
