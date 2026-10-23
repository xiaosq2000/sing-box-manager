"""Run G0 against an isolated VM and private controller subprocesses.

No production inventory, host service, firewall or SSH configuration is used.
The PostgreSQL, HTTPS and forced-command SSH processes bind only private sockets
or loopback ports. The guest cannot access the public network.
"""

import argparse
import contextlib
import hashlib
import http.client
import json
import os
import pwd
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CREDENTIAL = "33333333-3333-4333-8333-333333333333"
ENV = {
    key: os.environ[key]
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "LD_LIBRARY_PATH")
    if key in os.environ
}
ENV.update({"PYTHONPATH": str(ROOT), "CGO_ENABLED": "0", "GOTOOLCHAIN": "local"})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def write(path, value, mode=0o600):
    path.write_bytes(value.encode() if isinstance(value, str) else value)
    path.chmod(mode)


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CommandFailure(RuntimeError):
    def __init__(self, category, returncode=None):
        super().__init__("command failed; inspect private run logs")
        self.category = category
        self.returncode = returncode


class StateTimeout(RuntimeError):
    pass


ERROR_CATEGORIES = {
    "command-exit",
    "command-timeout",
    "ssh-host-key",
    "ssh-authentication",
    "ssh-refused",
    "ssh-timeout",
    "ssh-transport",
    "guest-not-ready",
}
PHASES = {
    "build",
    "database",
    "controller",
    "control-ssh",
    "guest-boot",
    "guest-ready",
    "install",
    "initial-apply",
    "client-start",
    "first-traffic",
    "repeat-install",
    "next-generation",
    "second-traffic",
    "accounting",
    "result",
}
PROCESS_NAMES = {"https", "control-ssh", "qemu", "client"}


def ssh_category(returncode, stderr):
    # Match fixed OpenSSH errors. Never return captured text or a remote value.
    if returncode != 255:
        return "guest-not-ready"
    for marker, category in (
        (b"Host key verification failed", "ssh-host-key"),
        (b"REMOTE HOST IDENTIFICATION HAS CHANGED", "ssh-host-key"),
        (b"Permission denied", "ssh-authentication"),
        (b"Connection refused", "ssh-refused"),
        (b"Connection timed out", "ssh-timeout"),
        (b"Connection timed out during banner exchange", "ssh-timeout"),
    ):
        if marker in stderr:
            return category
    return "ssh-transport"


def qemu_diagnostic(root):
    def contents(name):
        try:
            with (root / name).open("rb") as file:
                return file.read(65536).lower()
        except OSError:
            return b""

    output = contents("qemu.log")
    category = "unclassified" if output else "empty"
    for markers, candidate in (
        (
            (b"kvm: entry failed", b"kvm internal error", b"kvm run failed"),
            "kvm-run-error",
        ),
        (
            (
                b"failed to initialize kvm",
                b"could not access kvm",
                b"kvm is not supported",
            ),
            "kvm-init-error",
        ),
        ((b"host forwarding rule", b"address already in use"), "host-forward-error"),
        ((b"guest forwarding rule", b"guestfwd"), "guest-forward-error"),
        (
            (
                b"failed to get",
                b"failed to lock",
                b"is another process using the image",
            ),
            "drive-lock-error",
        ),
        (
            (b"could not open", b"could not read", b"image is not in qcow2 format"),
            "drive-open-error",
        ),
        (
            (
                b"cannot set up guest memory",
                b"cannot allocate memory",
                b"failed to allocate",
            ),
            "memory-error",
        ),
        ((b"could not load pc bios", b"could not find rom image"), "firmware-error"),
        (
            (b"invalid option", b"invalid parameter", b"unsupported machine type"),
            "option-error",
        ),
        ((b"terminating on signal",), "signal"),
    ):
        if any(marker in output for marker in markers):
            category = candidate
            break
    serial = contents("serial.log")
    return {
        "log_category": category,
        "boot_markers": {
            name: marker in serial
            for name, marker in (
                ("kernel-start", b"linux version"),
                ("kernel-panic", b"kernel panic"),
                ("reboot", b"reboot: restarting system"),
                ("poweroff", b"power down"),
                ("cloud-init", b"cloud-init"),
                ("login", b"node-g0 login:"),
            )
        },
    }


