"""Check native control-plane deployment only on a disposable GitHub runner."""

# ruff: noqa: INP001 -- standalone CI script, not an importable package

import os
import secrets
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path("/opt/sbm-control-plane-smoke")
SOURCE = Path(__file__).resolve().parents[2]


def command(value: str) -> None:
    # Match the root shell used by the real SSH transport, including redirection.
    subprocess.run(["sudo", "bash", "-eu", "-c", value], check=True)


def local_ssh(_hostname, value, _error, *, dry_run=False):
    if dry_run:
        raise RuntimeError("The native smoke test must not use a dry run")
    command(value)


def local_write(_hostname, path, content, *, mode=None, dry_run=False):
    if dry_run:
        raise RuntimeError("The native smoke test must not use a dry run")
    subprocess.run(
        ["sudo", "tee", path],
        input=content.encode(),
        stdout=subprocess.DEVNULL,
        check=True,
    )
    command(f"sudo chmod {mode or '0644'} {shlex.quote(path)}")


def native_os(_hostname):
    command(". /etc/os-release; case $ID in ubuntu|debian) ;; *) exit 1 ;; esac")


def write_runtime(config, settings):
    local_write(
        "fixture-local",
        str(config),
        yaml.safe_dump(
            settings.model_dump(mode="json", by_alias=True, exclude={"config_path"})
        ),
        mode="0640",
    )
    command(f"chown root:sbm {config}")


def check_authoritative_node(settings, config, env):
    """Exercise peer-authenticated root sync, the real installer, and path activation."""
    from sing_box_manager.control_plane import deployment
    from sing_box_manager.deploy import node_install_command

    package = ROOT / "fixture-server"
    node = Path("/usr/local/etc/sing-box/config.json")
    command(
        "apt-get update -qq && apt-get install -y -qq nginx openssl && "
        f"cp -a {ROOT}/scripts {package} && "
        f"cp {SOURCE}/.cache/sing-box-manager/bin/sing-box {package}/sing-box && "
        f"openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost "
        f"-keyout {ROOT}/config/generated/node.key -out {ROOT}/config/generated/node.crt "
        ">/dev/null 2>&1"
    )
    local_write(
        "fixture-local",
        deployment.NGINX_SITE_PATH,
        "server {\n    listen 127.0.0.1:48080;\n    server_name localhost;\n"
        "    access_log off;\n    location / {\n        return 418;\n    }\n}\n",
    )
    command(
        f"ln -s {deployment.NGINX_SITE_PATH} /etc/nginx/sites-enabled/sbm-smoke && "
        "nginx -t && systemctl restart nginx"
    )
    command(deployment.management_command(str(ROOT), "import-inventory"))
    subprocess.run(
        env,
        input=b"""
import django
django.setup()
from sing_box_manager.control_plane.models import Account, Credential
account = Account.objects.create(username='native_only', portal_password_hash=Account.objects.get(username='alice').portal_password_hash)
for protocol in ('trojan', 'hysteria2', 'naive'):
    Credential.objects.create(account=account, protocol=protocol, secret='synthetic-native-secret')
""",
        check=True,
        cwd=ROOT,
    )
    root_env = [env[0], *env[3:]]  # Keep the same Django environment, without -u sbm.
    subprocess.run(
        root_env,
        input=f"""
import json
from pathlib import Path
import django
django.setup()
from sing_box_manager.control_plane import django_settings, services
from sing_box_manager.release.profiles import build_server_config
runtime = django_settings.runtime
Path('{package}/config.json').write_text(json.dumps(build_server_config(runtime)))
services.sync_node_config(runtime, restart_service=False)
""".encode(),
        check=True,
        cwd=ROOT,
    )
    install = node_install_command(str(ROOT), str(package), [], authoritative=True)
    command(install)
    settings.control_plane.authoritative = True
    write_runtime(config, settings)
    pid_command = [
        "sudo",
        "systemctl",
        "show",
        "-p",
        "MainPID",
        "--value",
        "sing-box.service",
    ]
    for _ in range(2):
        deployment.pause_node_sync("fixture-local")
        command(install)
        pid = subprocess.check_output(pid_command, text=True).strip()
        deployment.deploy("fixture-local", str(ROOT), settings)
        if pid == "0" or subprocess.check_output(pid_command, text=True).strip() != pid:
            raise RuntimeError(
                "An unchanged authoritative deploy restarted the node again"
            )
        command(f"grep -q native_only {node}")
    command(
        'test "$(curl --noproxy "*" -s -o /dev/null -w "%{http_code}" http://127.0.0.1:48080/install.sh)" = 200'
    )
    for enabled in (False, True):
        subprocess.run(
            env,
            input=f"""
import django
django.setup()
from sing_box_manager.control_plane import django_settings, services
from sing_box_manager.control_plane.models import Account
Account.objects.filter(username='native_only').update(enabled={enabled!r})
services.request_node_sync(django_settings.runtime)
""".encode(),
            check=True,
            cwd=ROOT,
        )
        wait = f"""
import subprocess, time
from pathlib import Path
for _ in range(150):
    state = subprocess.check_output(['systemctl', 'show', '-p', 'ActiveState', '--value', '{deployment.SYNC_SERVICE_NAME}'], text=True).strip()
    if ('native_only' in Path('{node}').read_text()) == {enabled!r} and state == 'inactive':
        subprocess.run(['systemctl', 'is-active', '--quiet', 'sing-box.service'], check=True)
        break
    time.sleep(.2)
else:
    raise RuntimeError('The systemd watcher did not apply the account change')
"""
        command(f"{ROOT}/.pixi/envs/default/bin/python -c {shlex.quote(wait)}")
    deployment.pause_node_sync("fixture-local")
    settings.control_plane.authoritative = False
    write_runtime(config, settings)
    command(node_install_command(str(ROOT), str(package), [], authoritative=False))
    deployment.deploy("fixture-local", str(ROOT), settings)
    command(f"! grep -q native_only {node}")
    command(f"! systemctl is-active --quiet {deployment.SYNC_PATH_NAME}")
    command(
        'test "$(curl --noproxy "*" -s -o /dev/null -w "%{http_code}" http://127.0.0.1:48080/install.sh)" = 418'
    )
    subprocess.run(
        env,
        input=b"""
import django
django.setup()
from sing_box_manager.control_plane.models import Account
assert Account.objects.filter(username='native_only').exists()
""",
        check=True,
        cwd=ROOT,
    )


