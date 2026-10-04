"""Server provisioning for bootstrap deployments on Ubuntu/Debian.

Handles one-time VPS setup so that ``sbm deploy --bootstrap`` can turn a bare
Ubuntu/Debian server into a fully functional sing-box-manager host in a single
command.  Every step is idempotent: running ``--bootstrap`` twice changes
nothing the second time.
"""

from __future__ import annotations

import secrets
import shlex
import subprocess

import yaml

from sing_box_manager import ssh
from sing_box_manager.deploy import (
    REMOTE_PIXI_PYTHON_RELATIVE_PATH,
    SERVICE_USER,
    DeploymentError,
)
from sing_box_manager.settings import (
    STATE_ROOT,
    DeploymentConfig,
    RuntimeInventory,
    Settings,
    TrafficStatsSettings,
)

# systemd creates /var/lib/<name> for this, owned by the unit's User, before the
# service starts, and leaves it alone when the unit is restarted or the deploy
# root is wiped. That is what lets the remote root stay disposable while the
# traffic stats database survives. Derived from STATE_ROOT so the directory the
# units create cannot drift from the one the database defaults into.
STATE_DIRECTORY_NAME = STATE_ROOT.name
REMOTE_INVENTORY_FILENAME = "runtime.yaml"
NGINX_SITE_NAME = "sing-box-manager"
NGINX_SITE_PATH = f"/etc/nginx/sites-available/{NGINX_SITE_NAME}.conf"
NGINX_LOG_CONF_PATH = f"/etc/nginx/conf.d/{NGINX_SITE_NAME}-log.conf"
NGINX_REDACTED_LOG_FORMAT = "sbm_redacted"

# A subscription link's path is its credential. This is the combined format
# with the token replaced, so the access log records /sub/[token].
NGINX_LOG_CONFIG = f"""# Managed by sing-box-manager deploy.
map $request_uri $sbm_loggable_request_uri {{
    "~^/sub/[^/?]+(?<sbm_after_token>.*)$" "/sub/[token]$sbm_after_token";
    default $request_uri;
}}

log_format {NGINX_REDACTED_LOG_FORMAT} '$remote_addr - $remote_user [$time_local] '
    '"$request_method $sbm_loggable_request_uri $server_protocol" '
    '$status $body_bytes_sent "$http_referer" "$http_user_agent"';
"""


def provision_remote(  # noqa: PLR0913
    hostname: str,
    remote_root: str,
    settings: Settings,
    inventory: RuntimeInventory,
    *,
    certbot_email: str | None = None,
    dry_run: bool = False,
) -> None:
    """Provision infrastructure on a fresh VPS before the first code sync.

    Creates the service user, the project directory, installs system packages,
    ships derived configuration, configures the firewall, sets up the nginx
    reverse proxy, and obtains a TLS certificate.
    """
    print("Provisioning remote host...")

    _require_ubuntu_debian(hostname)

    print("Creating service user and project directory...")
    _create_service_user_and_root(hostname, remote_root, dry_run=dry_run)

    print("Installing system packages...")
    _install_apt_packages(hostname, dry_run=dry_run)

    print("Shipping configuration to remote...")
    _ship_config(hostname, remote_root, settings, inventory, dry_run=dry_run)

    print("Configuring firewall...")
    _configure_ufw(hostname, inventory.deployment, dry_run=dry_run)

    print("Installing nginx site configuration...")
    _install_nginx_site(
        hostname,
        inventory.deployment.host,
        settings.web.port,
        dry_run=dry_run,
    )

    print("Obtaining TLS certificate...")
    _run_certbot(hostname, inventory.deployment.host, certbot_email, dry_run=dry_run)


