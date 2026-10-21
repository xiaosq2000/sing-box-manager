"""Run installation, real upgrades and four boots in a disposable QEMU guest."""
# ruff: noqa: INP001, S101 -- executable acceptance assertions; guard refuses -O

import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ASSETS = Path("/opt/tun-e2e")
HOME = Path("/home/ubuntu")
CONFIG = HOME / ".config/sbc"
DATA = HOME / ".local/share/sbc"
EVIDENCE = HOME / "lifecycle-evidence"
STATE = HOME / "lifecycle-state.json"
JOURNAL = Path("/etc/sbc/tun-control/transaction.json")
SBC = DATA / "cli/sbc"
SYSTEMCTL = "/usr/bin/systemctl"


def run(*args: str, ok: bool = True, **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=90, check=False, **kwargs
    )
    if ok and result.returncode:
        raise RuntimeError(
            f"Command failed: {args!r}\n{result.stdout}\n{result.stderr}"
        )
    return result


def root(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return run("/usr/bin/sudo", "--", *args, **kwargs)


def sbc(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return run(str(SBC), *args, **kwargs)


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".next")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def boot_id() -> str:
    return str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))


def guard() -> str:
    if not __debug__:
        raise RuntimeError("Python optimization disables acceptance assertions")
    if socket.gethostname() != "tun-fixture" or not ASSETS.joinpath("run-id").is_file():
        raise RuntimeError(
            "Refusing to change a system outside the disposable TUN guest"
        )
    run("systemd-detect-virt", "--vm", "--quiet")
    identity = ASSETS.joinpath("run-id").read_text().strip()
    if not re.fullmatch("[a-f0-9]{32}", identity):
        raise RuntimeError("Invalid disposable guest identity")
    return identity


def network() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Fixture network setup requires guest root")
    links = json.loads(run("ip", "-j", "link").stdout)
    interface = next(
        item["ifname"] for item in links if item.get("link_type") == "ether"
    )
    commands = [
        ("ip", "netns", "add", "sbc-fixture"),
        ("ip", "link", "set", interface, "netns", "sbc-fixture"),
        (
            "ip",
            "-n",
            "sbc-fixture",
            "addr",
            "replace",
            "10.0.2.15/24",
            "dev",
            interface,
        ),
        ("ip", "-n", "sbc-fixture", "link", "set", interface, "up"),
        ("ip", "-n", "sbc-fixture", "route", "replace", "default", "via", "10.0.2.2"),
        ("ip", "link", "add", "sbcf-main", "type", "veth", "peer", "name", "sbcf-site"),
        ("ip", "link", "set", "sbcf-site", "netns", "sbc-fixture"),
        ("ip", "addr", "add", "10.77.0.1/30", "dev", "sbcf-main"),
        ("ip", "link", "set", "sbcf-main", "up"),
        ("ip", "-n", "sbc-fixture", "addr", "add", "10.77.0.2/30", "dev", "sbcf-site"),
        ("ip", "-n", "sbc-fixture", "link", "set", "sbcf-site", "up"),
        ("ip", "-n", "sbc-fixture", "link", "set", "lo", "up"),
        ("ip", "-n", "sbc-fixture", "addr", "add", "198.18.0.10/32", "dev", "lo"),
        ("ip", "-n", "sbc-fixture", "route", "add", "blackhole", "198.18.0.20/32"),
        ("ip", "route", "replace", "default", "via", "10.77.0.2", "dev", "sbcf-main"),
        (
            "ip",
            "netns",
            "exec",
            "sbc-fixture",
            "sysctl",
            "-q",
            "-w",
            "net.ipv4.ip_forward=1",
        ),
    ]
    for command in commands:
        run(*command)
    rules = f'table ip fixture_nat {{\n chain post {{\n type nat hook postrouting priority srcnat;\n ip saddr 10.77.0.0/30 oifname "{interface}" masquerade;\n }}\n}}\n'
    run("ip", "netns", "exec", "sbc-fixture", "nft", "-f", "-", input=rules)
    fixture = str(ASSETS / "fixtures.py")
    services = [
        [
            "ip",
            "netns",
            "exec",
            "sbc-fixture",
            "python3",
            fixture,
            "direct",
            str(ASSETS),
        ],
        [
            "ip",
            "netns",
            "exec",
            "sbc-fixture",
            "python3",
            fixture,
            "guest-dns",
            str(ASSETS),
            "--bind",
            "10.77.0.2",
            "--port",
            "53",
            "--baseline",
        ],
        ["python3", fixture, "guest-dns", str(ASSETS)],
    ]
    for index, command in enumerate(services):
        with (ASSETS / f"network-{boot_id()}-{index}.log").open("w") as log:
            subprocess.Popen(command, stdout=log, stderr=log)


