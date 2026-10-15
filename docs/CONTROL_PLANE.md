# Control-plane foundation

Checked on 2026-10-15.

The opt-in Django 5.2 service runs beside the FastAPI portal. PostgreSQL stores
Django operator accounts and sessions. It does not own proxy users, credentials,
subscriptions, or traffic. The encrypted inventory remains authoritative for them.

A separate [fixture-only importer](CONTROL_PLANE_IMPORT.md) tests access copying
in disposable databases. Its target marker defaults to disabled. The separate account-copy app adds a disabled
admission table, no admission rows, and no admin pages. Neither path has public routes.
Release and deploy never run or authorize an import.

No node agent is installed. Current clients keep their existing subscription URLs.

The next delivery target is [basic live migration](REFACTOR_SPEC.md#m4b-control-plane-devices-and-subscriptions):
Django-managed proxy accounts, PostgreSQL ownership, and working subscriptions on
the single VPS. A [checked real-data copy](CONTROL_PLANE_CAPTURE.md) comes first.
The [account-copy path](CONTROL_PLANE_ACCOUNT_COPY.md) is implemented, independently
source-reviewed, and tested with synthetic sources. It is not deployed. Serving and
proxy-account management remain unimplemented.
Traffic features, devices, and the general node agent are deferred from this delivery.
The selected cutover policy preserves account credentials but issues fresh subscription
links. Each installation must update its saved URL. Old links remain unchanged now
and must be rejected after cutover. Goal or policy approval does not authorize live operations. The foundation below remains
operator-only until a separately approved cutover.

## Layout

| Component      | Location or behavior                                               |
| -------------- | ------------------------------------------------------------------ |
| Django         | `sing_box_manager/control_plane/`, served by Gunicorn              |
| Admin          | `http://127.0.0.1:47071/admin/`, through an SSH tunnel             |
| Readiness      | `/health/`; checks database access, migrations, and an admin asset |
| Service        | `sing-box-manager-control-plane.service`, running as `sbm`         |
| Database       | OS-managed PostgreSQL 14 or later; database `sbm_control_plane`    |
| Database login | Local Unix socket, role `sbm`, peer authentication                 |
| Database files | The OS PostgreSQL data directory, outside the deploy root          |
| Static files   | Root-owned `config/generated/control-plane-static/`                |

Peer authentication uses the operating-system account to identify a database
client. Run management commands as `sbm`. No database password is needed.
The database role has no superuser, role-creation, or database-creation rights.
Django has no public nginx route. Do not open ports 47071 or 5432 in the firewall.

The service uses HTTP on loopback. SSH encrypts remote access. Do not publish this
HTTP service or reuse its cookie settings for a public site.

Django uses `Referrer-Policy: same-origin`. The `no-referrer` policy can make browsers
send `Origin: null` with form submissions. Django rejects that origin. Keep CSRF
checks enabled. Do not add `null` to trusted origins.

## Enable and deploy

Read [secret handling](SECRET_HANDLING.md) before editing operator configuration.
Add this block to the selected inventory:

```yaml
control_plane:
  enabled: true
  port: 47071
  database_name: sbm_control_plane
  database_socket_dir: /var/run/postgresql
  database_port: 5432
```

`secret_key` is optional for release and deploy. Deploy generates it when absent
and preserves the server's key on later deployments, including disable/re-enable.
Local Django commands require a configured key of at least 50 characters.
Keep that key in protected infrastructure configuration, not public source.

Build and deploy with the same inventory. Replace `vpn-host` with the target alias:

```sh
pixi run sbm --config /path/to/runtime.sops.yaml release
pixi run sbm --config /path/to/runtime.sops.yaml deploy --hostname vpn-host
```

Use `--bootstrap` for a fresh host. Both fresh and ordinary deployments prepare
PostgreSQL when enabled. Existing package installations are reused.
The host must provide PostgreSQL 14 or later, such as Debian 12+ or Ubuntu 22.04+.
Check free memory and disk space before enabling both services.
Use a deploy root outside `/home` and `/root`, because the service enables `ProtectHome`.
Managed `config_root` must be relative and contain no parent traversal.

Deployment creates the restricted role and database when missing. It refuses a
privileged existing role, another database owner, or a non-loopback database listener.
It does not rewrite PostgreSQL authentication rules or replace an existing database.
A custom cluster must permit peer authentication for `sbm` on the selected socket.

Deployment checks Django, applies migrations, collects static files, starts the
service, and checks readiness. Concurrent migrations use a database advisory lock.
A failed readiness check prevents publication of successful deployment metadata.
Deployment is not an atomic rollback of the existing portal or proxy services.
Inspect a failed step before retrying.

`--skip-code` leaves the control-plane service, database, and configuration unchanged.
A disabled control plane does not install PostgreSQL or apply migrations.

## Create an operator and open the admin

Create the first account from an interactive terminal. The command prompts for the
password. Do not put passwords in command arguments or environment variables.

```sh
ssh -t vpn-host 'cd /opt/sing-box-manager && sudo -u sbm env PYTHONDONTWRITEBYTECODE=1 .pixi/envs/default/bin/python -m sing_box_manager --config config/inventory/runtime.yaml control-plane --action create-admin'
```

Open a tunnel from your workstation:

```sh
ssh -N -L 127.0.0.1:47071:127.0.0.1:47071 vpn-host
```

Open `http://127.0.0.1:47071/admin/`. Use a different local port if 47071 is occupied.
After a CSRF 403, open a fresh login page at the exact tunnel URL.
Keep the hostname and port unchanged during login.

Admin changes affect Django operators only. They do not grant or revoke proxy access.
A Django superuser has application permissions, not PostgreSQL superuser rights.
The custom Django user model is the starting point for the later ownership cutover.

For a manual readiness check on the server:

```sh
cd /opt/sing-box-manager
sudo -u sbm .pixi/envs/default/bin/python -m sing_box_manager --config config/inventory/runtime.yaml control-plane --action health
sudo systemctl status sing-box-manager-control-plane.service
```

Other explicit actions are `check`, `migrate`, `collect-static`, and `serve`.
Collect static files as root during deployment. The service account must not write
those files or the deployed source tree.

## Back up and test recovery

These commands use the default database and local PostgreSQL cluster.
Keep dumps private and outside the checkout. Use a new filename for each backup.
Keep an encrypted copy on another host. Do not commit dumps or database files.

On the server:

```sh
sudo install -d -m 0700 /var/backups/sing-box-manager
BACKUP=/var/backups/sing-box-manager/control-plane-$(date +%Y%m%dT%H%M%S).dump
sudo sh -c 'umask 077; sudo -u postgres pg_dump --format=custom --dbname=sbm_control_plane > "$1"' sh "$BACKUP"
```

Restore into a separate database, not over the active one:

```sh
sudo -u postgres createdb --owner=sbm sbm_control_plane_restore
sudo sh -c 'sudo -u postgres pg_restore --exit-on-error --no-owner --role=sbm --dbname=sbm_control_plane_restore < "$1"' sh "$BACKUP"
sudo -u sbm psql --dbname=sbm_control_plane_restore --command='SELECT count(*) FROM control_plane_user;'
```

For recovery, select the restored name in `control_plane.database_name`, then build
and deploy. Check readiness and admin login. Keep the original database until the
operator authorizes its removal. A database backup does not contain the Django
signing key; keep a protected backup of infrastructure configuration too.

## Disable without removing data

Set `control_plane.enabled: false` in the inventory and deploy normally.
Deployment disables only the managed Django service. It retains PostgreSQL, the
database, operator accounts, and signing key. It does not reverse migrations.
Disable this feature before considering a code rollback. Older revisions do not
understand the new settings or service. Do not assume an older deploy removes it.

## Verification

```sh
pixi run -e control-plane-test test tests/test_control_plane.py tests/test_control_plane_integration.py tests/test_deploy.py tests/test_provision.py tests/test_remote_config.py
```

The test environment supplies PostgreSQL 17. Tests start an isolated cluster with
no TCP listener and a private socket directory. Its temporary authentication rules
are fixtures, not production configuration. Tests cover migrations, admin access,
CSRF and host checks, static files, repeat provisioning, database privileges,
backup restoration, and disabled-state retention. They do not contact a live VPS.
CI requires these real-database checks. It also runs a native systemd deployment
smoke test on a disposable GitHub runner. That test checks production peer
authentication, read-only assets, repeat deployment, and disablement.
See [Development](DEVELOPMENT.md).