def safe_error(error):
    if isinstance(error, CommandFailure):
        category = (
            error.category if error.category in ERROR_CATEGORIES else "command-exit"
        )
        result = {"category": category}
        if type(error.returncode) is int:
            result["returncode"] = error.returncode
        return result
    if isinstance(error, StateTimeout):
        return {"category": "state-timeout"}
    if isinstance(error, ssl.SSLError):
        return {"category": "tls-error"}
    if isinstance(error, OSError):
        return {"category": "os-error"}
    return {"category": "fixture-error"}


class Diagnostics:
    def __init__(self):
        self.current = "build"
        self.acceleration = "not-selected"
        self.guest_attempts = 0
        self.guest_last = None

    def phase(self, name):
        if name not in PHASES:
            raise ValueError("invalid diagnostic phase")
        self.current = name
        print(f"G0 phase: {name}", flush=True)

    def failure(self, error, processes, root=None):
        result = {
            "phase": self.current if self.current in PHASES else "unknown",
            **safe_error(error),
            "acceleration": self.acceleration
            if self.acceleration in {"kvm", "tcg"}
            else "not-selected",
            "guest_attempts": self.guest_attempts,
            "processes": {
                name: {"returncode": process.poll()}
                for name, process in processes.items()
                if name in PROCESS_NAMES
            },
        }
        if self.guest_last is not None:
            result["guest_last"] = safe_error(self.guest_last)
        if root is not None and "qemu" in processes:
            result["qemu"] = qemu_diagnostic(root)
        print("G0 diagnostic: " + json.dumps(result, sort_keys=True), flush=True)


def run(argv, *, stdin=None, env=None, cwd=ROOT, timeout=120, log=None):
    try:
        result = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            cwd=cwd,
            env=env or ENV,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        if log is not None:
            write(log, (error.stdout or b"") + (error.stderr or b""))
        raise CommandFailure("command-timeout") from None
    if log is not None:
        write(log, result.stdout + result.stderr)
    if result.returncode:
        category = (
            ssh_category(result.returncode, result.stderr)
            if argv[0] == "ssh"
            else "command-exit"
        )
        raise CommandFailure(category, result.returncode)
    return result.stdout


def wait(predicate, seconds=90):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.25)
    raise StateTimeout("timed out waiting for fixture state")


def reachable(number):
    try:
        with socket.create_connection(("127.0.0.1", number), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for_guest(guest, diagnostics, log, seconds=180):
    def ready():
        diagnostics.guest_attempts += 1
        try:
            ready = (
                guest(
                    "test -f /etc/sbm-node-g0-disposable && systemctl is-active --quiet sbm-g0-target",
                    log=log,
                )
                == b""
            )
            diagnostics.guest_last = (
                None if ready else CommandFailure("guest-not-ready")
            )
            return ready
        except CommandFailure as error:
            diagnostics.guest_last = error
            return False

    wait(ready, seconds)


def certificate(root, name, san):
    run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-subj",
            f"/CN={name}.fixture.test",
            "-addext",
            f"subjectAltName={san}",
            "-keyout",
            str(root / f"{name}.key"),
            "-out",
            str(root / f"{name}.crt"),
        ],
        log=root / f"{name}-certificate.log",
    )


def ssh_key(root, name):
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / name)])
    return (root / f"{name}.pub").read_text().strip()