def bootstrap() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Fixture bootstrap requires guest root")
    Path("/etc/sudoers.d/sbc-fixture-umask").write_text(
        "Defaults:ubuntu umask=0077,umask_override\n"
    )
    Path("/etc/sudoers.d/sbc-fixture-umask").chmod(0o440)
    run("visudo", "-cf", "/etc/sudoers.d/sbc-fixture-umask")
    run(
        "install",
        "-m",
        "644",
        str(ASSETS / "ca.crt"),
        "/usr/local/share/ca-certificates/sbc-tun-fixture.crt",
    )
    run("update-ca-certificates")
    run("loginctl", "enable-linger", "ubuntu")
    network_unit = """[Unit]
Description=Disposable TUN fixture topology
DefaultDependencies=no
After=local-fs.target
Before=network-pre.target sbc-tun.service
[Service]
Type=oneshot
RemainAfterExit=yes
UMask=0077
StandardOutput=journal+console
StandardError=journal+console
ExecStart=/usr/bin/python3 /opt/tun-e2e/lifecycle.py network
[Install]
WantedBy=multi-user.target
"""
    controller_unit = """[Unit]
Description=Disposable TUN lifecycle controller
After=sbc-fixture-network.service user@1000.service sbc-tun.service
Wants=user@1000.service
[Service]
Type=oneshot
User=ubuntu
UMask=0077
Environment=HOME=/home/ubuntu XDG_RUNTIME_DIR=/run/user/1000 SBC_LANG=en PYTHONUNBUFFERED=1
Environment=PATH=/home/ubuntu/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/usr/bin/python3 /opt/tun-e2e/lifecycle.py run
StandardOutput=journal+console
StandardError=journal+console
TimeoutStartSec=1200
[Install]
WantedBy=multi-user.target
"""
    for name, body in [("network", network_unit), ("lifecycle", controller_unit)]:
        Path(f"/etc/systemd/system/sbc-fixture-{name}.service").write_text(body)
    run(SYSTEMCTL, "daemon-reload")
    run(
        SYSTEMCTL,
        "enable",
        "sbc-fixture-network.service",
        "sbc-fixture-lifecycle.service",
    )
    run(SYSTEMCTL, "start", "sbc-fixture-network.service")
    run(SYSTEMCTL, "start", "--no-block", "sbc-fixture-lifecycle.service")


def properties(unit: str, *, user: bool = False) -> dict:
    args = [SYSTEMCTL, *(["--user"] if user else []), "show", unit]
    names = "ActiveState,UnitFileState,MainPID,ExecCondition,LastTriggerUSecMonotonic,InvocationID,ExecMainStatus,Result,TriggeredBy"
    output = run(*args, "--property=" + names).stdout
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def api(path: str) -> dict:
    state = read(CONFIG / "tun.json")
    client = http.client.HTTPConnection("127.0.0.1", state["api_port"], timeout=5)
    try:
        client.request(
            "GET", path, headers={"Authorization": "Bearer " + state["api_secret"]}
        )
        response = client.getresponse()
        assert response.status == 200
        return json.loads(response.read())
    finally:
        client.close()


def wait_for(check, *, seconds: float = 60) -> None:
    end = time.monotonic() + seconds
    last = None
    while time.monotonic() < end:
        try:
            if check():
                return
        except (AssertionError, OSError, ValueError, RuntimeError) as error:
            last = error
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for fixture state: {last}")