def install_managed_units(
    hostname: str,
    remote_root: str,
    settings: Settings,
    *,
    dry_run: bool = False,
) -> None:
    """Install and enable the managed systemd units after code is in place.

    Every deploy runs this, not just ``--bootstrap``. The unit files are
    generated from this module, so a host only ever gets an edit to them by
    having them rewritten; installing them once at bootstrap left the remote
    pinned to whatever the units said on the day it was provisioned, and a
    later release that depended on a new directive (a ``StateDirectory``, an
    ``ExecStart`` path) deployed clean and then failed at runtime. Writing the
    same bytes again is a no-op, so doing it unconditionally costs nothing.
    """
    print("Installing managed systemd services...")

    _install_unit(
        hostname,
        _web_service_unit(remote_root),
        "sing-box-manager-web.service",
        dry_run=dry_run,
    )

    if settings.traffic_stats.enabled:
        _install_unit(
            hostname,
            _traffic_stats_service_unit(remote_root),
            "sing-box-manager-traffic-stats.service",
            dry_run=dry_run,
        )
        _install_unit(
            hostname,
            _traffic_stats_timer_unit(),
            "sing-box-manager-traffic-stats.timer",
            dry_run=dry_run,
        )
        _install_unit(
            hostname,
            _traffic_stats_stream_unit(remote_root),
            "sing-box-manager-traffic-stream.service",
            dry_run=dry_run,
        )

    _install_unit(
        hostname,
        _certbot_service_unit(),
        "certbot-renew.service",
        dry_run=dry_run,
    )
    _install_unit(
        hostname,
        _certbot_timer_unit(),
        "certbot-renew.timer",
        dry_run=dry_run,
    )

    units = ["sing-box-manager-web.service"]
    if settings.traffic_stats.enabled:
        units.append("sing-box-manager-traffic-stats.timer")
        units.append("sing-box-manager-traffic-stream.service")
    units.append("certbot-renew.timer")

    enable_parts = " && ".join(
        f"sudo systemctl enable --now {shlex.quote(unit)}" for unit in units
    )
    _ssh(
        hostname,
        f"sudo systemctl daemon-reload && {enable_parts}",
        "Failed to enable managed services.",
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------


def _ssh(
    hostname: str,
    command: str,
    error_message: str,
    *,
    dry_run: bool = False,
) -> None:
    if dry_run:
        print(f"  [dry-run] ssh {hostname} {command}")
        return

    argv = ssh.command(hostname, command, tty=True)

    def attempt() -> None:
        result = subprocess.run(argv, check=True, stderr=subprocess.PIPE, text=True)
        ssh.echo_stderr(result.stderr)

    try:
        ssh.with_connection_retry(attempt)
    except FileNotFoundError as exc:
        raise DeploymentError("Required command not found: ssh") from exc
    except subprocess.CalledProcessError as exc:
        ssh.echo_stderr(exc.stderr)
        raise DeploymentError(error_message) from exc


def _ssh_stdout(hostname: str, command: str, error_message: str) -> str:
    argv = ssh.command(hostname, command)

    def attempt() -> str:
        result = subprocess.run(argv, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    try:
        return ssh.with_connection_retry(attempt)
    except subprocess.CalledProcessError as exc:
        ssh.echo_stderr(exc.stderr)
        raise DeploymentError(error_message) from exc


def _ssh_write(
    hostname: str,
    remote_path: str,
    content: str,
    *,
    mode: str | None = None,
    dry_run: bool = False,
) -> None:
    """Write content to a file on the remote atomically via SSH."""
    if dry_run:
        print(f"  [dry-run] Would write {remote_path}")
        return
    tmp = f"{remote_path}.provision.tmp"
    # Only the first step is retried: it writes to a scratch path, so running
    # it twice is harmless. The chmod and the mv are left alone because a
    # transport that dies after the remote acted is indistinguishable from one
    # that dies before it, and a repeated `mv` fails on the vanished source.
    try:
        ssh.with_connection_retry(
            lambda: subprocess.run(
                ssh.command(hostname, f"sudo tee {shlex.quote(tmp)} > /dev/null"),
                input=content.encode("utf-8"),
                stderr=subprocess.PIPE,
                check=True,
            )
        )
        if mode is not None:
            subprocess.run(
                ssh.command(hostname, f"sudo chmod {mode} {shlex.quote(tmp)}"),
                check=True,
            )
        subprocess.run(
            ssh.command(
                hostname, f"sudo mv -f {shlex.quote(tmp)} {shlex.quote(remote_path)}"
            ),
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        ssh.echo_stderr(exc.stderr)
        raise DeploymentError(f"Failed to write {remote_path} on {hostname}.") from exc


def _ssh_file_exists(hostname: str, remote_path: str) -> bool:
    result = subprocess.run(
        ssh.command(hostname, f"test -f {shlex.quote(remote_path)}"),
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Provisioning steps
# ---------------------------------------------------------------------------


def _require_ubuntu_debian(hostname: str) -> None:
    os_id = _ssh_stdout(
        hostname,
        "grep ^ID= /etc/os-release | cut -d= -f2 | tr -d '\"'",
        "Failed to detect remote OS.",
    )
    if os_id not in ("ubuntu", "debian"):
        raise DeploymentError(
            f"Bootstrap requires Ubuntu or Debian, but the remote reports ID={os_id}."
        )


def _create_service_user_and_root(
    hostname: str, remote_root: str, *, dry_run: bool
) -> None:
    user_cmd = (
        f"id -u {SERVICE_USER} >/dev/null 2>&1 || "
        f"sudo useradd --system --home-dir {shlex.quote(remote_root)} "
        f"--shell /usr/sbin/nologin {SERVICE_USER}"
    )
    _ssh(hostname, user_cmd, "Failed to create service user.", dry_run=dry_run)

    # Root owns the tree and the service account only reads it; see
    # deploy._lock_down_remote_tree for why.
    dir_cmd = (
        f"sudo mkdir -p {shlex.quote(remote_root)} && "
        f"sudo chown root:{SERVICE_USER} {shlex.quote(remote_root)} && "
        f"sudo chmod 0750 {shlex.quote(remote_root)}"
    )
    _ssh(hostname, dir_cmd, "Failed to create project directory.", dry_run=dry_run)


def _install_apt_packages(hostname: str, *, dry_run: bool) -> None:
    packages = "nginx certbot python3-certbot-nginx"
    command = (
        "export DEBIAN_FRONTEND=noninteractive && "
        "sudo apt-get update -qq && "
        f"sudo apt-get install -y -qq {packages}"
    )
    _ssh(hostname, command, "Failed to install system packages.", dry_run=dry_run)


def _ship_config(
    hostname: str,
    remote_root: str,
    settings: Settings,
    inventory: RuntimeInventory,
    *,
    dry_run: bool,
) -> None:
    """Ship the merged config+inventory file (write-once)."""
    config_dir = f"{remote_root}/config"
    inventory_dir = f"{config_dir}/inventory"
    config_path = f"{inventory_dir}/{REMOTE_INVENTORY_FILENAME}"

    _ssh(
        hostname,
        (
            f"sudo mkdir -p {shlex.quote(inventory_dir)} && "
            f"sudo chown -R root:{SERVICE_USER} {shlex.quote(config_dir)}"
        ),
        "Failed to create config directories.",
        dry_run=dry_run,
    )

    if not dry_run:
        exists = _ssh_file_exists(hostname, config_path)
    else:
        exists = False

    if exists:
        print(f"  {config_path} already exists, skipping.")
        return

    merged_yaml = _derive_remote_config(settings, inventory)
    _ssh_write(hostname, config_path, merged_yaml, mode="0640", dry_run=dry_run)

    if not dry_run:
        _ssh(
            hostname,
            f"sudo chown root:{SERVICE_USER} {shlex.quote(config_path)}",
            "Failed to set ownership on config file.",
        )


def _derive_remote_config(settings: Settings, inventory: RuntimeInventory) -> str:
    """Generate the remote's unified config from local settings and inventory."""
    config: dict[str, object] = {
        "sing_box_version": settings.sing_box_version,
        "default_protocol": settings.default_protocol,
        "web": {
            "host": "127.0.0.1",
            "port": settings.web.port,
            # A deploy creates the server's secrets when the inventory has none.
            # Later deploys keep whatever the server holds; see remote_config.
            "session_secret": settings.web.session_secret or secrets.token_hex(32),
            "subscription_secret": (
                settings.web.subscription_secret or secrets.token_hex(32)
            ),
            "allowed_hosts": [
                "localhost",
                "127.0.0.1",
                inventory.deployment.host,
            ],
        },
        "traffic_stats": _remote_traffic_stats(settings.traffic_stats),
        "client_upgrade": {
            "policy": settings.client_upgrade.policy,
            "message": settings.client_upgrade.message,
        },
    }
    inventory_data = inventory.model_dump(
        mode="json",
        include={
            "deployment_data",
            "web_portal_data",
            "trojan_data",
            "hysteria2_data",
            "naive_data",
            "vps_info",
        },
        exclude_none=True,
    )
    config.update(inventory_data)
    return yaml.dump(config, default_flow_style=False, sort_keys=False)


def _remote_traffic_stats(traffic_stats: TrafficStatsSettings) -> dict[str, object]:
    """Every traffic stats field, serialized for the remote's YAML config.

    Read off the dataclass rather than a hand-kept key list. The inventory has
    no ``traffic_stats`` section to merge on top, so a field this block forgets
    is not merely missing from the file: it silently takes its default on the
    remote, with nothing on either end to say the operator's setting was
    dropped.
    """
    block: dict[str, object] = traffic_stats.model_dump(mode="json")
    block["database_path"] = str(traffic_stats.database_path)
    return block


# The firewall must admit the port this deploy is connected through before it is
# enabled, or the next connection is refused. sshd can listen on any port, or
# several, so ask it, and add the server port of the current session in case
# `sshd -T` fails. Port 22 is only the fallback when both come up empty.
SSH_PORTS_ALLOW_COMMAND = (
    "{ ssh_ports=$({ sudo sshd -T 2>/dev/null | awk '$1 == \"port\" { print $2 }'; "
    "printf '%s\\n' \"${SSH_CONNECTION##* }\"; } | grep -E '^[0-9]+$' | sort -u); "
    "for port in ${ssh_ports:-22}; do "
    'sudo ufw allow "$port/tcp" comment SSH || exit 1; done; }'
)


def _configure_ufw(
    hostname: str,
    deployment: DeploymentConfig,
    *,
    dry_run: bool,
) -> None:
    """Open protocol ports and optionally set up NAT redirect for port hopping.

    All ufw rules, NAT redirects, enable, and reload are batched into a single
    SSH call to avoid connection throttling on hosts with rate-limited SSH.
    """
    rules = [
        ("80/tcp", "HTTP"),
        ("443/tcp", "HTTPS"),
        (f"{deployment.trojan_port}/tcp", "Trojan"),
        (f"{deployment.hysteria2_port}/udp", "Hysteria2"),
        (f"{deployment.naive_port}/tcp", "Naive"),
    ]

    if deployment.hysteria2_port_range:
        rules.append(
            (f"{deployment.hysteria2_port_range}/udp", "Hysteria2 port hopping")
        )

    parts = [SSH_PORTS_ALLOW_COMMAND]
    parts.extend(
        f"sudo ufw allow {spec} comment {shlex.quote(comment)}"
        for spec, comment in rules
    )

    if deployment.hysteria2_port_range:
        parts.extend(
            _ufw_nat_redirect_commands(
                deployment.hysteria2_port_range,
                deployment.hysteria2_port,
            )
        )

    parts.append("sudo ufw --force enable")
    parts.append("sudo ufw reload")

    _ssh(
        hostname,
        " && ".join(parts),
        "Failed to configure ufw.",
        dry_run=dry_run,
    )


def _ufw_nat_redirect_commands(port_range: str, target_port: int) -> list[str]:
    """Shell fragments that prepend a *nat PREROUTING redirect to before.rules."""
    nat_block = (
        f"*nat\\n"
        f":PREROUTING ACCEPT [0:0]\\n"
        f"-A PREROUTING -p udp --dport {port_range} "
        f"-j REDIRECT --to-ports {target_port}\\n"
        f"COMMIT\\n"
    )

    parts: list[str] = []
    for rules_file in ("/etc/ufw/before.rules", "/etc/ufw/before6.rules"):
        check = (
            f"grep -q 'PREROUTING.*REDIRECT.*--to-ports.*{target_port}' "
            f"{shlex.quote(rules_file)} 2>/dev/null"
        )
        insert = (
            f"{{ printf '{nat_block}\\n'; cat {shlex.quote(rules_file)}; }} | "
            f"sudo tee {shlex.quote(rules_file + '.new')} > /dev/null && "
            f"sudo mv -f {shlex.quote(rules_file + '.new')} "
            f"{shlex.quote(rules_file)}"
        )
        parts.append(f"{{ {check} || {{ {insert}; }}; }}")
    return parts


def _install_nginx_site(
    hostname: str,
    server_name: str,
    port: int,
    *,
    dry_run: bool,
) -> None:
    """Write the nginx reverse-proxy config and enable the site."""
    config = (
        "server {\n"
        "    listen 80;\n"
        "    listen [::]:80;\n"
        f"    server_name {server_name};\n"
        "\n"
        "    location / {\n"
        f"        proxy_pass http://127.0.0.1:{port};\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "    }\n"
        "}\n"
    )

    available = f"/etc/nginx/sites-available/{NGINX_SITE_NAME}.conf"
    enabled = f"/etc/nginx/sites-enabled/{NGINX_SITE_NAME}.conf"

    _ssh_write(hostname, available, config, dry_run=dry_run)
    _ssh(
        hostname,
        (
            f"sudo ln -sf {shlex.quote(available)} {shlex.quote(enabled)} && "
            "sudo nginx -t && "
            "sudo systemctl reload nginx"
        ),
        "Failed to enable and reload nginx.",
        dry_run=dry_run,
    )


def _run_certbot(
    hostname: str,
    domain: str,
    certbot_email: str | None,
    *,
    dry_run: bool,
) -> None:
    """Obtain a TLS certificate via certbot --nginx."""
    email_flag = (
        f"--email {shlex.quote(certbot_email)}"
        if certbot_email
        else "--register-unsafely-without-email"
    )
    command = (
        f"sudo certbot --nginx -d {shlex.quote(domain)} "
        f"--non-interactive --agree-tos {email_flag}"
    )
    _ssh(
        hostname,
        command,
        f"Failed to obtain TLS certificate for {domain}.",
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Systemd unit generators
# ---------------------------------------------------------------------------


def _install_unit(
    hostname: str,
    content: str,
    unit_name: str,
    *,
    dry_run: bool,
) -> None:
    path = f"/etc/systemd/system/{unit_name}"
    _ssh_write(hostname, path, content, dry_run=dry_run)


def _remote_config_path() -> str:
    return f"config/inventory/{REMOTE_INVENTORY_FILENAME}"


def _sbm_unit(
    *,
    description: str,
    after: str,
    remote_root: str,
    subcommand: str,
    restart: str | None,
) -> str:
    """One managed unit that runs an ``sbm`` subcommand as the service user.

    Every managed unit shares the same identity, working directory and state
    directory, and every one of them has to keep sharing them: the state
    directory is what holds the usage database, so a unit that spells it
    differently writes its rows somewhere the others cannot read.

    ``restart`` decides the whole shape rather than being one knob among
    several, because the three properties that vary move together. A unit with
    no restart policy is a ``oneshot`` driven by its own timer, which re-runs it
    and is the thing that gets enabled, so the unit needs no ``[Install]``
    section either. A unit with one is long-running, and needs both.
    """
    unit = (
        "[Unit]\n"
        f"Description={description}\n"
        f"After={after}\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        f"Type={'oneshot' if restart is None else 'simple'}\n"
        f"User={SERVICE_USER}\n"
        f"Group={SERVICE_USER}\n"
        f"WorkingDirectory={remote_root}\n"
        f"StateDirectory={STATE_DIRECTORY_NAME}\n"
        "StateDirectoryMode=0750\n"
        # The environment's interpreter rather than `pixi run`: the service
        # account cannot write the tree, and the interpreter needs nothing
        # written there to start.
        f"ExecStart={remote_root}/{REMOTE_PIXI_PYTHON_RELATIVE_PATH} "
        f"-m sing_box_manager --config {_remote_config_path()} {subcommand}\n"
    )
    if restart is None:
        return unit + "Environment=PYTHONUNBUFFERED=1\n"

    return (
        unit
        + f"Restart={restart}\n"
        + "RestartSec=5s\n"
        + "Environment=PYTHONUNBUFFERED=1\n"
        + "\n[Install]\nWantedBy=multi-user.target\n"
    )


def _web_service_unit(remote_root: str) -> str:
    return _sbm_unit(
        description="sing-box-manager web portal",
        after="network-online.target",
        remote_root=remote_root,
        subcommand="serve",
        restart="on-failure",
    )


def _traffic_stats_service_unit(remote_root: str) -> str:
    return _sbm_unit(
        description="sing-box-manager traffic stats collector",
        after=("network-online.target sing-box.service"),
        remote_root=remote_root,
        subcommand="stats-collect",
        restart=None,
    )


def _traffic_stats_stream_unit(remote_root: str) -> str:
    """The per-connection stream daemon.

    Long-running rather than timer-driven because sing-box drops connection
    events whenever nobody is subscribed, so every second this is down is
    traffic the domain breakdown never sees. Restart=always for the same reason;
    the five-minute poller remains the authority on quota either way.
    """
    return _sbm_unit(
        description="sing-box-manager connection stats stream",
        after=("network-online.target sing-box.service"),
        remote_root=remote_root,
        subcommand="stats-stream",
        restart="always",
    )


def _traffic_stats_timer_unit() -> str:
    return (
        "[Unit]\n"
        "Description=Run sing-box-manager traffic stats collection "
        "every 5 minutes\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=*:0/5\n"
        "RandomizedDelaySec=30s\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def _certbot_service_unit() -> str:
    return (
        "[Unit]\n"
        "Description=Renew Let's Encrypt certificates\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/bin/env certbot renew --quiet "
        '--deploy-hook "systemctl reload nginx && '
        "systemctl try-restart sing-box.service"
        '"\n'
    )


def _certbot_timer_unit() -> str:
    return (
        "[Unit]\n"
        "Description=Run Certbot renewal twice daily\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=*-*-* 00,12:00:00\n"
        "RandomizedDelaySec=43200\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def nginx_log_redaction_script(site_path: str, conf_path: str) -> str:
    """Return the root shell script that switches the site to the redacted log.

    certbot rewrote the site file after bootstrap wrote it, so the script edits
    it in place instead of replacing it: it adds one `access_log` line after
    each `server_name`, once. The new log format arrives staged beside its
    final path. If `nginx -t` rejects the result, both files go back to what
    they were, so a bad edit never reaches the next nginx start.
    """
    site = shlex.quote(site_path)
    conf = shlex.quote(conf_path)
    staged = shlex.quote(f"{conf_path}.new")
    access_log = f"access_log /var/log/nginx/access.log {NGINX_REDACTED_LOG_FORMAT};"
    return (
        f"[ -f {site} ] || {{ echo 'missing nginx site {site_path}' >&2; exit 1; }}\n"
        "work=$(mktemp -d) || exit 1\n"
        f'cp -p {site} "$work/site" || exit 1\n'
        f'if [ -e {conf} ]; then cp -p {conf} "$work/conf" || exit 1; fi\n'
        f"mv {staged} {conf} || exit 1\n"
        f"grep -q {shlex.quote(access_log)} {site} || "
        f"sed -i {shlex.quote('/^[[:space:]]*server_name /a\\    ' + access_log)} "
        f"{site}\n"
        "if nginx -t; then\n"
        "    systemctl reload nginx; status=$?\n"
        "else\n"
        "    status=1\n"
        f'    cp -p "$work/site" {site}\n'
        f'    if [ -e "$work/conf" ]; then cp -p "$work/conf" {conf}; '
        f"else rm -f {conf}; fi\n"
        "fi\n"
        'rm -rf "$work"\n'
        "exit $status\n"
    )


def install_nginx_log_redaction(hostname: str, *, dry_run: bool = False) -> None:
    """Keep subscription tokens out of nginx's access log. Runs every deploy."""
    print("Redacting subscription tokens from the nginx access log...")
    _ssh_write(
        hostname, f"{NGINX_LOG_CONF_PATH}.new", NGINX_LOG_CONFIG, dry_run=dry_run
    )
    script = nginx_log_redaction_script(NGINX_SITE_PATH, NGINX_LOG_CONF_PATH)
    _ssh(
        hostname,
        f"sudo sh -c {shlex.quote(script)}",
        "Failed to switch nginx to the redacted access log; "
        "the site and its log format were restored.",
        dry_run=dry_run,
    )
