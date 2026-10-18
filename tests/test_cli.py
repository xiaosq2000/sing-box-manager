"""Tests for CLI serve command wiring."""

import shlex
import sys
from pathlib import Path
from typing import get_args

import pytest

from sing_box_manager.cli import (
    ServeCommand,
    StatsCollectCommand,
    _run_serve,
    _run_stats_collect,
    main,
)
from sing_box_manager.control_plane.runner import Action, normalize_action
from sing_box_manager.settings import Settings, WebSettings
from sing_box_manager.traffic_stats import TrafficStatsCollectionResult


def _build_settings() -> Settings:
    return Settings(
        sing_box_version="1.0.0",
        web=WebSettings(
            port=8080,
            host="0.0.0.0",
            session_secret="test-secret",
            allowed_hosts=["localhost"],
        ),
        config_root=Path("./config"),
    )


@pytest.mark.parametrize("port", [None, 9090])
def test_serve_delegates_to_the_single_django_service(monkeypatch, port):
    calls = []
    monkeypatch.setattr("sing_box_manager.control_plane.runner.run", lambda *args: calls.append(args))
    runtime = _build_settings()
    _run_serve(ServeCommand(port=port), runtime)
    action, served = calls[0]
    assert action == "serve"
    assert served.control_plane.port == (port or runtime.control_plane.port)


def test_serve_rejects_public_admin_binding():
    with pytest.raises(SystemExit, match="127.0.0.1"):
        _run_serve(ServeCommand(host="0.0.0.0"), _build_settings())


def test_run_stats_collect_reports_success(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "sing_box_manager.traffic_stats.collect_traffic_stats",
        lambda settings: TrafficStatsCollectionResult(
            database_path=Path("data/traffic.sqlite3"),
            successful_services=("trojan", "hysteria2"),
            failed_services=(),
        ),
    )

    _run_stats_collect(StatsCollectCommand(), _build_settings())

    captured = capsys.readouterr()
    assert "Collected traffic stats into data/traffic.sqlite3" in captured.out
    assert captured.err == ""


def test_run_gc_prints_what_it_reclaimed(monkeypatch, capsys) -> None:
    from sing_box_manager.cli import GcCommand, _run_gc
    from sing_box_manager.release.gc import GcResult

    monkeypatch.setattr(
        "sing_box_manager.release.gc.collect_garbage",
        lambda settings, *, keep, dry_run: GcResult(
            kept_releases=("rel-newer",),
            removed_releases=("rel-older",),
            removed_store_entries=("abc.tar.gz.part",),
            reclaimed_bytes=190_000_000,
            dry_run=dry_run,
        ),
    )

    _run_gc(GcCommand(keep=1), _build_settings())

    output = capsys.readouterr().out
    assert "Kept releases: rel-newer" in output
    assert "Removed releases: rel-older" in output
    assert "Removed 1 store entries" in output
    assert "190.0 MB" in output


def test_run_gc_on_a_remote_runs_the_same_verb_there(monkeypatch) -> None:
    """The VPS is the host that runs out of disk, and it now has this code."""
    from sing_box_manager.cli import GcCommand, _run_gc

    issued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "sing_box_manager.deploy.run_remote_command",
        lambda hostname, command, error: issued.append((hostname, command)),
    )

    _run_gc(GcCommand(keep=2, remote="vpn-host", dry_run=True), _build_settings())

    hostname, command = issued[0]
    assert hostname == "vpn-host"
    assert command.startswith("cd /opt/sing-box-manager && ")
    assert "/opt/sing-box-manager/.pixi/envs/default/bin/python" in command
    assert (
        "-m sing_box_manager --config config/inventory/runtime.yaml gc --keep 2 --dry-run"
        in command
    )


def test_run_serve_keeps_request_urls_out_of_the_log(monkeypatch) -> None:
    """httpx logs each request URL at INFO, and KiwiVM URLs carry the API key."""
    import logging

    monkeypatch.setattr("sing_box_manager.control_plane.runner.run", lambda *a, **k: None)
    httpx_logger = logging.getLogger("httpx")
    monkeypatch.setattr(httpx_logger, "level", logging.NOTSET)

    _run_serve(ServeCommand(), _build_settings())

    assert httpx_logger.level >= logging.WARNING