def fixture_settings(config):
    from sing_box_manager.settings import ControlPlaneSettings, load_settings

    settings = load_settings(SOURCE / "config/inventory/example.yaml")
    settings.config_path = config
    settings.control_plane = ControlPlaneSettings(
        enabled=True,
        secret_key=secrets.token_hex(32),
        database_name="sbm_native_smoke",
    )
    settings.traffic_stats.enabled = False  # This job uses the official server binary.
    settings.web.subscription_secret = secrets.token_hex(32)
    settings.deployment.host = "localhost"
    settings.deployment.ip = "127.0.0.1"
    settings.deployment.tls.key_path = str(ROOT / "config/generated/node.key")
    settings.deployment.tls.certificate_path = str(ROOT / "config/generated/node.crt")
    return settings


def main() -> None:
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("SBM_CP_NATIVE_E2E") != "1"
    ):
        raise RuntimeError(
            "This test requires an explicitly enabled disposable GitHub runner"
        )
    if ROOT.exists():
        raise RuntimeError("Refusing to reuse an existing native smoke-test directory")
    sys.path.insert(0, str(SOURCE))
    from sing_box_manager.control_plane import deployment
    from sing_box_manager.provision import _create_service_user_and_root

    pixi = shutil.which("pixi")
    if pixi is None:
        raise RuntimeError("Pixi is required")
    with patch("sing_box_manager.provision._ssh", local_ssh):
        _create_service_user_and_root("fixture-local", str(ROOT), dry_run=False)
    for name in (
        "sing_box_manager",
        "scripts",
        "pixi.toml",
        "pixi.lock",
        "pyproject.toml",
        "config/inventory/example.yaml",
    ):
        destination = ROOT / name
        command(
            f"sudo mkdir -p {shlex.quote(str(destination.parent))} && sudo cp -a {shlex.quote(str(SOURCE / name))} {shlex.quote(str(destination))}"
        )
    # No hard links: root ownership must not change the runner's package cache.
    proxy_names = [
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ftp_proxy",
        "FTP_PROXY",
        "socks_proxy",
        "SOCKS_PROXY",
        "all_proxy",
        "ALL_PROXY",
    ]
    install = [
        "sudo",
        "env",
        *[item for name in proxy_names for item in ("-u", name)],
        "XDG_CACHE_HOME=/var/cache/sbm-control-plane-smoke",
        pixi,
        "install",
        "--locked",
        "--no-hard-links",
        "--manifest-path",
        str(ROOT / "pixi.toml"),
    ]
    subprocess.run(install, check=True)
    command(f"sudo mkdir -p {ROOT}/config/generated")
    config = ROOT / "config/inventory/runtime.yaml"
    settings = fixture_settings(config)
    write_runtime(config, settings)
    command(
        f"sudo chown -R root:sbm {ROOT} && sudo chmod -R u+rwX,g+rX,g-w,o-rwx {ROOT}"
    )
    try:
        with (
            patch.multiple(
                deployment,
                _ssh=local_ssh,
                _ssh_write=local_write,
                _require_ubuntu_debian=native_os,
            ),
            patch.multiple(
                "sing_box_manager.provision", _ssh=local_ssh, _ssh_write=local_write
            ),
        ):
            deployment.deploy("fixture-local", str(ROOT), settings)
            python = ROOT / ".pixi/envs/default/bin/python"
            env = [
                "sudo",
                "-u",
                "sbm",
                "env",
                f"SBM_CONFIG={config}",
                "DJANGO_SETTINGS_MODULE=sing_box_manager.control_plane.django_settings",
                "PYTHONDONTWRITEBYTECODE=1",
                str(python),
                "-",
            ]
            subprocess.run(
                env,
                input=b"import django; django.setup(); from django.contrib.auth import get_user_model; get_user_model().objects.create_superuser('native_operator', 'operator@example.com', 'synthetic-fixture-pass-123')\n",
                cwd=ROOT,
                check=True,
            )
            asset = ROOT / "config/generated/control-plane-static/admin/css/base.css"
            result = subprocess.run(
                ["sudo", "-u", "sbm", "test", "-w", str(asset)], check=False
            )
            if result.returncode == 0:
                raise RuntimeError("The service account can write an admin asset")
            deployment.deploy("fixture-local", str(ROOT), settings)
            query = [
                "sudo",
                "-u",
                "sbm",
                "psql",
                "-X",
                "-At",
                "--dbname=sbm_native_smoke",
                "--command=SELECT count(*) FROM control_plane_user;",
            ]
            if subprocess.check_output(query, text=True).strip() != "1":
                raise RuntimeError("Repeat deployment changed operator records")
            check_authoritative_node(settings, config, env)
            settings.control_plane.enabled = False
            write_runtime(config, settings)
            deployment.deploy("fixture-local", str(ROOT), settings)
            if subprocess.check_output(query, text=True).strip() != "1":
                raise RuntimeError("Disablement changed operator records")
            state = subprocess.run(
                ["sudo", "systemctl", "is-active", "--quiet", deployment.SERVICE_NAME],
                check=False,
            )
            if state.returncode == 0:
                raise RuntimeError("Disablement left the control-plane service active")
    finally:
        for unit in (
            deployment.SYNC_PATH_NAME,
            deployment.SYNC_SERVICE_NAME,
            deployment.SERVICE_NAME,
            "sing-box.service",
        ):
            command(f"sudo systemctl stop {unit} || true")
            command(f"sudo systemctl disable {unit} || true")
    print(
        "Native deployment, authoritative sync, account switches, repeat installation, and authority exit passed."
    )


if __name__ == "__main__":
    main()
