"""Deploy the Django portal and its private administration service."""

import re
import shlex
from pathlib import Path

from sing_box_manager.control_plane.database import provision_sql
from sing_box_manager.deploy import (
    REMOTE_PIXI_PYTHON_RELATIVE_PATH,
    SERVICE_USER,
    DeploymentError,
)
from sing_box_manager.provision import (
    NGINX_SITE_PATH,
    _install_unit,
    _require_ubuntu_debian,
    _sbm_unit,
    _ssh,
    _ssh_write,
)
from sing_box_manager.settings import Settings

WRAPPER_PATH = "/usr/local/bin/sbm"
SERVICE_NAME = "sing-box-manager-control-plane.service"
SYNC_SERVICE_NAME = "sing-box-manager-sync.service"
SYNC_PATH_NAME = "sing-box-manager-sync.path"
NGINX_BEGIN_MARKER = "    # BEGIN sbm-control-plane"
NGINX_END_MARKER = "    # END sbm-control-plane"


def control_plane_nginx_block(port: int) -> str:
    return (
        f"{NGINX_BEGIN_MARKER}\n"
        "    location = /admin { return 404; }\n"
        "    location ^~ /admin/ { return 404; }\n"
        "    location = /health/ { return 404; }\n"
        "    location ~ ^/(api/(sub|token)|auth)$ {\n"
        "        limit_req zone=sbm_auth burst=5 nodelay;\n"
        "        limit_req_status 429;\n"
        f"        proxy_pass http://127.0.0.1:{port};\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "    }\n"
        f"{NGINX_END_MARKER}"
    )


def update_nginx_site_text(content: str, port: int | None) -> str:
    """Route the whole portal to Django, or fail closed when it is disabled."""
    cleaned = re.sub(
        r"[ \t]*# BEGIN sbm-control-plane\n.*?# END sbm-control-plane\n\n?",
        "",
        content,
        flags=re.DOTALL,
    )
    pattern = r"    location / \{[^{}]*\}"
    if len(re.findall(pattern, cleaned)) != 1:
        raise ValueError("nginx site must have one 'location / {' block")
    if port is None:
        block = "    location / { return 503; }"
    else:
        block = (
            control_plane_nginx_block(port)
            + "\n\n"
            + (
                "    location / {\n"
                f"        proxy_pass http://127.0.0.1:{port};\n"
                "        proxy_http_version 1.1;\n"
                "        proxy_set_header Host $host;\n"
                "        proxy_set_header X-Real-IP $remote_addr;\n"
                "        proxy_set_header X-Forwarded-For $remote_addr;\n"
                "        proxy_set_header X-Forwarded-Proto $scheme;\n"
                "    }"
            )
        )
    return re.sub(pattern, lambda _: block, cleaned)


def nginx_route_script(
    remote_root: str, site_path: str = NGINX_SITE_PATH, port: int | None = None
) -> str:
    python = shlex.quote(str(Path(remote_root) / REMOTE_PIXI_PYTHON_RELATIVE_PATH))
    site = shlex.quote(site_path)
    port_arg = "None" if port is None else str(int(port))
    py_code = (
        "import sys; from pathlib import Path; "
        "from sing_box_manager.control_plane.deployment import update_nginx_site_text; "
        "p = Path(sys.argv[1]); "
        f"p.write_text(update_nginx_site_text(p.read_text(), {port_arg}))"
    )
    return (
        f"[ -f {site} ] || {{ echo 'missing nginx site {site_path}' >&2; exit 1; }}\n"
        "work=$(mktemp -d) || exit 1\n"
        f'cp -p {site} "$work/site" || exit 1\n'
        f"cd {shlex.quote(remote_root)} && "
        f"PYTHONDONTWRITEBYTECODE=1 {python} -c {shlex.quote(py_code)} {site}\n"
        "if [ $? -eq 0 ] && nginx -t && systemctl reload nginx; then\n"
        "    status=0\n"
        "else\n"
        "    status=1\n"
        f'    cp -p "$work/site" {site} && nginx -t && systemctl reload nginx\n'
        "fi\n"
        'rm -rf "$work"\n'
        "exit $status\n"
    )


def service_unit(remote_root: str) -> str:
    generated_dir = shlex.quote(str(Path(remote_root) / "config/generated"))
    return _sbm_unit(
        description="Subscriptions and private service administration",
        after="network-online.target postgresql.service",
        remote_root=remote_root,
        subcommand="control-plane --action serve",
        restart="on-failure",
    ) + (
        "\n[Unit]\n"
        "Wants=postgresql.service\n"
        "\n[Service]\n"
        "UMask=0077\n"
        "NoNewPrivileges=true\n"
        "ProtectSystem=strict\n"
        f"ReadWritePaths={generated_dir}\n"
        "ProtectHome=true\n"
        "PrivateTmp=true\n"
        "ProtectKernelTunables=true\n"
        "ProtectKernelModules=true\n"
        "ProtectControlGroups=true\n"
        "RestrictSUIDSGID=true\n"
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
        "Environment=PYTHONDONTWRITEBYTECODE=1\n"
    )


