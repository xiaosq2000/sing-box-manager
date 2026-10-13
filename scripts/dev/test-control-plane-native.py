"""Run the native foundation deployment only on a disposable GitHub runner."""

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
    from sing_box_manager.settings import ControlPlaneSettings, Settings

    pixi = shutil.which("pixi")
    if pixi is None:
        raise RuntimeError("Pixi is required")
    with patch("sing_box_manager.provision._ssh", local_ssh):
        _create_service_user_and_root("fixture-local", str(ROOT), dry_run=False)
    for name in (
        "sing_box_manager",
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
        f"XDG_CACHE_HOME={Path.home() / '.cache'}",
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
    settings = Settings(
        config_path=config,
        control_plane=ControlPlaneSettings(
            enabled=True,
            secret_key=secrets.token_hex(32),
            database_name="sbm_native_smoke",
        ),
    )
    local_write(
        "fixture-local",
        str(config),
        yaml.safe_dump(
            {
                "sing_box_version": settings.sing_box_version,
                "control_plane": settings.control_plane.model_dump(mode="json"),
            }
        ),
        mode="0640",
    )
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
            settings.control_plane.enabled = False
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
        command(f"sudo systemctl disable --now {deployment.SERVICE_NAME} || true")
    print("Native foundation deployment, repeat deployment, and disablement passed.")


if __name__ == "__main__":
    main()