def engine_bundle(secret, generation=1):
    from sing_box_manager.release.profiles import build_server_config
    from sing_box_manager.settings import Settings

    data = yaml.safe_load((ROOT / "config/inventory/example.yaml").read_text())
    data["default_protocol"] = "trojan"
    data["deployment"].update(
        {
            "host": "node.fixture.test",
            "ip": "127.0.0.1",
            "trojan_port": 8443,
            "tls": {
                "enabled": True,
                "server_name": "node.fixture.test",
                "self_signed_cert": False,
                "certificate_path": "/etc/sbm-node-g0/node.crt",
                "key_path": "/etc/sbm-node-g0/node.key",
            },
        }
    )
    for section in ("web_portal", "hysteria2", "naive"):
        data[section]["users"] = []
    data["snell"] = {"users": []}
    data["deployment"].pop("snell_port", None)
    data["trojan"]["users"] = [{"username": "fixturealice", "password": secret}]
    data["traffic_stats"] = {
        "enabled": True,
        "api_listen": "127.0.0.1:19080",
        "connection_api_listen": "127.0.0.1:19090",
    }
    settings = Settings.model_validate(data)
    config = build_server_config(settings, settings.traffic_stats)
    config["inbounds"] = [
        item for item in config["inbounds"] if item["type"] == "trojan"
    ]
    config["log"]["level"] = "warn" if generation == 1 else "error"
    # Fixture-only exception: one loopback HTTP target in this isolated guest.
    # All production-builder reserved-destination checks remain below it.
    config["route"]["rules"].insert(
        0,
        {
            "ip_cidr": ["127.0.0.1/32"],
            "port": 18080,
            "action": "route",
            "outbound": "direct",
        },
    )
    return {
        "schema": 1,
        "node_id": "",
        "generation": generation,
        "config": config,
        "config_sha256": hashlib.sha256(canonical(config)).hexdigest(),
        "stats_address": "127.0.0.1:19080",
        "roster": [{"credential_id": CREDENTIAL, "wire_name": "trojan:fixturealice"}],
    }


