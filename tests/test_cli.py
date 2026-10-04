"""Tests for CLI serve command wiring."""

from pathlib import Path

from sing_box_manager.cli import (
    StatsCollectCommand,
    ServeCommand,
    _run_serve,
    _run_stats_collect,
)
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


def test_run_serve_uses_settings_host_when_cli_host_is_unset(
    monkeypatch,
) -> None:
    import sing_box_manager.web.app as web_app_module

    captured: dict[str, object] = {}
    app = object()

    monkeypatch.setattr(web_app_module, "create_app", lambda settings: app)

    def fake_run(
        application,
        *,
        host: str,
        port: int,
        proxy_headers: bool,
        forwarded_allow_ips: str,
    ) -> None:
        captured.update(
            {
                "application": application,
                "host": host,
                "port": port,
                "proxy_headers": proxy_headers,
                "forwarded_allow_ips": forwarded_allow_ips,
            }
        )

    monkeypatch.setattr("sing_box_manager.cli.uvicorn.run", fake_run)

    _run_serve(ServeCommand(), _build_settings())

    assert captured == {
        "application": app,
        "host": "0.0.0.0",
        "port": 8080,
        "proxy_headers": True,
        "forwarded_allow_ips": "127.0.0.1",
    }


def test_run_serve_prefers_cli_host_override(monkeypatch) -> None:
    import sing_box_manager.web.app as web_app_module

    captured: dict[str, object] = {}

    monkeypatch.setattr(web_app_module, "create_app", lambda settings: object())

    def fake_run(
        application,
        *,
        host: str,
        port: int,
        proxy_headers: bool,
        forwarded_allow_ips: str,
    ) -> None:
        captured.update(
            {
                "host": host,
                "port": port,
                "proxy_headers": proxy_headers,
                "forwarded_allow_ips": forwarded_allow_ips,
            }
        )

    monkeypatch.setattr("sing_box_manager.cli.uvicorn.run", fake_run)

    _run_serve(ServeCommand(host="127.0.0.1", port=9090), _build_settings())

    assert captured == {
        "host": "127.0.0.1",
        "port": 9090,
        "proxy_headers": True,
        "forwarded_allow_ips": "127.0.0.1",
    }


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
    assert "-m sing_box_manager --config config/inventory/runtime.yaml gc --keep 2 --dry-run" in command


def test_run_serve_keeps_request_urls_out_of_the_log(monkeypatch) -> None:
    """httpx logs each request URL at INFO, and KiwiVM URLs carry the API key."""
    import logging

    import sing_box_manager.web.app as web_app_module

    monkeypatch.setattr(web_app_module, "create_app", lambda settings: object())
    monkeypatch.setattr("sing_box_manager.cli.uvicorn.run", lambda *a, **k: None)
    httpx_logger = logging.getLogger("httpx")
    monkeypatch.setattr(httpx_logger, "level", logging.NOTSET)

    _run_serve(ServeCommand(), _build_settings())

    assert httpx_logger.level >= logging.WARNING


def test_run_serve_redacts_subscription_tokens_from_access_logs(monkeypatch) -> None:
    import logging

    import sing_box_manager.web.app as web_app_module
    from sing_box_manager.subscription import TokenRedactingFilter

    monkeypatch.setattr(web_app_module, "create_app", lambda settings: object())
    monkeypatch.setattr("sing_box_manager.cli.uvicorn.run", lambda *a, **k: None)
    access_logger = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access_logger, "filters", [])

    _run_serve(ServeCommand(), _build_settings())

    assert any(isinstance(f, TokenRedactingFilter) for f in access_logger.filters)
