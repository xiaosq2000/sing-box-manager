"""Regression tests for web app startup auth loading."""

import json
import os
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from sing_box_manager.release.artifacts import ReleaseInfo, write_release_info
from sing_box_manager.settings import Settings, TrafficStatsSettings, WebSettings
from sing_box_manager.web.app import (
    _build_versioned_static_asset_url,
    _resolve_allowed_hosts,
    create_app,
)


def _build_settings(
    *,
    config_root: Path | str,
    releases_root: Path | str = Path("./releases"),
) -> Settings:
    return Settings(
        sing_box_version="1.0.0",
        web=WebSettings(
            port=8080,
            host="127.0.0.1",
            session_secret="test-secret",
            allowed_hosts=["localhost", "testserver"],
        ),
        config_root=Path(config_root),
        releases_root=Path(releases_root),
    )


def _create_release_root(base_dir: Path, config_root: Path | None = None) -> Path:
    release_root = base_dir / "releases"
    release_dir = release_root / "20260401-9237d372_dirty"
    release_dir.mkdir(parents=True, exist_ok=True)
    write_release_info(
        (config_root or base_dir / "config") / "generated" / "release-info.json",
        ReleaseInfo(
            release_dir_name=release_dir.name,
            commit_sha="9237d372",
            dirty=True,
            date="20260401",
            upstream_version="1.0.0",
            deployment_host="downloads.example.com",
        ),
    )
    return release_root