def sync_service_unit(remote_root: str) -> str:
    python = Path(remote_root) / REMOTE_PIXI_PYTHON_RELATIVE_PATH
    config = Path(remote_root) / "config/inventory/runtime.yaml"
    return (
        "[Unit]\n"
        "Description=Sync sing-box node config from PostgreSQL\n"
        "After=postgresql.service\n"
        "\n[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={remote_root}\n"
        "Environment=PYTHONDONTWRITEBYTECODE=1\n"
        f"ExecStart={python} -m sing_box_manager --config {config} "
        "control-plane --action sync-node\n"
    )


def sync_path_unit(remote_root: str) -> str:
    trigger = Path(remote_root) / "config/generated/node-sync.trigger"
    return (
        "[Unit]\n"
        "Description=Watch for control-plane node sync requests\n"
        "\n[Path]\n"
        f"PathChanged={trigger}\n"
        f"Unit={SYNC_SERVICE_NAME}\n"
        "\n[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def wrapper_script(remote_root: str) -> str:
    python = shlex.quote(str(Path(remote_root) / REMOTE_PIXI_PYTHON_RELATIVE_PATH))
    config = shlex.quote(str(Path(remote_root) / "config/inventory/runtime.yaml"))
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f"export SBM_CONFIG=${{SBM_CONFIG-{config}}}\n"
        "export PYTHONDONTWRITEBYTECODE=1\n"
        f"cd {shlex.quote(remote_root)}\n"
        f'exec {python} -m sing_box_manager "$@"\n'
    )


def install_wrapper(hostname: str, remote_root: str, *, dry_run: bool = False) -> None:
    _ssh_write(
        hostname,
        WRAPPER_PATH,
        wrapper_script(remote_root),
        mode="0755",
        dry_run=dry_run,
    )


def management_command(
    remote_root: str, action: str, *, service_user: bool = True
) -> str:
    python = str(Path(remote_root) / REMOTE_PIXI_PYTHON_RELATIVE_PATH)
    config = str(Path(remote_root) / "config/inventory/runtime.yaml")
    argv = [
        python,
        "-m",
        "sing_box_manager",
        "--config",
        config,
        "control-plane",
        "--action",
        action,
    ]
    if service_user:
        argv = ["sudo", "-u", SERVICE_USER, "env", "PYTHONDONTWRITEBYTECODE=1", *argv]
    else:
        argv = ["sudo", "env", "PYTHONDONTWRITEBYTECODE=1", *argv]
    return f"cd {shlex.quote(remote_root)} && {shlex.join(argv)}"


def validate_deployment(remote_root: str, settings: Settings) -> Path:
    if settings.config_root.is_absolute() or ".." in settings.config_root.parts:
        raise DeploymentError(
            "Managed control-plane config_root must be relative without parent traversal"
        )
    if any(
        Path(remote_root).is_relative_to(path)
        for path in ("/home", "/root", "/run/user")
    ):
        raise DeploymentError(
            "The control-plane deploy root must be outside protected home directories"
        )
    return Path(remote_root) / settings.generated_root / "control-plane-static"


def pause_node_sync(hostname: str, *, dry_run: bool = False) -> None:
    """Disable the watcher and let an existing sync finish before node installation."""
    wait = (
        f"while state=$(systemctl show -p ActiveState --value {SYNC_SERVICE_NAME}); do "
        'case "$state" in activating|active|deactivating) sleep 1;; *) exit 0;; esac; '
        "done; exit 1"
    )
    _ssh(
        hostname,
        f"if sudo systemctl cat {SYNC_PATH_NAME} >/dev/null 2>&1; then "
        f"sudo systemctl disable --now {SYNC_PATH_NAME} || exit 1; fi; "
        f"if sudo systemctl cat {SYNC_SERVICE_NAME} >/dev/null 2>&1; then "
        f"sudo timeout 60 sh -c {shlex.quote(wait)}; fi",
        "Failed to pause node sync. Node installation stopped.",
        dry_run=dry_run,
    )


