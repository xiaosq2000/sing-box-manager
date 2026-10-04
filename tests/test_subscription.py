"""Subscription tokens, the envelope, and the endpoint sbc fetches it from."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sing_box_manager.settings import Settings
from sing_box_manager.subscription import (
    TOKEN_PATTERN,
    ResetCounters,
    TokenRedactingFilter,
    find_subscriber,
    minimum_sing_box_version,
    parse_version,
    subscription_token,
)
from sing_box_manager.traffic_models import UserTrafficSummary
from sing_box_manager.web.app import create_app

SECRET = "subscription-secret"
PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQxMjM0NTY$c29tZWhhc2h2YWx1ZXNvbWVoaXN0b3J5"
QUERY = {"format": "sbc", "os": "linux", "sing-box": "1.14.2"}


def test_tokens_are_22_url_safe_characters_that_change_with_every_input() -> None:
    token = subscription_token(SECRET, "alice")
    assert TOKEN_PATTERN.fullmatch(token)
    assert token == subscription_token(SECRET, "alice", 0)
    assert (
        len(
            {
                token,
                subscription_token(SECRET, "bob"),
                subscription_token(SECRET, "alice", 1),
                subscription_token("other-secret", "alice"),
            }
        )
        == 4
    )


def test_find_subscriber_matches_only_the_current_token() -> None:
    users = ["alice", "bob"]
    assert find_subscriber(SECRET, subscription_token(SECRET, "bob"), users) == "bob"
    assert find_subscriber(SECRET, subscription_token(SECRET, "carol"), users) is None
    # A reset moves the counter, so the old link stops working.
    counters = {"alice": 1}
    old = subscription_token(SECRET, "alice")
    new = subscription_token(SECRET, "alice", 1)
    assert find_subscriber(SECRET, old, users, counters) is None
    assert find_subscriber(SECRET, new, users, counters) == "alice"
    assert find_subscriber(SECRET, "not a token", users) is None


def test_versions_and_the_minimum_a_config_needs() -> None:
    assert parse_version("1.14.2") == (1, 14, 2)
    assert parse_version("1.15.0-beta.3") == (1, 15, 0)
    assert parse_version("latest") is None
    assert minimum_sing_box_version("1.14.2") == "1.14.0"


def test_filter_redacts_tokens_in_uvicorn_access_records() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/sub/abcdefghijklmnopqrstuv?format=sbc", "1.1", 200),
        None,
    )
    assert TokenRedactingFilter().filter(record)
    assert record.getMessage() == (
        '127.0.0.1:1234 - "GET /sub/[token]?format=sbc HTTP/1.1" 200'
    )


def _inventory() -> dict:
    return {
        "deployment": {
            "host": "vpn.example.com",
            "ip": "203.0.113.10",
            "trojan_port": 443,
            "hysteria2_port": 8443,
            "naive_port": 9443,
            "tls": {
                "enabled": True,
                "server_name": "vpn.example.com",
                "key_path": "/fixture/key.pem",
                "certificate_path": "/fixture/cert.pem",
            },
        },
        "web_portal": {
            "users": [
                {"username": name, "password_hash": PASSWORD_HASH}
                for name in ("alice", "bob")
            ]
        },
        "trojan": {
            "users": [
                {"username": "alice", "password": "alice-trojan"},
                {"username": "carol", "password": "carol-trojan"},
            ]
        },
        "hysteria2": {"obfs_password": "obfs", "users": []},
        "naive": {"users": [{"username": "alice", "password": "alice-naive"}]},
    }


@pytest.fixture
def subscription_settings(tmp_path: Path, test_settings: Settings) -> Settings:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.dump(_inventory()), encoding="utf-8")
    return test_settings.model_copy(
        update={
            "sing_box_version": "1.14.2",
            "default_protocol": "hysteria2",
            "config_path": config_path,
            "web": test_settings.web.model_copy(
                update={
                    "subscription_secret": SECRET,
                    "subscription_database_path": tmp_path / "subscriptions.sqlite3",
                }
            ),
        }
    )


@pytest.fixture
def subscription_client(
    subscription_settings: Settings, tmp_release_dir: Path
) -> TestClient:
    app = create_app(subscription_settings)
    app.state.release_dir = tmp_release_dir
    return TestClient(app)


def _url(username: str) -> str:
    return f"/sub/{subscription_token(SECRET, username)}"


def test_envelope_carries_the_users_config_and_defaults(
    subscription_client: TestClient,
) -> None:
    response = subscription_client.get(_url("alice"), params=QUERY)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    envelope = response.json()
    assert envelope["version"] == 1
    assert envelope["default_route"] == "china"
    # hysteria2 is the configured default, but alice does not hold it.
    assert envelope["default_protocol"] == "trojan"
    assert envelope["nodes"] == [
        {"name": "vpn.example.com", "protocols": ["trojan", "naive"]}
    ]
    assert envelope["latest"] == {"sing_box": "1.14.2"}
    selector = envelope["config"]["outbounds"][0]
    assert selector["outbounds"] == ["trojan", "naive"]
    assert "alice-naive" in response.text
    assert "carol-trojan" not in response.text


def test_a_matching_etag_gets_304(subscription_client: TestClient) -> None:
    first = subscription_client.get(_url("alice"), params=QUERY)
    etag = first.headers["etag"]

    repeat = subscription_client.get(
        _url("alice"), params=QUERY, headers={"If-None-Match": etag}
    )
    other_os = subscription_client.get(
        _url("alice"),
        params={**QUERY, "os": "darwin"},
        headers={"If-None-Match": etag},
    )

    assert repeat.status_code == 304
    assert repeat.content == b""
    assert repeat.headers["etag"] == etag
    assert other_os.status_code == 200
    assert other_os.headers["etag"] != etag


@pytest.mark.parametrize(
    "path",
    [
        "/sub/abcdefghijklmnopqrstuv",
        "/sub/too-short",
        # carol has credentials but no portal account.
        f"/sub/{subscription_token(SECRET, 'carol')}",
        # bob has a portal account but no protocol.
        f"/sub/{subscription_token(SECRET, 'bob')}",
    ],
)
def test_unknown_links_are_not_found(
    subscription_client: TestClient, path: str
) -> None:
    assert subscription_client.get(path, params=QUERY).status_code == 404


def test_links_are_off_without_a_subscription_secret(client: TestClient) -> None:
    response = client.get(_url("alice"), params=QUERY)
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"format": None}, "format must be sbc"),
        ({"os": "freebsd"}, "os must be one of"),
        ({"sing-box": None}, "sing-box must name"),
        ({"sing-box": "latest"}, "sing-box must name"),
    ],
)
def test_bad_parameters_are_rejected(
    subscription_client: TestClient, changes: dict, detail: str
) -> None:
    params = {
        key: value for key, value in {**QUERY, **changes}.items() if value is not None
    }
    response = subscription_client.get(_url("alice"), params=params)
    assert response.status_code == 400
    assert detail in response.json()["detail"]


def test_an_old_sing_box_is_told_the_version_it_needs(
    subscription_client: TestClient,
) -> None:
    response = subscription_client.get(
        _url("alice"), params={**QUERY, "sing-box": "1.13.9"}
    )
    assert response.status_code == 409
    assert response.json()["sing_box_minimum"] == "1.14.0"


def test_usage_rides_in_the_subscription_userinfo_header(
    subscription_client: TestClient,
) -> None:
    class Provider:
        def get_user_summary(self, username: str) -> UserTrafficSummary:
            assert username == "alice"
            return UserTrafficSummary(
                username=username,
                cycle_month="2026-09",
                cycle_start=None,
                cycle_end=None,
                timezone="Asia/Shanghai",
                upload_bytes=12,
                download_bytes=3456,
            )

    subscription_client.app.state.traffic_stats_provider = Provider()

    response = subscription_client.get(_url("alice"), params=QUERY)

    assert response.headers["subscription-userinfo"] == "upload=12; download=3456"


def test_reset_counters_start_at_zero_and_advance(tmp_path: Path) -> None:
    counters = ResetCounters(tmp_path / "subscriptions.sqlite3")
    assert counters.current() == {}
    assert counters.advance("alice") == 1
    assert counters.advance("alice") == 2
    assert counters.advance("bob") == 1
    assert counters.current() == {"alice": 2, "bob": 1}


def _sign_in(client: TestClient, username: str) -> None:
    token = client.app.state.session_manager.create_token(username)
    client.cookies.set("session", token)


def test_files_page_shows_the_link_and_a_reset_replaces_it(
    subscription_client: TestClient,
) -> None:
    old_url = _url("alice")
    _sign_in(subscription_client, "alice")

    page = subscription_client.get("/files")
    assert f"http://testserver{old_url}" in page.text

    reset = subscription_client.post("/subscription/reset", follow_redirects=False)
    assert reset.status_code == 303
    assert reset.headers["location"] == "/files"

    new_url = f"/sub/{subscription_token(SECRET, 'alice', 1)}"
    assert subscription_client.get(old_url, params=QUERY).status_code == 404
    assert subscription_client.get(new_url, params=QUERY).status_code == 200
    assert f"http://testserver{new_url}" in subscription_client.get("/files").text


def test_reset_needs_a_session(subscription_client: TestClient) -> None:
    reset = subscription_client.post("/subscription/reset", follow_redirects=False)
    assert reset.status_code == 303
    assert reset.headers["location"] == "/"
    assert subscription_client.get(_url("alice"), params=QUERY).status_code == 200


def test_files_page_has_no_link_without_a_protocol_or_a_secret(
    subscription_client: TestClient, client: TestClient
) -> None:
    _sign_in(subscription_client, "bob")
    assert "/sub/" not in subscription_client.get("/files").text
    _sign_in(client, "alice")
    assert "/sub/" not in client.get("/files").text


def test_a_machine_token_exchanges_for_the_link(
    subscription_client: TestClient,
) -> None:
    machine_tokens = subscription_client.app.state.machine_token_manager

    def exchange(username: str):
        token = machine_tokens.create_token(username)
        return subscription_client.post(
            "/api/sub", headers={"Authorization": f"Bearer {token}"}
        )

    alice = exchange("alice")
    assert alice.status_code == 200
    assert alice.json() == {
        "url": f"http://testserver{_url('alice')}",
        "username": "alice",
    }
    assert exchange("bob").status_code == 404
    assert subscription_client.post("/api/sub").status_code == 401


def test_windows_bootstrapper_is_public_utf8_text(
    subscription_client: TestClient,
) -> None:
    response = subscription_client.get("/install.ps1")
    script = (
        Path(__file__).resolve().parents[1] / "sing_box_manager/web/client/install.ps1"
    )
    assert response.status_code == 200
    assert response.text == script.read_text(encoding="utf-8-sig")
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == 'inline; filename="install.ps1"'
    assert (
        subscription_client.get(
            "/install.ps1", headers={"host": "evil.example"}
        ).status_code
        == 400
    )