def source_fingerprint():
    digest = hashlib.sha256()
    files = [ROOT / "pixi.toml", ROOT / "pixi.lock", ROOT / "tests/test_node_g0.py"]
    for directory in ("node-agent", "tests/node_g0", "scripts/dev/node-g0"):
        files.extend(
            path
            for path in (ROOT / directory).rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc"}
        )
    for path in sorted(files):
        digest.update(
            str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes() + b"\0"
        )
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument(
        "--image",
        type=Path,
        default=Path.home() / ".cache/sbc-tun-e2e/noble-server-cloudimg-amd64.img",
    )
    args = parser.parse_args()
    if os.environ.get("SBM_NODE_G0") != "1" or os.geteuid() == 0:
        parser.error("set SBM_NODE_G0=1 and run as an unprivileged account")
    for tool in (
        "qemu-system-x86_64",
        "qemu-img",
        "nc",
        "ssh",
        "ssh-keygen",
        "openssl",
        "initdb",
        "pg_ctl",
        "psql",
        "go",
        "ansible-playbook",
    ):
        if shutil.which(tool) is None:
            parser.error(f"missing {tool}; use the node-g0 Pixi environment")
    engine, image = args.engine.resolve(), args.image.resolve()
    for path, checksum in (
        (engine, engine.with_suffix(".sha256")),
        (image, image.with_suffix(image.suffix + ".sha256")),
    ):
        if not path.is_file() or not checksum.is_file():
            parser.error(
                "verified cached engine and cloud image are required; no automatic download"
            )
        expected = checksum.read_text().split()[0]
        with path.open("rb") as file:
            actual = hashlib.file_digest(file, "sha256").hexdigest()
        if len(expected) != 64 or actual != expected:
            parser.error("cached artifact checksum mismatch")
    if "with_v2ray_api" not in run([str(engine), "version"]).decode():
        parser.error("the engine must include with_v2ray_api")
    cache = Path.home() / ".cache/sbm-node-g0/runs"
    cache.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix="g0-", dir=cache))
    print(f"Private G0 artifacts: {root}", flush=True)
    env = {
        **ENV,
        "SBM_NODE_G0_DIR": str(root),
        "DJANGO_SETTINGS_MODULE": "tests.node_g0.settings",
    }
    processes = {}
    diagnostics = Diagnostics()
    streams = []
    pg_started = False
    seed = None

    def start(argv, name, *, process_env=env):
        if name not in PROCESS_NAMES:
            raise ValueError("invalid fixture process name")
        file = (root / f"{name}.log").open("wb")
        streams.append(file)
        process = subprocess.Popen(
            argv, cwd=ROOT, env=process_env, stdout=file, stderr=subprocess.STDOUT
        )
        processes[name] = process
        return process

    def control(action, stdin=None):
        return run(
            [sys.executable, "-m", "tests.node_g0.control", action],
            env=env,
            stdin=stdin,
        )

    try:
        diagnostics.phase("build")
        run(
            [
                "go",
                "build",
                "-trimpath",
                "-o",
                str(root / "sbm-node-g0"),
                "./cmd/sbm-node-g0",
            ],
            cwd=ROOT / "node-agent",
            timeout=300,
            log=root / "build.log",
        )
        diagnostics.phase("database")
        socket_dir = root / "pgsocket"
        socket_dir.mkdir()
        run(
            [
                "initdb",
                "-D",
                str(root / "pgdata"),
                "--username=postgres",
                "--auth-local=trust",
                "--auth-host=reject",
                "--no-locale",
                "--encoding=UTF8",
            ],
            log=root / "initdb.log",
        )
        run(
            [
                "pg_ctl",
                "-D",
                str(root / "pgdata"),
                "-l",
                str(root / "postgres.log"),
                "-w",
                "-o",
                f"-k {socket_dir} -h '' -p 5432",
                "start",
            ]
        )
        pg_started = True
        run(
            [
                "psql",
                "-X",
                "-h",
                str(socket_dir),
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "CREATE DATABASE node_g0",
            ]
        )
        diagnostics.phase("controller")
        https_port, control_ssh, guest_ssh, guest_proxy, seed_port, mixed_port = [
            port() for _ in range(6)
        ]
        write(
            root / "control.json",
            canonical(
                {
                    "socket_dir": str(socket_dir),
                    "django_secret": secrets.token_hex(32),
                    "https_port": https_port,
                }
            ),
        )
        password = secrets.token_urlsafe(32)
        write(root / "bundle.json", canonical(engine_bundle(password)))
        write(root / "bundle-next.json", canonical(engine_bundle(password, 2)))
        certificate(root, "control", "IP:127.0.0.1,IP:10.0.2.100")
        certificate(root, "node", "DNS:node.fixture.test")
        control("bootstrap")
        start([sys.executable, "-m", "tests.node_g0.control", "serve"], "https")
        wait(lambda: reachable(https_port))
        probe = http.client.HTTPSConnection(
            "127.0.0.1",
            https_port,
            timeout=5,
            context=ssl.create_default_context(cafile=str(root / "control.crt")),
        )
        probe.request("GET", "/node-g0/v1/config")
        if probe.getresponse().status != 401:
            raise RuntimeError("HTTPS authentication preflight failed")
        probe.close()
        diagnostics.phase("control-ssh")
        operator_public = ssh_key(root, "operator")
        control_public = ssh_key(root, "control-host")
        guest_public = ssh_key(root, "guest-host")
        write(root / "authorized_keys", operator_public + "\n")
        wrapper = (
            "#!/bin/sh\n"
            + f"cd {shlex.quote(str(ROOT))}\nexport SBM_NODE_G0_DIR={shlex.quote(str(root))}\nexec {shlex.quote(sys.executable)} -m tests.node_g0.control register\n"
        )
        write(root / "register", wrapper, 0o700)
        username = pwd.getpwuid(os.getuid()).pw_name
        write(
            root / "sshd_config",
            f"""Port {control_ssh}
ListenAddress 127.0.0.1
HostKey {root}/control-host
PidFile {root}/sshd.pid
AuthorizedKeysFile {root}/authorized_keys
AllowUsers {username}
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitRootLogin no
StrictModes yes
ForceCommand {root}/register
AllowTcpForwarding no
X11Forwarding no
PermitTTY no
LogLevel ERROR
""",
        )
        start(
            ["/usr/sbin/sshd", "-D", "-e", "-f", str(root / "sshd_config")],
            "control-ssh",
        )
        wait(lambda: reachable(control_ssh), 10)
        write(
            root / "known_hosts",
            f"[127.0.0.1]:{control_ssh} {control_public}\n[127.0.0.1]:{guest_ssh} {guest_public}\n",
        )
        ssh_options = [
            "-F",
            "/dev/null",
            "-i",
            str(root / "operator"),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={root}/known_hosts",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "LogLevel=ERROR",
        ]
        diagnostics.phase("guest-boot")
        target_script = """from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = b'G0-fixture-' * 8192
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1',18080),Handler).serve_forever()
"""
        cloud = {
            "package_update": False,
            "ssh_pwauth": False,
            "users": [
                {
                    "name": "ubuntu",
                    "sudo": "ALL=(ALL) NOPASSWD:ALL",
                    "shell": "/bin/bash",
                    "lock_passwd": True,
                    "ssh_authorized_keys": [operator_public],
                }
            ],
            "ssh_keys": {
                "ed25519_private": (root / "guest-host").read_text(),
                "ed25519_public": guest_public,
            },
            "write_files": [
                {
                    "path": "/etc/sbm-node-g0-disposable",
                    "permissions": "0600",
                    "content": root.name,
                },
                {
                    "path": "/opt/g0-target.py",
                    "permissions": "0600",
                    "content": target_script,
                },
            ],
            "runcmd": [
                [
                    "systemd-run",
                    "--unit=sbm-g0-target",
                    "/usr/bin/python3",
                    "/opt/g0-target.py",
                ]
            ],
        }
        seed_data = {
            "/user-data": b"#cloud-config\n" + canonical(cloud),
            "/meta-data": f"instance-id: {root.name}\nlocal-hostname: node-g0\n".encode(),
            "/vendor-data": b"#cloud-config\n{}\n",
        }

        class SeedHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                value = seed_data.get(self.path)
                self.send_response(200 if value else 404)
                self.send_header("Content-Length", str(len(value) if value else 0))
                self.end_headers()
                if value:
                    self.wfile.write(value)

            def log_message(self, format, *args):
                with (root / "seed.log").open("a") as log:
                    log.write((format % args) + "\n")

        write(root / "user-data", seed_data["/user-data"])
        write(root / "meta-data", seed_data["/meta-data"])
        seed = HTTPServer(("127.0.0.1", seed_port), SeedHandler)
        threading.Thread(target=seed.serve_forever, daemon=True).start()
        run(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(image),
                str(root / "disk.qcow2"),
            ]
        )
        run(["qemu-img", "resize", str(root / "disk.qcow2"), "8G"])
        accel = (
            run([sys.executable, str(ROOT / "scripts/dev/tun-vm/select-accel.py")])
            .decode()
            .strip()
        )
        if accel not in {"kvm", "tcg"}:
            raise RuntimeError("invalid VM accelerator result")
        diagnostics.acceleration = accel
        print(f"G0 acceleration: {accel}", flush=True)
        net = f"user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:{guest_ssh}-:22,hostfwd=tcp:127.0.0.1:{guest_proxy}-:8443,guestfwd=tcp:10.0.2.100:{seed_port}-cmd:nc -N 127.0.0.1 {seed_port},guestfwd=tcp:10.0.2.100:{https_port}-cmd:nc -N 127.0.0.1 {https_port}"
        start(
            [
                "qemu-system-x86_64",
                "-machine",
                f"accel={accel}",
                "-cpu",
                "max",
                "-m",
                "2048",
                "-smp",
                "2",
                "-drive",
                f"file={root}/disk.qcow2,if=virtio,format=qcow2",
                "-smbios",
                f"type=1,serial=ds=nocloud-net;s=http://10.0.2.100:{seed_port}/",
                "-netdev",
                net,
                "-device",
                "virtio-net-pci,netdev=net0",
                "-display",
                "none",
                "-serial",
                f"file:{root}/serial.log",
                "-monitor",
                "none",
                "-no-reboot",
            ],
            "qemu",
        )

        def guest(command, *, log=None):
            return run(
                [
                    "ssh",
                    *ssh_options,
                    "-p",
                    str(guest_ssh),
                    "ubuntu@127.0.0.1",
                    command,
                ],
                timeout=15,
                log=log,
            )

        diagnostics.phase("guest-ready")
        wait_for_guest(guest, diagnostics, root / "guest-ready-last.log")
        diagnostics.phase("install")
        inventory = {
            "all": {
                "hosts": {
                    "localhost": {
                        "ansible_connection": "local",
                        "ansible_python_interpreter": sys.executable,
                    }
                },
                "children": {
                    "node": {
                        "hosts": {
                            "g0": {
                                "ansible_host": "127.0.0.1",
                                "ansible_port": guest_ssh,
                                "ansible_user": "ubuntu",
                                "ansible_python_interpreter": "/usr/bin/python3",
                                "ansible_ssh_private_key_file": str(root / "operator"),
                                "ansible_ssh_common_args": shlex.join(
                                    [
                                        "-F",
                                        "/dev/null",
                                        "-o",
                                        "IdentitiesOnly=yes",
                                        "-o",
                                        "StrictHostKeyChecking=yes",
                                        "-o",
                                        f"UserKnownHostsFile={root}/known_hosts",
                                    ]
                                ),
                            }
                        }
                    }
                },
            }
        }
        write(root / "inventory.yaml", yaml.safe_dump(inventory))
        variables = {
            "g0_run_dir": str(root),
            "g0_agent_binary": str(root / "sbm-node-g0"),
            "g0_engine_binary": str(engine),
            "g0_control_origin": f"https://10.0.2.100:{https_port}",
            "g0_control_ssh_argv": [
                "ssh",
                *ssh_options,
                "-p",
                str(control_ssh),
                f"{username}@127.0.0.1",
            ],
        }
        write(root / "vars.json", canonical(variables))
        playbook = [
            "ansible-playbook",
            "-i",
            str(root / "inventory.yaml"),
            str(ROOT / "scripts/dev/node-g0/playbook.yaml"),
            "--extra-vars",
            f"@{root}/vars.json",
        ]
        write(
            root / "ansible.cfg",
            f"[defaults]\nlocal_tmp={root}/ansible-tmp\nretry_files_enabled=False\nhost_key_checking=True\nforks=1\n[ssh_connection]\ncontrol_path_dir={root}/ssh-control\n",
        )
        ansible_env = {**env, "ANSIBLE_CONFIG": str(root / "ansible.cfg")}
        run(playbook, env=ansible_env, timeout=180, log=root / "ansible-first.log")

        def report():
            return json.loads(control("report"))

        diagnostics.phase("initial-apply")
        wait(lambda: report()["nodes"][0]["applied_generation"] == 1)
        before = report()["nodes"][0]["invocation"]
        diagnostics.phase("client-start")
        client_config = {
            "log": {"level": "error"},
            "inbounds": [
                {"type": "mixed", "listen": "127.0.0.1", "listen_port": mixed_port}
            ],
            "outbounds": [
                {
                    "type": "trojan",
                    "tag": "proxy",
                    "server": "127.0.0.1",
                    "server_port": guest_proxy,
                    "password": password,
                    "tls": {
                        "enabled": True,
                        "server_name": "node.fixture.test",
                        "certificate_path": str(root / "node.crt"),
                    },
                }
            ],
            "route": {"final": "proxy"},
        }
        write(root / "client.json", canonical(client_config))
        start(
            [str(engine), "run", "-D", str(root), "-c", str(root / "client.json")],
            "client",
        )
        wait(lambda: reachable(mixed_port), 15)

        def fetch():
            connection = http.client.HTTPConnection("127.0.0.1", mixed_port, timeout=10)
            try:
                connection.request("GET", "http://127.0.0.1:18080/g0")
                response = connection.getresponse()
                body = response.read()
                if response.status != 200 or body != b"G0-fixture-" * 8192:
                    raise RuntimeError("proxy payload mismatch")
                return len(body)
            finally:
                connection.close()

        diagnostics.phase("first-traffic")
        first_bytes = fetch()
        wait(
            lambda: (
                bool(report()["usage"])
                and report()["usage"][0]["download"] >= first_bytes
            )
        )
        diagnostics.phase("repeat-install")
        run(playbook, env=ansible_env, timeout=180, log=root / "ansible-repeat.log")
        if report()["nodes"][0]["invocation"] != before:
            raise RuntimeError("repeat deployment restarted the proxy")
        diagnostics.phase("next-generation")
        control("publish")
        wait(lambda: report()["nodes"][0]["applied_generation"] == 2)
        if report()["nodes"][0]["invocation"] == before:
            raise RuntimeError("configuration change did not replace the engine")
        diagnostics.phase("second-traffic")
        second_bytes = fetch()
        wait(lambda: report()["usage"][0]["download"] >= first_bytes + second_bytes)
        diagnostics.phase("accounting")
        # Wait for the final observed counters, then compare exact durable buckets.
        time.sleep(3)
        local = json.loads(
            guest(
                "sudo /usr/bin/python3 -c "
                + shlex.quote(
                    "import json,sqlite3; db=sqlite3.connect('/var/lib/sbm-node-g0/traffic.sqlite3'); print(json.dumps(db.execute('SELECT version,upload,download FROM usage').fetchall()))"
                )
            )
        )
        final = report()
        central = final["usage"][0]
        if local != [[central["version"], central["upload"], central["download"]]]:
            raise RuntimeError("local and central durable accounting differ")
        diagnostics.phase("result")
        result = {
            "gate": "G0",
            "result": "PASS",
            "commit": run(["git", "rev-parse", "HEAD"]).decode().strip(),
            "dirty_tree": bool(run(["git", "status", "--porcelain"]).strip()),
            "source_fingerprint": source_fingerprint(),
            "acceleration": accel,
            "checks": [
                "cgo-free agent build",
                "Ansible install over checked SSH",
                "SSH digest registration",
                "verified HTTPS config pull",
                "real Trojan traffic",
                "local SQLite and PostgreSQL equality",
                "repeat install without engine restart",
                "generation 2 application and traffic",
            ],
            "accounting": {
                "version": central["version"],
                "upload": central["upload"],
                "download": central["download"],
            },
            "limits": [
                "controller runs as isolated host subprocesses, node runs in a VM",
                "one synthetic account and Trojan only",
                "G1 crash/reboot, locking, startup identity and final-sample checks are not established",
                "no production command, account migration, client release or public-network acceptance",
            ],
        }
        write(root / "result.json", canonical(result) + b"\n")
        print(
            "G0 PASS: SSH/Ansible -> HTTPS -> real Trojan -> SQLite -> PostgreSQL",
            flush=True,
        )
    except Exception as error:
        diagnostics.failure(error, processes, root)
        write(root / "failure.log", traceback.format_exc())
        raise RuntimeError(
            "fixture failed; inspect the retained private logs"
        ) from None
    finally:
        if "guest" in locals() and (root / "inventory.yaml").exists():
            with contextlib.suppress(Exception):
                write(
                    root / "agent-journal.log",
                    guest(
                        "sudo journalctl -u sbm-g0-agent -u sbm-g0-proxy --no-pager -n 80"
                    ),
                )
            with contextlib.suppress(Exception):
                write(
                    root / "guest-https.log",
                    guest(
                        f"timeout 8 curl --noproxy '*' --cacert /etc/sbm-node-g0/control.crt --max-time 5 -sS -o /dev/null -w '%{{http_code}}' https://10.0.2.100:{https_port}/node-g0/v1/config"
                    ),
                )
        for process in reversed(list(processes.values())):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        if seed is not None:
            seed.shutdown()
            seed.server_close()
        for file in streams:
            file.close()
        if pg_started:
            with contextlib.suppress(Exception):
                run(
                    [
                        "pg_ctl",
                        "-D",
                        str(root / "pgdata"),
                        "-m",
                        "immediate",
                        "-w",
                        "stop",
                    ]
                )
        print(f"Retained private VM, environment and evidence: {root}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"G0 failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