def deploy(
    hostname: str, remote_root: str, settings: Settings, *, dry_run: bool = False
) -> None:
    control = settings.control_plane
    if not control.authoritative:
        pause_node_sync(hostname, dry_run=dry_run)
    if not control.enabled:
        _ssh(
            hostname,
            f"sudo sh -c {shlex.quote(nginx_route_script(remote_root))}",
            "Failed to disable public portal routes. Check nginx on the server.",
            dry_run=dry_run,
        )
        _ssh(
            hostname,
            f"if sudo systemctl cat {SERVICE_NAME} >/dev/null 2>&1; then "
            f"sudo systemctl disable --now {SERVICE_NAME}; fi",
            "Failed to disable the control-plane service. The database is retained.",
            dry_run=dry_run,
        )
        _disable_legacy_portal(hostname, dry_run=dry_run)
        return

    static_root = validate_deployment(remote_root, settings)
    _require_ubuntu_debian(hostname)
    install_wrapper(hostname, remote_root, dry_run=dry_run)
    _ssh(
        hostname,
        "if [ \"$(dpkg-query -W -f='${Status}\\n' postgresql postgresql-client "
        "2>/dev/null | grep -c '^install ok installed$')\" != 2 ]; then "
        "export DEBIAN_FRONTEND=noninteractive; sudo apt-get update -qq && "
        "sudo apt-get install -y -qq postgresql postgresql-client || exit 1; fi && "
        "sudo systemctl start postgresql.service",
        "Failed to provision PostgreSQL for the control plane.",
        dry_run=dry_run,
    )
    sql_path = str(Path(remote_root) / "config/generated/control-plane-provision.sql")
    _ssh_write(
        hostname,
        sql_path,
        provision_sql(control.database_name),
        mode="0600",
        dry_run=dry_run,
    )
    _ssh(
        hostname,
        shlex.join(
            [
                "sudo",
                "-u",
                "postgres",
                "psql",
                "-X",
                "--set",
                "ON_ERROR_STOP=1",
                "--host",
                str(control.database_socket_dir),
                "--port",
                str(control.database_port),
                "--dbname",
                "postgres",
            ]
        )
        + f" < {shlex.quote(sql_path)}",
        "Failed to prepare the dedicated control-plane database. Existing data was not replaced.",
        dry_run=dry_run,
    )
    for action in ("check", "migrate"):
        _ssh(
            hostname,
            management_command(remote_root, action),
            f"Control-plane {action} failed.",
            dry_run=dry_run,
        )
    _ssh(
        hostname,
        management_command(remote_root, "collect-static", service_user=False),
        "Failed to collect control-plane static files.",
        dry_run=dry_run,
    )
    # Service accounts read these assets but never write executable browser code.
    static = shlex.quote(str(static_root))
    _ssh(
        hostname,
        f"sudo chown -R root:{SERVICE_USER} {static} && "
        f"sudo chmod -R u+rwX,g+rX,g-w,o-rwx {static}",
        "Failed to protect control-plane static files.",
        dry_run=dry_run,
    )
    _install_unit(hostname, service_unit(remote_root), SERVICE_NAME, dry_run=dry_run)
    _ssh(
        hostname,
        f"sudo systemctl daemon-reload && sudo systemctl enable {SERVICE_NAME} && "
        f"sudo systemctl restart {SERVICE_NAME}",
        "Failed to start the private control-plane service.",
        dry_run=dry_run,
    )
    _ssh(
        hostname,
        management_command(remote_root, "health")
        + f" && sudo systemctl is-active --quiet {SERVICE_NAME}",
        "Control-plane readiness check failed. Deployment metadata was not advanced.",
        dry_run=dry_run,
    )
    if control.authoritative:
        _install_unit(
            hostname,
            sync_service_unit(remote_root),
            SYNC_SERVICE_NAME,
            dry_run=dry_run,
        )
        _install_unit(
            hostname,
            sync_path_unit(remote_root),
            SYNC_PATH_NAME,
            dry_run=dry_run,
        )
        trigger = shlex.quote(
            str(Path(remote_root) / "config/generated/node-sync.trigger")
        )
        _ssh(
            hostname,
            f"sudo install -o {SERVICE_USER} -g {SERVICE_USER} -m 0600 /dev/null {trigger} && "
            f"sudo systemctl daemon-reload && sudo systemctl enable --now {SYNC_PATH_NAME}",
            "Failed to enable the control-plane node sync watcher.",
            dry_run=dry_run,
        )
        _ssh(
            hostname,
            management_command(remote_root, "sync-node", service_user=False),
            "Failed to sync sing-box node config from PostgreSQL.",
            dry_run=dry_run,
        )
    _ssh(
        hostname,
        f"sudo sh -c {shlex.quote(nginx_route_script(remote_root, NGINX_SITE_PATH, control.port))}",
        "Failed to switch nginx portal routes. Check nginx on the server.",
        dry_run=dry_run,
    )
    # Disable the retired service only after Django is healthy and Nginx switched.
    _disable_legacy_portal(hostname, dry_run=dry_run)


def _disable_legacy_portal(hostname: str, *, dry_run: bool) -> None:
    _ssh(
        hostname,
        "if sudo systemctl cat sing-box-manager-web.service >/dev/null 2>&1; then "
        "sudo systemctl disable --now sing-box-manager-web.service; fi",
        "Failed to disable the retired web service.",
        dry_run=dry_run,
    )
