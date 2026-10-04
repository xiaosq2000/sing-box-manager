# Remote Deployment

`sbm deploy` ships two things in one command: the project **code** and the active
**release artifacts**. The remote is a plain directory, not a git checkout — it needs
no git, no Git LFS, and no credential that can read this repo.

## Command

Run from the Linux build machine, using the same inventory as the release build.
Replace `vpn-host` with your SSH alias:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml deploy --hostname vpn-host
```

- `--hostname` is required and is passed directly to `rsync` and `ssh`
- `--remote-root` defaults to `/opt/sing-box-manager`
- `--protocol` accepts one or more of `trojan`, `hysteria2`, `naive`; omitting it
  selects all packaged inbounds in one `sing-box.service`. Naive is absent from a
  release when its roster is empty or entirely disabled. An explicit list replaces
  the active protocol set; it does not add separate services. Explicitly selecting
  an absent inbound fails before the installer changes services
- `--bootstrap` provisions a fresh Ubuntu/Debian host end-to-end: creates the service
  user and directory, installs nginx/certbot, ships derived config, configures ufw,
  obtains a TLS certificate, syncs code and artifacts, and enables all managed systemd
  services — see [`VPS_DEPLOYMENT.md`](./VPS_DEPLOYMENT.md)
- `--certbot-email EMAIL` sets the Let's Encrypt notification email during bootstrap
  (omit to register without an email address)
- `--dry-run` prints every command and lets `rsync` report what it would transfer or
  delete, without changing anything on the remote
- `--skip-code` skips code, environment, and runtime config updates; it still
  installs the selected protocol inbounds, publishes metadata, and runs service
  checks. Do not use it for the first M3 migration: the collectors and runtime
  configuration must change together with the single node process

## Requirements

1. Build a release with the selected inventory first. The auth snapshot, release
   metadata, active `releases/rel-<id>/` directory, and `releases/store/` must exist
   locally.
1. Run `pixi install` locally so the deploy environment includes `rsync`.
1. Run `git lfs pull` locally if this clone might hold pointer stubs. Deploy checks the
   bundled fonts before it opens an SSH connection and refuses to ship stubs.
1. Make sure the remote host is reachable with `ssh` as `root`. Deploy leaves
   `--remote-root` writable only by root, so no other account can deploy.
1. Make sure the remote host can run `sudo systemctl` for the target services.
1. For anything other than `--bootstrap`, the remote needs a pixi-managed `rsync` at
   `<remote-root>/.pixi/envs/default/bin/rsync`, which `--bootstrap` creates. Deploy
   invokes that binary via `rsync --rsync-path=...` so both ends speak the same wire
   protocol, and fails fast if the versions differ. It also pins rsync's internal ssh
   transport to `ssh -T -o RequestTTY=no`, so per-host SSH config like
   `RequestTTY force` cannot allocate a PTY for the data channel — PTY CR/LF
   translation corrupts the binary protocol handshake.

## The SSH Connection

`sbm deploy` shares an SSH control socket across its SSH commands and rsync
transfers. The socket lives in a temporary directory and is closed when the deploy
ends. No extra `~/.ssh/config` settings are needed. If the master connection dies,
later commands can open a new connection.

Connection setup failures are retried up to five times, with delays of 1, 2, 4,
and 8 seconds. The retry checks recognize messages such as
`kex_exchange_identification` and connection refusal. A reset during connection
setup can have several causes; the error alone does not identify the source.

Permission errors, failed remote commands, and ambiguous disconnects after a
command may have started are not retried. For example, losing the connection after
`systemctl restart` does not show whether the restart completed. Inspect the failed
step before retrying a deploy that may have changed remote state. Retry notices
appear on stderr.

## What the Command Does

1. checks the bundled LFS fonts locally, before any remote contact
1. checks the remote root is writable and has `sudo`, `systemctl`, and the expected
   `rsync`; verifies local and remote `rsync` report the same version
1. syncs the code: `sing_box_manager/`, `scripts/`, `docs/` and `config/rules/`, which the portal reads to render subscriptions,
   with `--delete` scoped strictly inside each, plus `pixi.toml`, `pixi.lock`,
   `pyproject.toml` and `config/inventory/example.yaml` one file at a time
1. runs `pixi install --locked` remotely **only if `pixi.lock` changed**, then
   re-resolves and re-version-checks the managed `rsync`, since that install can
   replace the binary the artifact transfers are about to use
1. syncs `releases/store/` (excluding `upstream/`, which is a build input) —
   compressed on the wire, and **without** `--delete`, because the store is shared
   across releases
1. syncs `releases/rel-<id>/` with `--delete`, which is safe because that directory is
   named after a hash of its own contents
1. uploads the auth snapshot, release metadata and a `VERSION` file to temporary paths
1. locks down the remote root: deletes bytecode caches under `sing_box_manager/`,
   makes everything `root:sbm`, and removes group and other write access, so the
   service account can read the tree but not change what root later runs from it
1. runs `./server-install.sh` once from `releases/rel-<id>/server/`, with repeated
   `-p <protocol>` flags only for an explicitly selected inbound set, and checks `sing-box.service`
   is active. The installer checks the real TLS files before stopping services and
   restores the prior installation if startup fails
1. publishes the staged metadata with an atomic `mv`
1. derives `config/inventory/runtime.yaml` from the local encrypted config, stages it,
   and atomically publishes it as `root:sbm` mode `0640` after preserving the server's
   existing `web.session_secret` and `web.subscription_secret`
1. rewrites the managed systemd units — the web service, the traffic stats service and
   timer plus the connection stream service when `traffic_stats.enabled` is true, and
   the certbot renewal pair — then reloads the daemon and enables them
1. writes `/etc/nginx/conf.d/sing-box-manager-log.conf`, a log format that records
   `/sub/[token]` in place of a subscription token, and adds an `access_log` line using
   it after each `server_name` in the certbot-managed site, once. It reloads nginx only
   if `nginx -t` passes, and otherwise restores both files and fails the deploy
1. restarts `sing-box-manager-web.service` if that unit exists and verifies it is
   active
1. runs `sing-box-manager-traffic-stats.service` once when traffic stats are enabled,
   and fails the deploy if the collection does not succeed
1. restarts `sing-box-manager-traffic-stream.service` and confirms it is still active
   a few seconds later. A long-running unit that crashes on startup is restarted
   forever by systemd without ever failing a deploy, so the domain breakdown would
   otherwise stop filling while everything else looked healthy
1. only after those health checks, atomically publishes
   `config/generated/deployment-info.json` with the release identity and current UTC
   timestamp

The units are generated from `provision.py`, so a host only ever receives an edit to
them by having them rewritten. Every deploy does that, not just `--bootstrap`: writing
the same bytes again is a no-op, while skipping it pins a host to the units it was
provisioned with and lets a release that depends on a new directive deploy clean and
then fail at runtime.

Release metadata built by an older builder may carry no release id. The deploy still
succeeds in that case; it prints a notice and skips the marker, and the portal omits
the “last updated” time rather than showing a stale one.

The command does not build a release for you. If the expected artifacts are missing, it
fails and asks you to run `sbm release` first.

## Examples

Deploy the single node service with all packaged protocol inbounds into the default remote root:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml deploy --hostname vpn-host
```

