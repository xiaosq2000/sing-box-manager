"""Golden profiles, fail-closed release validation and shared-stream accounting."""

import json
import subprocess
from pathlib import Path

import pytest

from sing_box_manager import connection_stats, traffic_stats
from sing_box_manager.desktop_config import build_desktop_config
from sing_box_manager.proto import sing_box_api_pb2 as pb
from sing_box_manager.release.profiles import build_client_profile, build_server_config
from sing_box_manager.release.platforms import (
    MOBILE_TUN_PROFILE,
    MIXED_PROFILE,
    ROUTE_STRATEGIES,
    Platform,
    Protocol,
    client_config_filename,
    route_client_config_filename,
)
from sing_box_manager.release.renderers import apply_desktop_route_strategy
from sing_box_manager.release.validation import check_config
from sing_box_manager.settings import Settings, TrafficStatsSettings, load_settings
from sing_box_manager.subscription import user_protocols
from tests.fetch_sing_box import EXAMPLE_INVENTORY, TEST_BINARY
from tests.traffic_fixtures import NOW, new, update, usage_rows

GOLDENS = Path(__file__).parent / "fixtures/profiles"


def profile_inventory() -> Settings:
    inventory = load_settings(EXAMPLE_INVENTORY)
    # No live credentials: this is the checked-in example, expanded so one
    # fixture user exercises every protocol. Disabled users must not leak in.
    inventory.hysteria2.users[0].username = "alice"
    for protocol in ("trojan", "hysteria2", "naive"):
        section = getattr(inventory, protocol)
        section.users.append(
            section.users[0].model_copy(
                update={"username": "disabled-user", "enabled": False}
            )
        )
    inventory.deployment.direct_domain_suffixes = ["portal.example.net"]
    return inventory


def golden_profiles() -> dict[str, dict]:
    inventory = profile_inventory()
    protocols = user_protocols(inventory, "alice")
    configs = {
        f"desktop-{os_name}": build_desktop_config(
            inventory.deployment,
            protocols,
            os_name=os_name,
            default_protocol="trojan",
            default_route="china",
        )
        for os_name in ("linux", "darwin", "windows")
    }
    for protocol in ("trojan", "hysteria2", "naive"):
        configs[f"mobile-{protocol}"] = build_client_profile(
            inventory.deployment, protocols, protocol, MOBILE_TUN_PROFILE
        )
        for route in ROUTE_STRATEGIES:
            base = build_client_profile(
                inventory.deployment, protocols, protocol, MIXED_PROFILE
            )
            configs[f"mixed-{protocol}-{route}"] = apply_desktop_route_strategy(
                base, route, os_name="linux"
            )
    for enabled in (False, True):
        configs[f"server-stats-{str(enabled).lower()}"] = build_server_config(
            inventory, TrafficStatsSettings(enabled=enabled)
        )
    return configs


@pytest.mark.parametrize("name", list(golden_profiles()))
def test_profile_matches_golden_output(name: str) -> None:
    assert golden_profiles()[name] == json.loads((GOLDENS / f"{name}.json").read_text())