@pytest.mark.parametrize("action", get_args(Action))
@pytest.mark.parametrize("syntax", ["positional", "flag", "equals"])
def test_control_plane_actions_reach_runner(monkeypatch, action, syntax):
    settings = _build_settings()
    calls = []
    monkeypatch.setattr("sing_box_manager.settings.load_settings", lambda _: settings)
    monkeypatch.setattr(
        "sing_box_manager.control_plane.runner.run",
        lambda action, runtime: calls.append((action, runtime)),
    )
    action_args = {
        "positional": [action],
        "flag": ["--action", action],
        "equals": [f"--action={action}"],
    }[syntax]

    main(["--config", "/fixture/runtime.yaml", "control-plane", *action_args])

    assert calls == [(normalize_action(action), settings)]


@pytest.mark.parametrize("action", get_args(Action))
@pytest.mark.parametrize("syntax", ["positional", "flag"])
@pytest.mark.parametrize("options_first", [False, True])
def test_remote_control_plane_normalizes_actions_and_privileges(
    monkeypatch, action, syntax, options_first
):
    calls = []
    monkeypatch.setattr(
        "sing_box_manager.settings.load_settings", lambda _: _build_settings()
    )
    monkeypatch.setattr(
        "sing_box_manager.deploy.run_remote_command",
        lambda host, command, error: calls.append((host, command)),
    )
    action_args = [action] if syntax == "positional" else ["--action", action]
    root = "/srv/fixture's manager"
    options = ["--remote", "fixture-host", "--remote-root", root]
    args = options + action_args if options_first else action_args + options

    main(["--config", "/fixture/runtime.yaml", "control-plane", *args])

    [(host, command)] = calls
    canonical = normalize_action(action)
    assert host == "fixture-host"
    assert command.startswith(f"cd {shlex.quote(root)} && ")
    argv = shlex.split(command.split(" && ", 1)[1])
    if canonical in {"collect-static", "sync-node"}:
        assert argv[:3] == ["sudo", "env", "PYTHONDONTWRITEBYTECODE=1"]
    else:
        assert argv[:5] == ["sudo", "-u", "sbm", "env", "PYTHONDONTWRITEBYTECODE=1"]
    assert argv[-8:] == [
        f"{root}/.pixi/envs/default/bin/python",
        "-m",
        "sing_box_manager",
        "--config",
        f"{root}/config/inventory/runtime.yaml",
        "control-plane",
        "--action",
        canonical,
    ]


def test_control_plane_defaults_to_check_with_environment_config(monkeypatch):
    calls = []
    monkeypatch.setenv("SBM_CONFIG", "/fixture/runtime.yaml")
    monkeypatch.setattr(
        "sing_box_manager.settings.load_settings", lambda _: _build_settings()
    )
    monkeypatch.setattr(
        "sing_box_manager.control_plane.runner.run",
        lambda action, runtime: calls.append(action),
    )
    monkeypatch.setattr(sys, "argv", ["sbm", "control-plane"])

    main()

    assert calls == ["check"]


@pytest.mark.parametrize("args", [["unknown"], ["--action", "unknown"]])
def test_control_plane_rejects_unknown_actions_before_loading_settings(
    monkeypatch, args
):
    def unexpected_load(_):
        pytest.fail("Invalid actions must not load settings")

    monkeypatch.setattr("sing_box_manager.settings.load_settings", unexpected_load)
    with pytest.raises(SystemExit) as error:
        main(["--config", "/fixture/runtime.yaml", "control-plane", *args])
    assert error.value.code == 2


def test_control_plane_rejects_both_action_forms(monkeypatch, capsys):
    monkeypatch.setattr(
        "sing_box_manager.settings.load_settings", lambda _: _build_settings()
    )
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--config",
                "/fixture/runtime.yaml",
                "control-plane",
                "sync",
                "--action",
                "check",
            ]
        )
    assert error.value.code == 2
    assert "not both" in capsys.readouterr().err


def test_control_plane_help_documents_both_action_forms(monkeypatch, capsys):
    monkeypatch.delenv("SBM_CONFIG", raising=False)
    with pytest.raises(SystemExit) as error:
        main(["control-plane", "--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "positional arguments" in output
    assert "--action" in output
    assert "createsuperuser" in output
    assert "import" in output
    assert "sync" in output