def fingerprint() -> dict:
    paths = [
        CONFIG / "config.json",
        CONFIG / "tun.json",
        DATA / "sbc.version",
        DATA / "sing-box.version",
    ]
    paths += [
        p
        for directory in ("bin", "cli", "rules")
        for p in (DATA / directory).rglob("*")
        if p.is_file()
    ]
    result = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    output = root(
        "find",
        "/etc/sbc/tun",
        "/usr/local/lib/sbc-tun",
        "-type",
        "f",
        "-exec",
        "sha256sum",
        "{}",
        "+",
    ).stdout
    output += root("sha256sum", "/etc/systemd/system/sbc-tun.service").stdout
    for line in output.splitlines():
        digest, path = line.split(None, 1)
        result[path.strip()] = digest
    return result


def selection() -> bool:
    return (
        api("/configs")["mode"] == "gfw-proxy"
        and api("/proxies/proxy")["now"] == "naive"
    )


def matches_socket(
    item: dict, previous: set, endpoints: tuple, host: str, expected: str
) -> bool:
    source, target = endpoints
    metadata = item["metadata"]
    return (
        item["id"] not in previous
        and metadata.get("sourceIP") == source[0]
        and str(metadata.get("sourcePort")) == str(source[1])
        and metadata.get("destinationIP") == target[0]
        and str(metadata.get("destinationPort")) == str(target[1])
        and metadata.get("host") == host
        and metadata.get("network") == "tcp"
        and str(metadata.get("type", "")).startswith("tun/")
        and set(item["chains"])
        == ({"direct"} if expected == "direct" else {"naive", "proxy"})
    )


def probe(host: str, expected: str) -> None:
    print(f"=== socket proof: {host} via {expected} ===", flush=True)
    previous = {c["id"] for c in api("/connections")["connections"]}
    nonce = uuid.uuid4().hex
    with socket.create_connection((host, 443), timeout=10) as connection:
        source_ip, source_port = connection.getsockname()
        target_ip, target_port = connection.getpeername()
        with ssl.create_default_context().wrap_socket(
            connection, server_hostname=host
        ) as tls:

            def captured() -> bool:
                for item in api("/connections")["connections"]:
                    if matches_socket(
                        item,
                        previous,
                        ((source_ip, source_port), (target_ip, target_port)),
                        host,
                        expected,
                    ):
                        return True
                return False

            wait_for(captured, seconds=10)
            tls.sendall(
                f"GET /{nonce} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
            )
            response = http.client.HTTPResponse(tls)
            response.begin()
            assert response.status == 200
            assert response.getheader("Content-Length") == str(
                len(f"{expected} /{nonce}\n")
            )
            assert response.read(1024) == f"{expected} /{nonce}\n".encode()


def assert_active() -> None:
    journal = read(JOURNAL)
    assert journal["phase"] == "active"
    assert journal["committed"] is True
    state = properties("sbc-tun.service")
    assert state["ActiveState"] == "active"
    assert int(state["MainPID"]) > 0
    assert state["UnitFileState"] == "enabled"
    assert properties("sbc.service", user=True)["ActiveState"] != "active"
    assert selection()
    probe("www.baidu.com", "direct")
    probe("gfw.fixture.test", "proxy-alt")


def assert_off() -> None:
    assert read(JOURNAL)["phase"] == "off"
    state = properties("sbc-tun.service")
    assert state["ActiveState"] in {"inactive", "failed"}
    assert state["MainPID"] == "0"
    assert state["UnitFileState"] == "disabled"
    assert not Path("/etc/sbc/tun-control/blocked").exists()
    links = json.loads(run("ip", "-j", "link").stdout)
    assert not any(item["ifname"] == "sbc-tun0" for item in links)
    for family in ("-4", "-6"):
        routes = json.loads(
            run("ip", "-j", family, "route", "show", "table", "all").stdout
        )
        assert not any(item.get("dev") == "sbc-tun0" for item in routes)
    rules = json.loads(root("nft", "-j", "list", "ruleset").stdout)
    assert not any(
        item.get("table", {}).get("name") == "sing-box" for item in rules["nftables"]
    )


