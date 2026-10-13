"""A new config retains selections cached by an older client config."""

import copy
from pathlib import Path

import pytest

from sing_box_manager.settings import DeploymentConfig
from tests.test_desktop_config import (
    PROTOCOLS,
    _api,
    _enable_api,
    _fixture_config,
    _proxy_through,
)
from tests.test_sing_box_114 import binary as binary
from tests.test_sing_box_114 import deployment as deployment
from tests.test_sing_box_114 import (
    _running,
    assert_desktop_routing,
    routing_fixtures,
    use_routing_fixtures,
)


@pytest.mark.parametrize("legacy", ["ai", "gfw", "china"])
def test_cached_legacy_selection_survives_config_rename(
    tmp_path: Path, binary: Path, deployment: DeploymentConfig, legacy: str
) -> None:
    current = _fixture_config(deployment)
    aliases = {"ai-only": "ai", "gfw-proxy": "gfw", "china-direct": "china"}
    with routing_fixtures() as fixtures:
        _proxy_through(
            current, dict.fromkeys(PROTOCOLS, fixtures.proxy.server_address[1])
        )
        api_port, secret = _enable_api(current)
        use_routing_fixtures(current, tmp_path, binary, fixtures)
        old = copy.deepcopy(current)
        for section in ("route", "dns"):
            rules = old[section]["rules"]
            rules[:] = [
                rule
                for rule in rules
                if rule.get("clash_mode") not in (*aliases, "direct")
            ]
        old["experimental"]["clash_api"]["default_mode"] = "china"
        with _running(binary, tmp_path, old):
            # A no-op selection of the default does not write the mode cache.
            _api(api_port, secret, "PATCH", "/configs", {"mode": "global"})
            _api(api_port, secret, "PATCH", "/configs", {"mode": legacy})
            _api(api_port, secret, "PUT", "/proxies/proxy", {"name": "naive"})
        with _running(binary, tmp_path, current):
            state = _api(api_port, secret, "GET", "/configs")
            assert state["mode"] == legacy
            assert aliases.keys() <= set(state["mode-list"])
            assert _api(api_port, secret, "GET", "/proxies/proxy")["now"] == "naive"
            fixtures.reset_counters()
            assert_desktop_routing(
                current["inbounds"][0]["listen_port"],
                legacy,
                fixtures,
                proxy_available=True,
            )
