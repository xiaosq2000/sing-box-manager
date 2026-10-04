"""One real node process authenticates all three protocols and separates usage."""

import os
import threading
import time
from pathlib import Path

import pytest

from sing_box_manager import connection_stats, traffic_stats
from sing_box_manager.desktop_config import _protocol_outbounds
from sing_box_manager.release.profiles import build_server_config
from sing_box_manager.settings import TrafficStatsSettings
from sing_box_manager.subscription import user_protocols
from tests.test_m3_profiles import profile_inventory
from tests.test_sing_box_114 import binary as binary
from tests.test_sing_box_114 import deployment as deployment
from tests.test_sing_box_114 import _DirectHandler, _connect, _port, _running, _server
from tests.traffic_fixtures import usage_rows


def test_one_node_authenticates_all_protocols_and_reports_separate_usage(
    tmp_path: Path, binary, deployment, monkeypatch
) -> None:
    server_binary = os.environ.get("SBM_TEST_SING_BOX_SERVER")
    if not server_binary:
        if os.environ.get("SBM_REQUIRE_SING_BOX"):
            pytest.fail("CI must provide the pinned custom server binary")
        pytest.skip("Set SBM_TEST_SING_BOX_SERVER to the pinned custom server build")
    inventory = profile_inventory()
    inventory.deployment_data = deployment.model_copy(
        update={
            "trojan_port": _port(),
            "hysteria2_port": _port(),
            "naive_port": _port(),
        }
    )
    stats = TrafficStatsSettings(
        enabled=True,
        api_listen=f"127.0.0.1:{_port()}",
        connection_api_listen=f"127.0.0.1:{_port()}",
    )
    inventory.traffic_stats = stats
    server = build_server_config(inventory, stats)
    for inbound in server["inbounds"]:
        inbound["listen"] = "127.0.0.1"
    server_root = tmp_path / "server"
    server_root.mkdir()
    collector = connection_stats.ConnectionStreamCollector(
        settings=inventory, database_path=tmp_path / "stats.db"
    )
    subscribed = threading.Event()
    real_stream = connection_stats.open_stream

    def observed_stream(**options):
        for frame in real_stream(**options):
            subscribed.set()
            yield frame

    monkeypatch.setattr(connection_stats, "open_stream", observed_stream)
    with (
        _running(Path(server_binary), server_root, server),
        _server(_DirectHandler) as destination,
    ):
        thread = threading.Thread(
            target=collector._run_service, args=(collector.endpoints[0],), daemon=True
        )
        thread.start()
        try:
            assert subscribed.wait(10), "Node stream did not send its opening frame"
            outbounds = _protocol_outbounds(
                inventory.deployment, user_protocols(inventory, "alice")
            )
            for outbound in outbounds:
                protocol = outbound["type"]
                # The generated fixture certificate is self-signed. No real
                # client setting is changed; only these loopback test clients.
                outbound["tls"]["certificate"] = (
                    Path(deployment.tls.certificate_path).read_text().splitlines()
                )
                outbound.pop("domain_resolver", None)
                outbound["tag"] = "proxy"
                client_root = tmp_path / protocol
                client_root.mkdir()
                port = _port()
                config = {
                    "inbounds": [
                        {"type": "mixed", "listen": "127.0.0.1", "listen_port": port}
                    ],
                    "outbounds": [outbound],
                    "route": {"final": "proxy"},
                }
                with _running(binary, client_root, config):
                    assert b"direct" in _connect(
                        port, "127.0.0.1", destination.server_address[1]
                    )
                snapshot = traffic_stats._query_service_snapshot(
                    traffic_stats._ServiceTarget(protocol, stats.api_listen, ("alice",))
                )
                assert snapshot.counters["alice"].upload_bytes > 0
                assert snapshot.counters["alice"].download_bytes > 0
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                collector._flush()
                rows = usage_rows(collector._store)
                if {row["protocol"] for row in rows} == {
                    "trojan",
                    "hysteria2",
                    "naive",
                }:
                    break
                time.sleep(0.05)
            assert {row["protocol"] for row in rows} == {"trojan", "hysteria2", "naive"}
            assert {row["username"] for row in rows} == {"alice"}
            assert all(
                row["upload_bytes"] > 0 and row["download_bytes"] > 0 for row in rows
            )
        finally:
            collector.stop()
            thread.join(timeout=5)
            assert not thread.is_alive()
            collector._flush(final=True)