@pytest.mark.parametrize("protocol", ["trojan", "hysteria2", "naive"])
def test_production_client_profiles_match_goldens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str
) -> None:
    from sing_box_manager.release import builder as builder_module
    from sing_box_manager.release.artifacts import ReleaseInfo

    monkeypatch.setattr(
        builder_module,
        "build_release_info",
        lambda version, project_root: ReleaseInfo(
            release_dir_name="fixture",
            commit_sha="fixture",
            dirty=False,
            date="2026-10-04T00:00:00Z",
            upstream_version=version,
        ),
    )
    inventory = profile_inventory()
    builder = builder_module.ReleaseBuilder(inventory, project_root=tmp_path)
    builder._inventory = inventory
    checked = []
    monkeypatch.setattr(
        builder, "_check_config", lambda config, label: checked.append(config)
    )
    platform = Platform(
        name="linux-amd64",
        url_filename="unused",
        client_profiles=(MIXED_PROFILE, MOBILE_TUN_PROFILE),
    )
    user = builder._collect_release_users(inventory)[0]
    profiles = builder._customize_protocol_config(
        user, inventory.deployment, platform, Protocol(name=protocol)
    )
    expected = {
        route_client_config_filename(protocol, route): json.loads(
            (GOLDENS / f"mixed-{protocol}-{route}.json").read_text()
        )
        for route in ROUTE_STRATEGIES
    }
    expected[client_config_filename(protocol, MOBILE_TUN_PROFILE)] = json.loads(
        (GOLDENS / f"mobile-{protocol}.json").read_text()
    )
    assert {filename: json.loads(payload) for filename, payload in profiles} == expected
    assert len(checked) == len(expected)


def test_server_keeps_wire_credentials_and_qualifies_only_auth_labels() -> None:
    inventory = profile_inventory()
    original = inventory.model_dump()
    config = build_server_config(inventory, TrafficStatsSettings(enabled=True))
    assert inventory.model_dump() == original
    assert [entry["tag"] for entry in config["inbounds"]] == [
        "trojan",
        "hysteria2",
        "naive",
    ]
    assert [entry["users"] for entry in config["inbounds"]] == [
        [{"name": "trojan:alice", "password": "change-me"}],
        [{"name": "hysteria2:alice", "password": "change-me"}],
        [{"username": "alice", "password": "change-me"}],
    ]
    assert config["experimental"]["v2ray_api"]["stats"]["users"] == [
        "trojan:alice",
        "hysteria2:alice",
        "alice",
    ]
    assert len(config["services"]) == 1
    for inbound in config["inbounds"]:
        assert inbound["listen_port"] == getattr(
            inventory.deployment, f"{inbound['type']}_port"
        )
        assert inbound["tls"]["server_name"] == inventory.deployment.tls.server_name
        assert inbound["tls"]["key_path"] == inventory.deployment.tls.key_path
        assert (
            inbound["tls"]["certificate_path"]
            == inventory.deployment.tls.certificate_path
        )
    assert config["inbounds"][0]["multiplex"] == {"enabled": True}
    assert config["inbounds"][1]["obfs"] == {
        "type": "salamander",
        "password": inventory.hysteria2.obfs_password,
    }
    assert config["inbounds"][2]["network"] == "tcp"


@pytest.mark.parametrize("enabled", [False, True])
def test_server_uses_one_configured_api_pair_only_when_enabled(enabled: bool) -> None:
    config = build_server_config(
        profile_inventory(),
        TrafficStatsSettings(
            enabled=enabled,
            api_listen="127.0.0.1:19180",
            connection_api_listen="[::1]:19190",
            connection_api_secret="fixture-secret",
        ),
    )
    if not enabled:
        assert "services" not in config
        assert "experimental" not in config
        return
    assert config["experimental"]["v2ray_api"]["listen"] == "127.0.0.1:19180"
    assert config["services"] == [
        {
            "type": "api",
            "tag": "connection-api",
            "listen": "::1",
            "listen_port": 19190,
            "secret": "fixture-secret",
            "dashboard": {"enabled": False},
        }
    ]


def test_validation_never_changes_the_rendered_config_and_checks_features(
    tmp_path: Path,
) -> None:
    if not TEST_BINARY.exists():
        pytest.skip("Run pixi run fetch-sing-box")
    config = build_server_config(profile_inventory())
    original = json.dumps(config, sort_keys=True)
    check_config(TEST_BINARY, config, label="fixture", server=True)
    assert json.dumps(config, sort_keys=True) == original
    config["unsupported-secret-field"] = "must-not-be-logged"
    with pytest.raises(ValueError, match="sing-box check failed") as error:
        check_config(TEST_BINARY, config, label="fixture", server=True)
    assert "must-not-be-logged" not in str(error.value)


