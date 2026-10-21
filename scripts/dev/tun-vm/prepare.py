"""Render product client configs with synthetic external dependencies."""
# ruff: noqa: INP001

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from sing_box_manager.desktop_config import (
    UserProtocols,
    build_desktop_config,
    build_linux_tun_config,
)
from sing_box_manager.settings import DeploymentConfig, NaiveUser, TlsConfig, TrojanUser

TOKEN = "e2eToken-e2eToken-e2eT"


def external_dependencies(config: dict, ports: dict, ca: list[str]) -> None:
    """Replace transports, resolvers and clock input, not product policy rules."""
    for outbound in config["outbounds"]:
        if outbound["tag"] in {"trojan", "naive"}:
            tag = outbound["tag"]
            outbound.clear()
            outbound.update(
                type="socks",
                tag=tag,
                server="10.0.2.100",
                server_port=ports["socks"]
                if tag == "trojan"
                else ports.get("socks_alt", ports["socks"]),
            )
    for server in config["dns"]["servers"]:
        if server["tag"] == "google":
            server.update(
                server="198.18.0.53",
                server_port=853,
                tls={"server_name": "dns.fixture.test", "certificate": ca},
            )
        else:
            server.update(server="127.0.0.1", server_port=5353)
    config.pop("ntp")


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
    protocols = UserProtocols(
        trojan=TrojanUser(username="fixture", password="fixture-password"),  # noqa: S106 -- synthetic account
        naive=NaiveUser(username="fixture-alt", password="fixture-alt-password"),  # noqa: S106 -- selector tag only, not Naive acceptance
    )
    config = build_linux_tun_config(
        deployment, protocols, default_protocol="trojan", default_route="china-direct"
    )
    mixed = build_desktop_config(
        deployment,
        protocols,
        os_name="linux",
        default_protocol="trojan",
        default_route="china-direct",
    )
    ca = (assets / "ca.crt").read_text().splitlines()
    for current in (config, mixed):
        external_dependencies(current, ports, ca)
    config["experimental"]["cache_file"]["cache_id"] = "fixture-good"
    mixed["experimental"]["clash_api"].update(
        external_controller="127.0.0.1:19091",
        secret="fixture-local-api",  # noqa: S106 -- synthetic API state
    )
    (assets / "mixed.json").write_text(json.dumps(mixed))
    rules = {
        "AdGuard": [{"domain": ["blocked.fixture.test"]}],
        "AI-Anthropic": [{"domain": ["ai.fixture.test"]}],
        "AI-Geosite": [{"domain": ["other-ai.fixture.test"]}],
        "GFWList": [{"domain": ["gfw.fixture.test"]}],
        "GeoSite-CN": [{"domain": ["www.baidu.com", "china.fixture.test"]}],
        "GeoSite-Apple-CN": [{"domain": ["apple-cn.fixture.test"]}],
        "GeoIP-CN": [{"ip_cidr": ["198.18.0.10/32"]}],
    }
    changed = assets / "changed-rules"
    changed.mkdir(exist_ok=True)
    for rule_set in config["route"]["rule_set"]:
        tag = rule_set["tag"]
        content = rules["AdGuard"] if "adguard" in tag.lower() else rules[tag]
        target = assets / rule_set["initial_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        source = target.with_suffix(".json")
        source.write_text(json.dumps({"version": 3, "rules": content}))
        subprocess.run(
            [str(binary), "rule-set", "compile", "--output", str(target), str(source)],
            check=True,
        )
        if tag == "GFWList":
            source = changed / "gfw.json"
            source.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "rules": [
                            {"domain": ["gfw.fixture.test", "ordinary.fixture.test"]}
                        ],
                    }
                )
            )
            subprocess.run(
                [
                    str(binary),
                    "rule-set",
                    "compile",
                    "--output",
                    str(changed / target.name),
                    str(source),
                ],
                check=True,
            )
        else:
            shutil.copy2(target, changed / target.name)
    (assets / "tun.json").write_text(json.dumps(config))
    sub = assets / "sub" / TOKEN
    sub.parent.mkdir(parents=True, exist_ok=True)
    sub.write_text(json.dumps({"version": 1, "config": mixed, "config_tun": config}))
    (assets / "good-sub.json").write_text(sub.read_text())
    failed = json.loads(json.dumps(config))
    for outbound in failed["outbounds"]:
        if outbound["tag"] in {"trojan", "naive"}:
            outbound["server_port"] = 9
    failed["experimental"]["clash_api"]["default_mode"] = "global"
    failed["experimental"]["cache_file"]["cache_id"] = "fixture-bad"
    (assets / "failed-sub.json").write_text(
        json.dumps({"version": 1, "config": mixed, "config_tun": failed})
    )
    failed["experimental"]["clash_api"]["default_mode"] = "direct"
    failed["experimental"]["cache_file"]["cache_id"] = "fixture-direct-bad"
    (assets / "direct-failed-sub.json").write_text(
        json.dumps({"version": 1, "config": mixed, "config_tun": failed})
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