Preview transfers and deletions without changing remote files or services. A dry
run can still connect over SSH and requires an existing local release:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml deploy --hostname vpn-host --dry-run
```

Deploy only Hysteria2 into a custom remote root:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml deploy --hostname vpn-host --remote-root /srv/sing-box-manager --protocol hysteria2
```

## Server-Local State

`--delete` is confined to `sing_box_manager/`, `scripts/`, `docs/` and `config/rules/`, directories git owns entirely. These paths are never delete
targets:

| Path                          | What it is                          |
| ----------------------------- | ----------------------------------- |
| `.pixi/`                      | the managed environment             |
| `releases/` above `rel-<id>/` | the shared store and older releases |

The traffic history lives outside the remote root entirely, at
`/var/lib/sing-box-manager/traffic-stats.sqlite3`. Every systemd unit that touches it
declares it as their `StateDirectory`, so systemd creates it owned by the `sbm` user and leaves it
alone across restarts. That keeps the remote root disposable: it can be wiped and
reprovisioned without losing usage history. Set `traffic_stats.database_path` to a
relative path only if you want the database back inside the deploy root.

`config/generated/` is written, but one named file at a time and never with
`--delete`.

`config/inventory/runtime.yaml` is intentionally synchronized because it is derived
from the encrypted local source of truth. Deploy transfers it to a temporary mode
`0600` path, validates the YAML on the server, preserves the live session secret, and
uses an atomic replacement. Credentials are never placed in SSH command arguments.
`--skip-code` leaves this file alone along with the remote code tree.

Ordinary deploys do not replace TLS files or configure the Nginx site.
`--bootstrap` configures Nginx and obtains certificates. Both flows install the
managed renewal units; neither builds a release.

## Reclaiming Space

The shared store is append-only during a build, so nothing deletes from it on its own.
Run `sbm gc` periodically:

```sh
# Preview local cleanup
pixi run sbm --config config/inventory/runtime.sops.yaml gc --keep 3 --dry-run
# Preview cleanup on the server
pixi run sbm --config config/inventory/runtime.sops.yaml gc --keep 3 --remote vpn-host --dry-run
```

It keeps the newest N releases plus whichever one the portal is serving, and deletes
only store entries no retained manifest points at. The examples preview the cleanup.
Remove `--dry-run` to delete the listed artifacts.

## Notes

- the remote host must already have the prerequisites needed by the packaged `server-install.sh`
- root owns everything under `--remote-root`. The service account reads it through
  the `sbm` group and writes only `/var/lib/sing-box-manager`. The managed units start
  the environment's interpreter directly, not `pixi run`, because pixi may write under
  the project directory
- a `VERSION` file at the remote root records the release id, commit sha, dirty flag
  build date, and Unix client build id; it replaces `git log` for forensics on a host
  with no git
- `config/generated/deployment-info.json` records only the latest successful deploy;
  the portal ignores it when its release id does not match the served release
- if `traffic_stats.enabled: true` was set during the release build,
  `releases/rel-<id>/server/sing-box` already points at the custom-built Linux binary
  with `with_v2ray_api` (a symlink into the shared store, so it stays off the wire on
  every release that merely adds a user)
- ordinary deploys keep the remote `config/inventory/runtime.yaml` aligned with the
  encrypted local config; do not hand-edit the derived remote copy
- ordinary deploys install and enable the collector timer when
  `traffic_stats.enabled: true`; the units in `docs/examples/systemd/` are for
  manual setup
- for the full primary server setup, use [`docs/VPS_DEPLOYMENT.md`](./VPS_DEPLOYMENT.md)