class TestCreateAppAuthSnapshot:
    def test_resolve_allowed_hosts_appends_release_info_host(
        self, tmp_path: Path
    ) -> None:
        settings = _build_settings(config_root=tmp_path)

        assert _resolve_allowed_hosts(settings, "downloads.example.com") == [
            "localhost",
            "testserver",
            "downloads.example.com",
        ]

    def test_resolve_allowed_hosts_deduplicates_release_info_host(
        self, tmp_path: Path
    ) -> None:
        settings = Settings(
            sing_box_version="1.0.0",
            web=WebSettings(
                port=8080,
                host="127.0.0.1",
                session_secret="test-secret",
                allowed_hosts=["localhost", "127.0.0.1"],
            ),
            config_root=tmp_path,
        )

        assert _resolve_allowed_hosts(settings, "localhost") == [
            "localhost",
            "127.0.0.1",
        ]

    def test_create_app_trusts_deployment_host_from_release_info(
        self,
        tmp_path: Path,
        portal_passwords: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from fastapi.testclient import TestClient

        from sing_box_manager.release import artifacts

        project_root = tmp_path / "project-root"
        snapshot_file = project_root / "config" / "generated" / "auth-users.json"
        snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        password_hasher = PasswordHasher()
        snapshot_file.write_text(
            json.dumps(
                {
                    "users": [
                        {
                            "username": "alice",
                            "password_hash": password_hasher.hash(
                                portal_passwords["alice"]
                            ),
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(artifacts, "PROJECT_ROOT", project_root)
        monkeypatch.chdir(tmp_path)
        _create_release_root(project_root, config_root=project_root / "config")

        app = create_app(_build_settings(config_root=Path("./config")))
        response = TestClient(app).get("/", headers={"host": "downloads.example.com"})

        assert response.status_code == 200

    def test_create_app_stores_runtime_vps_hostname(
        self,
        tmp_path: Path,
        portal_passwords: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sing_box_manager.release import artifacts
        from sing_box_manager.web import app as web_app

        project_root = tmp_path / "project-root"
        snapshot_file = project_root / "config" / "generated" / "auth-users.json"
        snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        password_hasher = PasswordHasher()
        snapshot_file.write_text(
            json.dumps(
                {
                    "users": [
                        {
                            "username": "alice",
                            "password_hash": password_hasher.hash(
                                portal_passwords["alice"]
                            ),
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(artifacts, "PROJECT_ROOT", project_root)
        monkeypatch.setattr(web_app.socket, "gethostname", lambda: "vps-host")
        monkeypatch.chdir(tmp_path)
        _create_release_root(project_root, config_root=project_root / "config")

        app = create_app(_build_settings(config_root=Path("./config")))

        assert app.state.vps_hostname == "vps-host"

    def test_create_app_resolves_project_relative_resources_from_project_root(
        self,
        tmp_path: Path,
        portal_passwords: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sing_box_manager.release import artifacts
        from sing_box_manager.web.app import _WEB_DIR

        project_root = tmp_path / "project-root"
        snapshot_file = project_root / "config" / "generated" / "auth-users.json"
        snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        password_hasher = PasswordHasher()
        snapshot_file.write_text(
            json.dumps(
                {
                    "users": [
                        {
                            "username": "alice",
                            "password_hash": password_hasher.hash(
                                portal_passwords["alice"]
                            ),
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(artifacts, "PROJECT_ROOT", project_root)
        monkeypatch.chdir(tmp_path)
        _create_release_root(project_root, config_root=project_root / "config")

        app = create_app(_build_settings(config_root=Path("./config")))

        assert (
            app.state.release_dir
            == project_root / "releases" / "20260401-9237d372_dirty"
        )
        assert app.state.users.keys() == {"alice"}
        assert app.state.templates.env.loader.searchpath == [
            os.fspath(_WEB_DIR / "templates")
        ]
        assert app.state.portal_stylesheet_url == _build_versioned_static_asset_url(
            "styles.css"
        )
        assert (
            app.state.templates.env.globals["portal_stylesheet_url"]
            == app.state.portal_stylesheet_url
        )
        static_route = next(
            route for route in app.routes if getattr(route, "name", None) == "static"
        )
        assert static_route.app.directory == _WEB_DIR / "static"

    def test_create_app_keeps_absolute_auth_snapshot_path_unchanged(
        self,
        tmp_path: Path,
        portal_passwords: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sing_box_manager.release import artifacts

        project_root = tmp_path / "project-root"
        config_root = tmp_path / "external-config"
        absolute_snapshot = config_root / "generated" / "auth-users.json"
        absolute_snapshot.parent.mkdir(parents=True, exist_ok=True)
        password_hasher = PasswordHasher()
        absolute_snapshot.write_text(
            json.dumps(
                {
                    "users": [
                        {
                            "username": "bob",
                            "password_hash": password_hasher.hash(
                                portal_passwords["bob"]
                            ),
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(artifacts, "PROJECT_ROOT", project_root)
        monkeypatch.chdir(tmp_path)
        _create_release_root(project_root, config_root=config_root)

        app = create_app(_build_settings(config_root=config_root))

        assert app.state.users.keys() == {"bob"}

    def test_create_app_resolves_release_dir_from_project_root(
        self,
        tmp_path: Path,
        auth_snapshot_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sing_box_manager.release import artifacts

        project_root = tmp_path / "project-root"
        monkeypatch.setattr(artifacts, "PROJECT_ROOT", project_root)
        _create_release_root(project_root, config_root=auth_snapshot_path.parents[1])

        app = create_app(_build_settings(config_root=auth_snapshot_path.parents[1]))

        assert (
            app.state.release_dir
            == project_root / "releases" / "20260401-9237d372_dirty"
        )

    def test_create_app_sets_traffic_stats_provider_when_enabled(
        self,
        tmp_path: Path,
        auth_snapshot_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sing_box_manager.release import artifacts

        project_root = tmp_path / "project-root"
        monkeypatch.setattr(artifacts, "PROJECT_ROOT", project_root)
        _create_release_root(project_root, config_root=auth_snapshot_path.parents[1])

        app = create_app(
            Settings(
                sing_box_version="1.0.0",
                web=WebSettings(
                    port=8080,
                    host="127.0.0.1",
                    session_secret="test-secret",
                    allowed_hosts=["localhost", "testserver"],
                ),
                config_root=auth_snapshot_path.parents[1],
                traffic_stats=TrafficStatsSettings(enabled=True),
            )
        )

        assert app.state.traffic_stats_provider is not None

    def test_create_app_fails_when_auth_snapshot_missing(self, tmp_path: Path) -> None:
        missing_config_root = tmp_path / "missing-config"
        release_root = _create_release_root(tmp_path, config_root=missing_config_root)

        with pytest.raises(FileNotFoundError):
            create_app(
                _build_settings(
                    config_root=missing_config_root,
                    releases_root=release_root,
                )
            )

    def test_create_app_fails_when_release_dir_missing(
        self,
        tmp_path: Path,
        auth_snapshot_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sing_box_manager.release import artifacts

        project_root = tmp_path / "project-root"
        monkeypatch.setattr(artifacts, "PROJECT_ROOT", project_root)

        with pytest.raises(RuntimeError, match="Release directory not found"):
            create_app(_build_settings(config_root=auth_snapshot_path.parents[1]))

    def test_create_app_fails_when_auth_snapshot_is_invalid_json(
        self, tmp_path: Path
    ) -> None:
        config_root = tmp_path / "config"
        release_root = _create_release_root(tmp_path)
        snapshot_file = config_root / "generated" / "auth-users.json"
        snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        snapshot_file.write_text("{not json}\n", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            create_app(
                _build_settings(config_root=config_root, releases_root=release_root)
            )

    def test_create_app_fails_when_auth_snapshot_is_unreadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_root = tmp_path / "config"
        release_root = _create_release_root(tmp_path)
        snapshot_file = config_root / "generated" / "auth-users.json"
        snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        snapshot_file.write_text(
            json.dumps(
                {
                    "users": [
                        {
                            "username": "alice",
                            "password_hash": PasswordHasher().hash(
                                "alice-portal-pass-123"
                            ),
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        original_read_text = Path.read_text

        def read_text(self: Path, *args, **kwargs) -> str:
            if self == snapshot_file:
                raise PermissionError(f"Permission denied: {self}")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)

        with pytest.raises(PermissionError):
            create_app(
                _build_settings(config_root=config_root, releases_root=release_root)
            )

    def test_create_app_fails_when_auth_snapshot_file_is_empty(
        self, tmp_path: Path
    ) -> None:
        config_root = tmp_path / "config"
        release_root = _create_release_root(tmp_path)
        snapshot_file = config_root / "generated" / "auth-users.json"
        snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        snapshot_file.write_text("", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            create_app(
                _build_settings(config_root=config_root, releases_root=release_root)
            )

    def test_create_app_fails_when_auth_snapshot_has_no_users(
        self, tmp_path: Path
    ) -> None:
        config_root = tmp_path / "config"
        release_root = _create_release_root(tmp_path)
        snapshot_file = config_root / "generated" / "auth-users.json"
        snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        snapshot_file.write_text('{"users": []}\n', encoding="utf-8")

        with pytest.raises(RuntimeError, match="No enabled auth users"):
            create_app(
                _build_settings(config_root=config_root, releases_root=release_root)
            )

    def test_create_app_fails_when_auth_snapshot_has_no_enabled_users(
        self, tmp_path: Path, portal_passwords: dict[str, str]
    ) -> None:
        config_root = tmp_path / "config"
        release_root = _create_release_root(tmp_path)
        snapshot_file = config_root / "generated" / "auth-users.json"
        snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        password_hasher = PasswordHasher()
        snapshot_file.write_text(
            json.dumps(
                {
                    "users": [
                        {
                            "username": "alice",
                            "password_hash": password_hasher.hash(
                                portal_passwords["alice"]
                            ),
                            "enabled": False,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="No enabled auth users"):
            create_app(
                _build_settings(config_root=config_root, releases_root=release_root)
            )


def test_every_install_route_is_marked_no_store(client):
    """The macOS installer shipped cacheable because this list was updated by hand.

    Asserted against real responses rather than a constant, so the guarantee holds
    however the policy is implemented.
    """
    install_paths = {
        route.path
        for route in client.app.routes
        if getattr(route, "path", "").startswith("/install")
    }

    assert install_paths, "no /install routes found"
    cacheable = sorted(
        path
        for path in install_paths
        if client.get(path).headers.get("cache-control") != "no-store"
    )
    assert not cacheable, (
        f"these installer routes would be cacheable by intermediaries: {cacheable}"
    )


def test_parameterized_routes_are_not_cacheable(client):
    """An exact-path allowlist could never cover these; a default-deny policy does."""
    for path in ("/files/does-not-exist.zip", "/admin/users/nobody", "/nope"):
        response = client.get(path)
        assert response.headers.get("cache-control") == "no-store", (
            f"{path} responded {response.status_code} without no-store"
        )


def test_static_assets_stay_cacheable(client):
    """Inverting the default must not turn the stylesheet into a no-store response."""
    response = client.get("/static/styles.css")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache, must-revalidate"


def test_cross_origin_requests_get_no_cors_grant(client):
    """No page is meant to be read by another origin, least of all with cookies."""
    headers = {"Origin": "https://evil.example.com"}

    page = client.get("/", headers=headers, cookies={"session": "anything"})
    preflight = client.options(
        "/api/usage",
        headers={**headers, "Access-Control-Request-Method": "GET"},
    )

    for response in (page, preflight):
        assert "access-control-allow-origin" not in response.headers
        assert "access-control-allow-credentials" not in response.headers


def test_create_app_requires_a_session_secret(tmp_path: Path) -> None:
    """A generated secret would sign everyone out at every restart."""
    settings = Settings(
        sing_box_version="1.0.0",
        web=WebSettings(allowed_hosts=["localhost"]),
        config_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="session_secret"):
        create_app(settings)
