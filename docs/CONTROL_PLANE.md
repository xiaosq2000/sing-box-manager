# Control-plane guide

Checked on 2026-10-18.

The opt-in Django 5.2 service (`sing_box_manager/control_plane/`) manages operator
accounts, proxy user accounts (`Account`), per-protocol credentials (`Credential`),
subscriptions (`/sub/<token>`), signed client downloads (`/sub/<token>/files/<path>`),
and single-node `sing-box` configuration sync.

## Layout

| Component      | Location or behavior                                                         |
| -------------- | ---------------------------------------------------------------------------- |
| Django         | `sing_box_manager/control_plane/`, served by Gunicorn                        |
| Admin          | `http://127.0.0.1:47071/admin/`, through an SSH tunnel                       |
| Subscriptions  | `/sub/<token>`, `/sub/<token>/files/<path>`, `/install.sh`, `/install.ps1`   |
| Readiness      | `/health/`; checks database access, migrations, and an admin static asset    |
| Service        | `sing-box-manager-control-plane.service`, running as `sbm`                   |
| Database       | OS-managed PostgreSQL 14 or later; database `sbm_control_plane`              |
| Database login | Local Unix socket, role `sbm`, peer authentication                           |
| Static files   | Root-owned `config/generated/control-plane-static/`                          |
| Models         | `User` (operators), `Account` (proxy users), `Credential` (protocol secrets) |

Peer authentication uses the operating-system account (`sbm`) to identify a database
client. No database password is needed. The database role has no superuser,
role-creation, or database-creation rights. Do not open ports 47071 or 5432 in the
firewall.

Django uses `Referrer-Policy: same-origin` on admin pages so browser form submissions
send a valid `Origin` header, while `/sub/` responses send `Referrer-Policy: no-referrer`.

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

Build and deploy with the same inventory. Replace `vpn-host` with the target alias:

```sh
pixi run sbm --config /path/to/runtime.sops.yaml release
pixi run sbm --config /path/to/runtime.sops.yaml deploy --hostname vpn-host
```

## Account import, admin management, and node sync

To idempotently import accounts, protocol credentials, and existing SQLite reset
counters from the runtime configuration into PostgreSQL (and verify rendered
server/client config parity in one transaction):

```sh
sudo -u sbm .pixi/envs/default/bin/python -m sing_box_manager \
  --config config/inventory/runtime.yaml control-plane --action import-inventory
```

In Django Admin (`/admin/`), the **Accounts** view manages `Account` rows and inline
`Credential` rows (`trojan`, `hysteria2`, `naive`):

- Enter a plaintext password in **New portal password** to hash it with Argon2id, or
  leave it blank to preserve the existing hash.
- Use the **Reset subscription links for selected accounts** action to advance
  `subscription_counter` and invalidate old `/sub/<token>` links.

To render the server `sing-box` config from PostgreSQL accounts, validate it with
`sing-box check`, and atomically update `/usr/local/etc/sing-box/config.json` and
restart `sing-box.service`:

```sh
sudo .pixi/envs/default/bin/python -m sing_box_manager \
  --config config/inventory/runtime.yaml control-plane --action sync-node
```

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