def evidence(name: str, **values) -> None:
    payload = {"name": name, "run_id": guard(), "boot_id": boot_id(), **values}
    save(EVIDENCE / f"{name}.json", payload)
    portal = urlsplit(ASSETS.joinpath("subscription").read_text().strip())
    hostname = portal.hostname
    if hostname != "10.0.2.100" or portal.scheme != "http":
        raise RuntimeError("Evidence must stay on the allowlisted fixture host")
    client = http.client.HTTPConnection(hostname, portal.port, timeout=10)
    try:
        client.request(
            "POST",
            "/fixture/lifecycle-evidence",
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert client.getresponse().status == 204
    finally:
        client.close()
    print(f"=== {'FAIL' if name == 'failure' else 'PASS'}: {name} ===", flush=True)


def reboot(phase: str) -> None:
    save(
        STATE,
        {
            "run_id": guard(),
            "phase": phase,
            "boot_id": boot_id(),
            "transaction": read(JOURNAL)["id"],
            "files": fingerprint(),
        },
    )
    print(f"TUN_LIFECYCLE_REBOOT={guard()}:{phase}", flush=True)
    root(SYSTEMCTL, "reboot")
    # systemd terminates this controller during a real shutdown.
    time.sleep(120)
    raise RuntimeError("Guest reboot did not terminate the controller")


def first_boot() -> None:
    assert not CONFIG.exists()
    assert not DATA.exists()
    assert not JOURNAL.exists()
    env = os.environ.copy()
    env["SBC_INSTALL_TTY"] = str(ASSETS / "subscription")
    installer = HOME / "install.sh"
    portal = ASSETS.joinpath("subscription").read_text().strip().split("/sub/")[0]
    run(
        "curl",
        "--noproxy",
        "*",
        "-fsS",
        "--max-time",
        "10",
        portal + "/install.sh",
        "-o",
        str(installer),
    )
    run("sh", str(installer), "--no-rc", "--shell", "bash", env=env)
    assert sbc("version").stdout.strip() == "sbc tun-e2e-old"
    assert "version 1.14.2" in run(str(DATA / "bin/sing-box"), "version").stdout
    assert properties("sbc.service", user=True)["ActiveState"] == "active"
    for action in ("refresh", "upgrade"):
        sbc(action, "auto", "off")
        sbc(action, "interval", "1m")
    evidence("hosted-install", initial_engine="1.14.2")
    sbc("tun", "on")
    sbc("route", "gfw-proxy")
    sbc("protocol", "naive")
    assert_active()
    before = fingerprint()
    old_engine = read(ASSETS / "engines.json")["1.14.2"]
    for name, digest in old_engine["files"].items():
        assert before[str(DATA / "bin" / name)] == digest
        assert before["/usr/local/lib/sbc-tun/" + name] == digest
    assert "version 1.14.2" in run("/usr/local/lib/sbc-tun/sing-box", "version").stdout
    assert (
        sbc(
            "install", input=ASSETS.joinpath("subscription").read_text(), ok=False
        ).returncode
        != 0
    )
    assert sbc("uninstall", "--yes", ok=False).returncode != 0
    assert fingerprint() == before
    evidence("active-install-uninstall-refusal")
    timer_and_upgrade(portal, before)
    reboot("committed")


def timer_and_upgrade(portal: str, before: dict) -> None:
    # A trap in the guest user's manager PATH detects sudo from real workers.
    deny = HOME / "deny-bin"
    deny.mkdir()
    (deny / "sudo").write_text(
        "#!/bin/sh\nprintf 'attempt\\n' >>/home/ubuntu/worker-sudo-attempts\nexit 126\n"
    )
    (deny / "sudo").chmod(0o700)
    run(
        SYSTEMCTL,
        "--user",
        "set-environment",
        f"PATH={deny}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    )
    run(
        "curl",
        "--noproxy",
        "*",
        "-fsS",
        "--max-time",
        "10",
        "-X",
        "POST",
        portal + "/fixture/release/new",
    )
    transaction, pid = read(JOURNAL)["id"], properties("sbc-tun.service")["MainPID"]
    started = time.time()
    for action in ("refresh", "upgrade"):
        sbc(action, "auto", "on")

    def workers_finished() -> bool:
        settings = read(CONFIG / "maintenance.json")
        return all(
            settings[a].get("last_success")
            and datetime.fromisoformat(settings[a]["last_success"]).timestamp()
            > started
            for a in ("refresh", "upgrade")
        )

    wait_for(workers_finished, seconds=180)
    triggered = {}
    for action, unit in (("refresh", "sbc-config-refresh"), ("upgrade", "sbc-upgrade")):
        timer = properties(unit + ".timer", user=True)
        service = properties(unit + ".service", user=True)
        assert timer["LastTriggerUSecMonotonic"] not in {"", "0"}
        assert service["ExecMainStatus"] == "0"
        assert service["Result"] == "success"
        triggered[action] = {"timer": timer, "service": service}
        sbc(action, "auto", "off")
    assert not HOME.joinpath("worker-sudo-attempts").exists()
    assert CONFIG.joinpath("tun-update-available").is_file()
    assert "updates are available" in sbc("status").stdout
    assert read(JOURNAL)["id"] == transaction
    assert properties("sbc-tun.service")["MainPID"] == pid
    assert fingerprint() == before
    assert_active()
    evidence(
        "native-timer-check-only",
        workers=triggered,
        unchanged_generation=True,
        sudo_attempts=0,
    )
    sbc("upgrade")
    assert read(JOURNAL)["id"] != transaction
    assert sbc("version").stdout.strip() == "sbc tun-e2e-new"
    assert DATA.joinpath("sing-box.version").read_text().strip() == "1.14.3"
    receipts = read(ASSETS / "engines.json")
    files = fingerprint()
    for name, digest in receipts["1.14.3"]["files"].items():
        assert files[str(DATA / "bin" / name)] == digest
        assert files["/usr/local/lib/sbc-tun/" + name] == digest
    assert "version 1.14.3" in run("/usr/local/lib/sbc-tun/sing-box", "version").stdout
    assert_active()
    assert not DATA.joinpath("upgrade-recovery.json").exists()
    assert not CONFIG.joinpath("tun-update-available").exists()
    evidence(
        "real-engine-upgrade",
        old=receipts["1.14.2"],
        new=receipts["1.14.3"],
        selections_preserved=True,
    )


def resumed(state: dict) -> None:
    assert state["run_id"] == guard()
    assert state["boot_id"] != boot_id()
    assert read(JOURNAL)["id"] == state["transaction"]
    assert fingerprint() == state["files"]
    phase = state["phase"]
    if phase == "committed":
        wait_for(selection)
        assert_active()
        evidence("committed-boot", same_generation=True, same_files=True)
        prepare_pending()
    repair_after_boot(phase, state)
    if phase == "pending":
        prepare_dirty()
    uninstall()


def prepare_pending() -> None:
    sbc("tun", "off")
    assert_off()
    # Pause immediately before the real commit helper. Do not forge a journal.
    wrapper = HOME / "sudo-pause"
    wrapper.write_text(
        '#!/bin/sh\nif [ "$1" = -- ] && [ "$3" = _tun-commit ]; then\n touch /home/ubuntu/commit-paused\n sleep 900\n exit 1\nfi\nexec /usr/bin/sudo "$@"\n'
    )
    root("install", "-m", "755", str(wrapper), "/usr/local/bin/sudo")
    with HOME.joinpath("pending-controller.log").open("w") as log:
        subprocess.Popen([str(SBC), "tun", "on"], stdout=log, stderr=log)
    wait_for(lambda: HOME.joinpath("commit-paused").exists())
    assert read(JOURNAL)["phase"] == "pending"
    assert not read(JOURNAL).get("committed", False)
    assert properties("sbc-tun.service")["ActiveState"] == "active"
    root("rm", "/usr/local/bin/sudo")
    evidence("real-pending-before-reboot")
    reboot("pending")


def repair_after_boot(phase: str, state: dict) -> None:
    assert phase in {"pending", "dirty"}
    assert read(JOURNAL)["phase"] == phase
    timer = "sbc-tun-recover-" + state["transaction"] + ".timer"
    assert properties(timer)["ActiveState"] != "active"
    # Pending is not boot-enabled. An explicit root start must also be refused.
    root(SYSTEMCTL, "start", "sbc-tun.service", ok=False)
    assert properties("sbc-tun.service")["ActiveState"] != "active"
    assert "status=1" in properties("sbc-tun.service")["ExecCondition"]
    assert Path("/etc/sbc/tun-control/blocked").exists()
    assert sbc("status", ok=False).returncode != 0
    assert sbc("on", ok=False).returncode != 0
    run(SYSTEMCTL, "--user", "start", "sbc.service", ok=False)
    assert properties("sbc.service", user=True)["ActiveState"] != "active"
    assert read(JOURNAL)["id"] == state["transaction"]
    sbc("tun", "off")
    assert_off()
    evidence(phase + "-boot-recovery", no_implicit_capture=True, mixed_blocked=True)


def prepare_dirty() -> None:
    sbc("tun", "on")
    sbc("route", "gfw-proxy")
    sbc("protocol", "naive")
    assert_active()
    wrapper = HOME / "systemctl-fail-stop"
    wrapper.write_text(
        '#!/bin/sh\nif [ -e /run/sbc-fixture-fail-stop ] && [ "$1" = disable ] && [ "$2" = --now ]; then exit 1; fi\nexec /usr/bin/systemctl "$@"\n'
    )
    root("install", "-m", "755", str(wrapper), "/usr/local/bin/systemctl")
    root("touch", "/run/sbc-fixture-fail-stop")
    assert sbc("tun", "off", ok=False).returncode != 0
    assert read(JOURNAL)["phase"] == "dirty"
    assert read(JOURNAL)["committed"] is True
    assert properties("sbc-tun.service")["ActiveState"] == "active"
    root("rm", "/usr/local/bin/systemctl")
    evidence("real-dirty-before-reboot")
    reboot("dirty")


def uninstall() -> None:
    assert read(JOURNAL)["committed"] is True
    # Confirm normal mixed use is possible after explicit repair.
    sbc("start")
    assert properties("sbc.service", user=True)["ActiveState"] == "active"
    sbc("uninstall", "--yes")
    assert not CONFIG.exists()
    assert not DATA.exists()
    for unit in ("sbc.service", "sbc-config-refresh.timer", "sbc-upgrade.timer"):
        assert properties(unit, user=True)["ActiveState"] != "active"
        assert not HOME.joinpath(".config/systemd/user", unit).exists()
    assert_off()
    assert Path("/etc/sbc/tun-control/recover-sbc").is_file()
    assert root(
        "find", "/etc/sbc", "-maxdepth", "1", "-name", "tun.prev-*"
    ).stdout.strip()
    evidence("off-reuse-and-uninstall", root_generations_retained=True)
    evidence(
        "summary",
        result="PASS",
        boots=4,
        gates=[
            "hosted-install",
            "native-timers",
            "real-engine-upgrade",
            "committed-boot",
            "pending-boot",
            "dirty-boot",
            "off-uninstall",
        ],
    )
    print(f"TUN_E2E_RESULT={guard()}:PASS", flush=True)
    root(SYSTEMCTL, "poweroff")


def main() -> None:
    identity = guard()
    os.umask(0o077)
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    if action in {"network", "bootstrap"}:
        try:
            if action == "network":
                network()
            else:
                bootstrap()
        except Exception:
            traceback.print_exc()
            print(f"TUN_E2E_RESULT={identity}:FAIL", flush=True)
            run(SYSTEMCTL, "--no-block", "poweroff", ok=False)
            raise
    elif action == "run":
        assert os.getuid() == 1000
        assert os.geteuid() == 1000
        os.environ.update(
            HOME=str(HOME),
            XDG_CONFIG_HOME=str(HOME / ".config"),
            XDG_DATA_HOME=str(HOME / ".local/share"),
            XDG_RUNTIME_DIR="/run/user/1000",
            SBC_LANG="en",
        )
        for key in list(os.environ):
            if key.lower().endswith("_proxy"):
                del os.environ[key]
        EVIDENCE.mkdir(mode=0o700, exist_ok=True)
        try:
            root("resolvectl", "dns", "sbcf-main", "10.77.0.2")
            root("resolvectl", "default-route", "sbcf-main", "yes")
            wait_for(
                lambda: (
                    run(SYSTEMCTL, "--user", "show-environment", ok=False).returncode
                    == 0
                )
            )
            if STATE.exists():
                resumed(read(STATE))
            else:
                first_boot()
        except Exception as error:
            traceback.print_exc()
            print(f"TUN_E2E_RESULT={identity}:FAIL", flush=True)
            try:
                evidence("failure", error=str(error), traceback=traceback.format_exc())
            finally:
                root(SYSTEMCTL, "--no-block", "poweroff", ok=False)
            raise
    else:
        raise RuntimeError("Unknown lifecycle action")


if __name__ == "__main__":
    main()
