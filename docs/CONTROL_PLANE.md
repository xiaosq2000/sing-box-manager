# Control-plane guide

Checked on 2026-10-19.

The Django 5.2 service (`sing_box_manager/control_plane/`) manages operator
accounts, proxy user accounts (`Account`), per-protocol credentials (`Credential`),
subscriptions (`/sub/<token>`), signed client downloads (`/sub/<token>/files/<path>`),
traffic pages, legacy client migration APIs, and single-node `sing-box` configuration sync.
It is the only Web service. Public password login and per-user release archives are retired.
Existing proxy credentials, subscription tokens, and signed download contracts remain unchanged.

## Layout

| Component      | Location or behavior                                                                    |
| -------------- | --------------------------------------------------------------------------------------- |
| Django         | `sing_box_manager/control_plane/`, served by Gunicorn                                   |
| Admin          | `http://127.0.0.1:47071/admin/`, through an SSH tunnel (returns 404 off loopback)       |
| Subscriptions  | `/sub/<token>` (`sbc` & mobile), `/sub/<token>/` (browser page), `/sub/<token>/files/…` |
| Installers     | `/install.sh`, `/install/linux.sh`, `/install/macos.sh`, `/install.ps1`                 |
| Readiness      | `/health/`; checks database access, migrations, and admin/portal static assets          |
| Service        | `sing-box-manager-control-plane.service`, running as `sbm`                              |
| Database       | OS-managed PostgreSQL 14 or later; database `sbm_control_plane`                         |
| Database login | Local Unix socket, role `sbm`, peer authentication                                      |
| Static files   | Root-owned `config/generated/control-plane-static/`                                     |
| Models         | `User` (operators), `Account` (proxy users), `Credential` (protocol secrets)            |

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
  authoritative: false
  port: 47071
  database_name: sbm_control_plane
  database_socket_dir: /var/run/postgresql
  database_port: 5432
```

Set `control_plane.authoritative: true` after importing accounts. Deployments without
`--skip-code` run `control-plane sync-node` (applying PostgreSQL-managed
accounts to `sing-box.service`), enables `sing-box-manager-sync.path` (so saving or
deleting accounts in Django Admin requests a `sing-box` config sync after transaction commit),
and routes the portal through Django. Nginx rejects public `/admin` and `/admin/` requests.
Deployment checks Django and reloads Nginx before it disables the retired FastAPI service.
A failed Nginx check or reload attempts to restore the previous site.
When creating a new `Account` in Django Admin (`http://127.0.0.1:47071/admin/`),
leaving `Credentials` blank generates random credentials for all three protocols
(`trojan`, `hysteria2`, `naive`) and a
ready-to-share `https://<host>/sub/<token>/` user page URL.

`secret_key` is optional for release and deploy. Deploy generates it when absent
and preserves the server's key on later deployments, including disable/re-enable.
Local Django commands require a configured key of at least 50 characters.

Build and deploy with the same inventory. Replace `vpn-host` with the target alias:

```sh
pixi run sbm --config /path/to/runtime.sops.yaml release
pixi run sbm --config /path/to/runtime.sops.yaml deploy --hostname vpn-host
```

## Command syntax and VPS wrapper

Use `sbm control-plane ACTION`. Omitting the action runs `check`.
The `--action ACTION` form remains supported for scripts and systemd units.
Use one action form per command, not both.

| Concise action    | Full action        |
| ----------------- | ------------------ |
| `createsuperuser` | `create-admin`     |
| `import`          | `import-inventory` |
| `sync`            | `sync-node`        |

Other actions are `migrate`, `collect-static`, `serve`, `health`, and `check`.
Both action forms accept concise and full names.

When the control plane is enabled, deployment installs `/usr/local/bin/sbm` as a
root-owned POSIX shell script with mode `0755`. The wrapper:

- changes directory to the deployment root (`/opt/sing-box-manager` by default)
- executes `<remote-root>/.pixi/envs/default/bin/python -m sing_box_manager`
- exports `SBM_CONFIG=<remote-root>/config/inventory/runtime.yaml` only when `SBM_CONFIG` is unset
- exports `PYTHONDONTWRITEBYTECODE=1` to prevent writes to the root-owned source tree
- passes all command arguments unchanged

A deployment with `--remote-root` writes that root into the wrapper.
An explicit global `--config PATH` overrides `SBM_CONFIG`.
`--skip-code` leaves the wrapper unchanged. Disabling the control plane retains
an installed wrapper and the database.

From a workstation, select your local inventory and use `--remote` to run an action
on the VPS. Both concise and full names support `--remote-root`:

```sh
pixi run sbm --config /path/to/runtime.sops.yaml control-plane createsuperuser --remote vpn-host
pixi run sbm --config /path/to/runtime.sops.yaml control-plane sync-node --remote vpn-host --remote-root /srv/sing-box-manager
```

Remote actions use the deployed runtime inventory. They run as `sbm`, except
`collect-static`, `sync`, and `sync-node`, which run as root.

## Account import, admin management, and node sync

