"""Render the product TUN config with synthetic external dependencies."""
# ruff: noqa: INP001

import argparse
import json
import subprocess
from pathlib import Path

from sing_box_manager.desktop_config import (
    UserProtocols,
    build_linux_tun_config,
)
from sing_box_manager.settings import DeploymentConfig, TlsConfig, TrojanUser

TOKEN = "e2eToken-e2eToken-e2eT"


def prepare(assets: Path, binary: Path) -> dict:
    ports = json.loads((assets / "ports.json").read_text())
    deployment = DeploymentConfig(
        host="portal.fixture.test",
        ip="203.0.113.42",
        trojan_port=8443,
        hysteria2_port=4443,
        naive_port=9443,
        tls=TlsConfig(enabled=False),
    )
    config = build_linux_tun_config(
        deployment,
        UserProtocols(
            trojan=TrojanUser(username="fixture", password="fixture-password")  # noqa: S106 -- synthetic test account
        ),
        default_protocol="trojan",
        default_route="china-direct",
    )
    # Only external dependencies change. Route rules and DNS rules are untouched.
    for outbound in config["outbounds"]:
        if outbound["tag"] == "trojan":
            outbound.clear()
            outbound.update(
                type="socks",
                tag="trojan",
                server="10.0.2.100",
                server_port=ports["socks"],
            )
    ca = (assets / "ca.crt").read_text().splitlines()
    for server in config["dns"]["servers"]:
        if server["tag"] == "google":
            server.update(
                server="198.18.0.53",
                server_port=853,
                tls={"server_name": "dns.fixture.test", "certificate": ca},
            )
        else:
            server.update(server="127.0.0.1", server_port=5353)
    config["experimental"]["cache_file"]["cache_id"] = "fixture-good"
    config.pop("ntp")  # Clock preparation is outside this routing test.
    rules = {
        "AdGuard": [{"domain": ["blocked.fixture.test"]}],
        "AI-Anthropic": [{"domain": ["ai.fixture.test"]}],
        "AI-Geosite": [{"domain": ["other-ai.fixture.test"]}],
        "GFWList": [{"domain": ["gfw.fixture.test"]}],
        "GeoSite-CN": [{"domain": ["www.baidu.com", "china.fixture.test"]}],
        "GeoSite-Apple-CN": [{"domain": ["apple-cn.fixture.test"]}],
        "GeoIP-CN": [{"ip_cidr": ["198.18.0.10/32"]}],
    }
    for rule_set in config["route"]["rule_set"]:
        tag = rule_set["tag"]
        # Avoid assuming the production spelling of the AdGuard tag.
        content = rules["AdGuard"] if "adguard" in tag.lower() else rules[tag]
        target = assets / rule_set["initial_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        source = target.with_suffix(".json")
        source.write_text(json.dumps({"version": 3, "rules": content}))
        subprocess.run(
            [str(binary), "rule-set", "compile", "--output", str(target), str(source)],
            check=True,
        )
    (assets / "tun.json").write_text(json.dumps(config))
    sub = assets / "sub" / TOKEN
    sub.parent.mkdir(parents=True, exist_ok=True)
    sub.write_text(json.dumps({"version": 1, "config": {}, "config_tun": config}))
    (assets / "good-sub.json").write_text(sub.read_text())
    failed = json.loads(json.dumps(config))
    next(outbound for outbound in failed["outbounds"] if outbound["tag"] == "trojan")[
        "server_port"
    ] = 9
    failed["experimental"]["clash_api"]["default_mode"] = "global"
    failed["experimental"]["cache_file"]["cache_id"] = "fixture-bad"
    (assets / "failed-sub.json").write_text(
        json.dumps({"version": 1, "config": {}, "config_tun": failed})
    )
    failed["experimental"]["clash_api"]["default_mode"] = "direct"
    failed["experimental"]["cache_file"]["cache_id"] = "fixture-direct-bad"
    (assets / "direct-failed-sub.json").write_text(
        json.dumps({"version": 1, "config": {}, "config_tun": failed})
    )
    (assets / "subscription").write_text(
        f"http://10.0.2.100:{ports['portal']}/sub/{TOKEN}\n"
    )
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assets", type=Path)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    prepare(args.assets, args.binary.resolve())


if __name__ == "__main__":
    main()
