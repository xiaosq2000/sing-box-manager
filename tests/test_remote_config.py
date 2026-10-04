"""Tests for safely publishing the server's derived runtime config."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from sing_box_manager.remote_config import publish_runtime_config


def _write_yaml(path: Path, data: object, mode: int = 0o600) -> None:
    path.write_text(yaml.dump(data, sort_keys=False), encoding="utf-8")
    path.chmod(mode)


def test_publish_updates_config_but_preserves_server_session_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "runtime.yaml"
    staged = tmp_path / "runtime.staged.yaml"
    _write_yaml(
        target,
        {
            "web": {"session_secret": "server-secret"},
            "vps_info": {"kiwi_veid": "old", "kiwi_api_key": "old"},
        },
        mode=0o640,
    )
    _write_yaml(
        staged,
        {
            "web": {"session_secret": "new-local-secret"},
            "vps_info": {"kiwi_veid": "new", "kiwi_api_key": "new"},
        },
    )
    ownership: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "sing_box_manager.remote_config.os.chown",
        lambda _path, uid, gid: ownership.append((uid, gid)),
    )

    publish_runtime_config(staged, target, tmp_path)

    published = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert published["web"]["session_secret"] == "server-secret"
    assert published["vps_info"] == {
        "kiwi_veid": "new",
        "kiwi_api_key": "new",
    }
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert ownership == [(target.stat().st_uid, target.stat().st_gid)]
    assert not staged.exists()


def test_publish_keeps_the_server_subscription_secret_or_adopts_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sing_box_manager.remote_config.os.chown", lambda *_: None)
    target = tmp_path / "runtime.yaml"
    staged = tmp_path / "runtime.staged.yaml"
    # A server bootstrapped before subscription links has only a session secret.
    _write_yaml(target, {"web": {"session_secret": "server-session"}}, mode=0o640)
    _write_yaml(
        staged,
        {"web": {"session_secret": "local", "subscription_secret": "generated"}},
    )

    publish_runtime_config(staged, target, tmp_path)
    _write_yaml(
        staged,
        {"web": {"session_secret": "local", "subscription_secret": "regenerated"}},
    )
    publish_runtime_config(staged, target, tmp_path)

    published = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert published["web"] == {
        "session_secret": "server-session",
        "subscription_secret": "generated",
    }


def test_publish_rejects_invalid_staged_config_without_touching_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "runtime.yaml"
    staged = tmp_path / "runtime.staged.yaml"
    original = "web:\n  session_secret: server-secret\n"
    target.write_text(original, encoding="utf-8")
    staged.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must be a mapping"):
        publish_runtime_config(staged, target, tmp_path)

    assert target.read_text(encoding="utf-8") == original


def test_a_new_config_is_readable_by_the_service_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root owns it and the service account reads it through its group."""
    target = tmp_path / "runtime.yaml"
    staged = tmp_path / "runtime.staged.yaml"
    _write_yaml(staged, {"web": {"session_secret": "local-secret"}})
    monkeypatch.setattr(
        "sing_box_manager.remote_config.os.chown", lambda *_args: None
    )

    publish_runtime_config(staged, target, tmp_path)

    assert stat.S_IMODE(target.stat().st_mode) == 0o640