Before enabling authoritative mode, import accounts, protocol credentials, and SQLite
reset counters from the runtime configuration. The transaction verifies server/client
config parity. Repeat imports preserve the highest reset counter:

```sh
sudo -u sbm sbm control-plane import
```

Import refuses to run when `control_plane.authoritative: true`. Do not disable
that setting to repeat an import: inventory can overwrite Admin changes and disable
accounts that exist only in PostgreSQL. Deploy never imports accounts automatically.

In Django Admin (`/admin/`), **Operators** manages dashboard access. **Accounts**
manages proxy users and inline `Credential` rows (`trojan`, `hysteria2`, `naive`):

| Switch                   | Effect                                                                   |
| ------------------------ | ------------------------------------------------------------------------ |
| `Account.enabled`        | Master switch for subscriptions, downloads, and all protocol credentials |
| `Account.portal_enabled` | Allows subscription access without requiring a portal password           |
| `Credential.enabled`     | Enables one protocol when its account is enabled                         |

Re-enabling an account retains its individual protocol settings and secrets.
Disabling subscription access does not revoke credentials already held by a client.
Account disablement removes those credentials from the node after a successful sync.
A changed config requires a sing-box restart, which interrupts existing connections on this single node.
All portal entry points query PostgreSQL. There is no inventory or authentication-snapshot fallback.