def test_failed_checker_output_is_not_logged(monkeypatch, capsys) -> None:
    def fail(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 1, b"private-password", b"private-key"
        )

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ValueError, match="fixture"):
        check_config(Path("/unused/binary"), {}, label="fixture")
    assert "private" not in str(capsys.readouterr())


def test_node_stream_partitions_new_updates_closed_and_replay(tmp_path: Path) -> None:
    collector = connection_stats.ConnectionStreamCollector(
        settings=Settings(), database_path=tmp_path / "stats.db", clock=lambda: NOW
    )
    events = []
    for index, protocol in enumerate(("trojan", "hysteria2", "naive"), start=1):
        event = new(
            protocol,
            user="alice" if protocol == "naive" else f"{protocol}:alice",
            up=index * 10,
            down=index * 20,
        )
        event.connection.inboundType = protocol
        event.connection.inbound = protocol
        events.append(event)
    opening = pb.ConnectionEvents(reset=True, events=events)
    collector._apply_node_frame(opening, now=NOW)
    collector._flush()
    collector._apply_node_frame(opening, now=NOW)
    collector._flush()
    collector._apply_node_frame(
        pb.ConnectionEvents(events=[update("hysteria2", up=3, down=4)]), now=NOW
    )
    closed = pb.ConnectionEvent(type=pb.CONNECTION_EVENT_CLOSED, id="hysteria2")
    closed.connection.CopyFrom(events[1].connection)
    closed.connection.uplinkTotal = 25
    closed.connection.downlinkTotal = 46
    collector._apply_node_frame(pb.ConnectionEvents(events=[closed]), now=NOW)
    collector._flush()
    rows = usage_rows(collector._store)
    assert {
        (
            row["protocol"],
            row["username"],
            row["upload_bytes"],
            row["download_bytes"],
            row["connection_count"],
        )
        for row in rows
    } == {
        ("trojan", "alice", 10, 20, 1),
        ("hysteria2", "alice", 25, 46, 1),
        ("naive", "alice", 30, 60, 1),
    }
    # An empty complete replay clears the live view without recounting usage.
    collector._apply_node_frame(pb.ConnectionEvents(reset=True), now=NOW)
    collector._flush()
    assert not collector._store.read_live_connections(now=NOW)


@pytest.mark.parametrize(
    "protocol,wire_name",
    [("trojan", "trojan:alice"), ("hysteria2", "hysteria2:alice"), ("naive", "alice")],
)
def test_shared_counter_api_queries_only_the_protocols_wire_names(
    monkeypatch, protocol: str, wire_name: str
) -> None:
    import grpc

    request_type, response_type, _, sys_response_type = (
        traffic_stats._v2ray_api_message_types()
    )
    requested = []

    class Channel:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def unary_unary(self, method, **kwargs):
            def call(request, **options):
                if method == traffic_stats._GET_SYS_STATS_METHOD:
                    return sys_response_type(Uptime=100)
                assert isinstance(request, request_type)
                requested.extend(request.patterns)
                response = response_type()
                response.stat.add(
                    name=f"user>>>{wire_name}>>>traffic>>>uplink", value=10
                )
                response.stat.add(
                    name=f"user>>>{wire_name}>>>traffic>>>downlink", value=20
                )
                response.stat.add(name="user>>>foreign>>>traffic>>>downlink", value=999)
                return response

            return call

    monkeypatch.setattr(grpc, "insecure_channel", lambda address: Channel())
    snapshot = traffic_stats._query_service_snapshot(
        traffic_stats._ServiceTarget(protocol, "127.0.0.1:19080", ("alice",))
    )
    assert requested == [f"user>>>{wire_name}>>>traffic>>>"]
    assert snapshot.counters == {"alice": traffic_stats.UserTrafficCounters(10, 20)}