The [planned clean break](REFACTOR_SPEC.md#clean-break-and-legacy-removal) follows
Linux TUN, device management, the node agent, and multi-node acceptance.
Its user flow uses operator-issued single-use device enrollment codes, not user login passwords.
At that cutover, `portal_enabled`, `portal_password_hash`, and `is_portal_admin`
are removed with legacy authentication and account subscriptions. They remain operational today.
Do not reset credentials or remove these fields merely because the roadmap approves their retirement.
Django operator authentication and permissions remain separate and are retained.

- **New portal password** is optional legacy-client authentication. Blank preserves
  an existing hash. New accounts need no portal password or generated hash.
- New accounts with no supplied credentials receive three random protocol secrets.
  Deleting every credential from an existing account does not generate replacements.
- Use **Reset subscription links for selected accounts** to advance the read-only
  `subscription_counter`. Old links fail, but downloaded proxy credentials remain valid.
- Admin requests sync only after a successful transaction commit. A rollback does
  not request sync. A trigger-write failure reports that changes were saved but sync failed.
  A successful request does not prove that the node applied the change. Check
  `sing-box-manager-sync.service`, or run the manual sync below.

To render the server `sing-box` config from PostgreSQL accounts, validate it with
`sing-box check`, and atomically update `/usr/local/etc/sing-box/config.json` and
restart `sing-box.service`:

```sh
sudo sbm control-plane sync
```

Sync and the server installer share `/usr/local/etc/sing-box/.sync.lock`. Sync reads
PostgreSQL only after it acquires this lock. Root temporarily uses the `sbm` identity
for the peer-authenticated query, then restores root privileges to apply the config.

Sync compares parsed JSON, not formatting. If the config matches and the service is active,
sync does not replace the file or restart sing-box. An inactive service still requires recovery.

A rejected config leaves the active file unchanged. A failed restart or health check
restores the previous file atomically and attempts to restart the previous service.
Recovery failure returns an error. Inspect the service before retrying. If no previous
config exists, sync removes the failed candidate. Checker output is not logged because
it can contain credentials. Database edits remain saved even when node sync fails.

## User pages and private traffic views

Users open `/sub/<token>/` to read their subscription link, installation commands,
current usage, and [mobile import buttons, local QR codes, and JSON downloads](README_MOBILE.md).
They cannot change credentials or reset links there.
The link grants access. A user who loses it must request it from an operator.
`traffic_stats.show_user_domains` controls personal domain visibility in both the page and `/api/usage`.

Django staff need `control_plane.view_traffic` for these tunnel-only routes:

| Route                              | View                                                               |
| ---------------------------------- | ------------------------------------------------------------------ |
| `/admin/traffic/`                  | All accounts, billing-cycle totals, trends, protocols, and domains |
| `/admin/traffic/users/<username>/` | One account's history, protocols, domains, and destination IPs     |
| `/admin/traffic/connections/`      | Up to 100 active connections, refreshed every 10 seconds           |

Superusers have this permission. Grant it to other operators through Django permissions or groups.
It does not grant account-edit permission. `Account.is_portal_admin` is retained for initial-import parity only.
It does not authorize an operator or a traffic view. Historical traffic remains visible after account removal.

## Traffic collection

In authoritative mode, the traffic collector reads accounts and enabled credentials from
PostgreSQL. It includes accounts created only in Django Admin. Account and protocol
switches determine collection targets; `portal_enabled` does not.
A database error fails collection instead of using the obsolete inventory roster.
Non-authoritative mode continues to read inventory users. Usage history remains in SQLite.
Deployment checks traffic collection after the final node sync.

## Schema upgrade

Migration `0004_traffic_permission` adds the operator traffic permission.
It does not change proxy credentials, passwords, counters, or operator grants.
Back up PostgreSQL, infrastructure secrets, release metadata, and SQLite history before the authorized portal cutover.
Keep the deployed source and environment for rollback. Do not delete old service files or backups during the cutover.
The acceptance limit is less than 60 seconds of proxy interruption, measured on the target.
Native CI times node installation. Production acceptance must still verify the actual service interruption.

Back up the database and infrastructure configuration before an authorized upgrade.
Migration `0003_account_portal_enabled` preserves effective access, secrets, and counters:

- The old `enabled` value becomes `portal_enabled`.
- The master switch remains enabled if the portal or any protocol credential was enabled.
- Accounts with neither type of access remain disabled.

Thus an account with disabled portal access and an enabled protocol keeps that protocol.
To revoke all access, disable its master switch after the upgrade and verify node sync.
The data migration has no automatic reverse operation: the old model cannot enforce
account-wide revocation. Do not revert application code alone: old code treats the
master switch as the portal switch. Use a protected backup for a deliberate schema rollback.

## Create an operator and open the admin

Create the first account from an interactive terminal. The command prompts for the
password. Do not put passwords in command arguments or environment variables.

```sh
ssh -t vpn-host 'sudo -u sbm sbm control-plane createsuperuser'
```

Open a tunnel from your workstation:

```sh
ssh -N -L 127.0.0.1:47071:127.0.0.1:47071 vpn-host
```

Open `http://127.0.0.1:47071/admin/`. Use a different local port if 47071 is occupied.
After a CSRF 403, open a fresh login page at the exact tunnel URL.
Keep the hostname and port unchanged during login.

Operator permissions and proxy access are separate. Edit **Accounts**, not **Operators**,
to grant or revoke proxy access. A Django superuser has application permissions,
not PostgreSQL superuser rights.

For a manual readiness check on the server:

```sh
sudo -u sbm sbm control-plane health
sudo systemctl status sing-box-manager-control-plane.service
```

Run database migrations as `sudo -u sbm sbm control-plane migrate`.
Collect static files as root during deployment. The service account must not write
those files or the deployed source tree.

## Back up and test recovery

These commands use the default database and local PostgreSQL cluster.
Keep dumps private and outside the checkout. Use a new filename for each backup.
Keep an encrypted copy on another host. Do not commit dumps or database files.
Create a backup after cutover and continue regular backups: the inventory does not
contain accounts or credential changes made in Django Admin.

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
sudo -u sbm psql --dbname=sbm_control_plane_restore --command='SELECT count(*) FROM control_plane_user; SELECT count(*) FROM control_plane_account; SELECT count(*) FROM control_plane_credential;'
```

For recovery, select the restored name in `control_plane.database_name`, then build
and deploy. Check readiness and admin login. Keep the original database until the
operator authorizes its removal. A database backup does not contain the Django
signing key; keep a protected backup of infrastructure configuration too.

## Disable without removing data

An ordinary deployment uses the incoming `authoritative` setting. `--skip-code` uses
the deployed setting and cannot switch modes.

| Incoming mode              | Node configuration                                                        | Public routes and watcher                                   |
| -------------------------- | ------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Authoritative              | Preserve the live config during binary installation, then sync PostgreSQL | Route the portal to Django and enable the watcher           |
| Enabled, not authoritative | Install the selected release config                                       | Keep Django portal and Admin; disable the watcher           |
| Disabled                   | Install the selected release config                                       | Return 503 publicly; disable the watcher and Django service |

Before leaving authoritative mode, build and review the intended inventory-backed release.
**Database-only accounts and Admin credential changes do not transfer back to inventory.**
A return to inventory can revoke those users or restore obsolete credentials and links.
Do not use this transition merely to bypass the import restriction.

Ordinary deployments pause the watcher before node installation and wait up to 60 seconds
for an existing sync to finish. A timeout stops deployment without killing the sync.
Authoritative deployment enables the watcher again before the final sync.
If deployment fails while the watcher is paused, resolve the failure and repeat deployment.
Until then, Admin saves do not automatically apply to the node.

Portal authentication never returns to inventory, even when node authority is disabled.
Set both `control_plane.enabled` and `control_plane.authoritative` to `false` to disable Django.
Deployment retains PostgreSQL, database records, and the signing key. It does not reverse
migrations. Do not assume an older application revision can read the current schema.

## Verification

```sh
pixi run -e control-plane-test test tests/test_routes.py tests/test_control_plane*.py tests/test_deploy.py tests/test_provision.py tests/test_remote_config.py tests/test_server_install.py
```

The test environment supplies PostgreSQL 17. Tests start an isolated cluster with
no TCP listener and a private socket directory. Its temporary authentication rules
are fixtures, not production configuration. Tests cover migrations, admin access,
CSRF and host checks, static files, repeat provisioning, database privileges,
backup restoration, action aliases, the deployed wrapper, and disabled-state
retention. They do not contact a live VPS.
CI requires these real-database checks. It also runs a native systemd deployment
smoke test on a disposable GitHub runner. That test checks peer authentication,
read-only assets, real node installation, watcher-driven account switches, no-op sync,
whole-portal Nginx routing, retirement of the old service, authority transitions,
repeat deployment, and fail-closed disablement.
See [Development](DEVELOPMENT.md).
