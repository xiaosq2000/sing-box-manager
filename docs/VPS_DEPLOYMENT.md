# VPS Deployment

Checked on 2026-10-21.

This is the primary deployment path for `sing-box-manager`.

It assumes:

- an Ubuntu or Debian VPS reachable via SSH
- DNS for `deployment.host` already points at the VPS
- the Pixi binary is pre-installed at `/usr/local/bin/pixi`
- the repo lives at `/opt/sing-box-manager` (configurable with `--remote-root`)

## Port Layout

- `443/tcp`: Nginx HTTPS portal
- `80/tcp`: Nginx ACME challenge and HTTP redirect
- `8443/tcp`: Trojan
- `4443/udp`: Hysteria2 (base listen port)
- `20000:50000/udp`: Hysteria2 port-hopping range (only if enabled — see below)
- `9443/tcp`: Naive
- operator-selected `deployment.snell_port/tcp`: optional Snell v6
- `47071/tcp`: Django portal and control plane, bound to `127.0.0.1`

Enable the [control plane](CONTROL_PLANE.md) for portal service. It uses OS-managed
PostgreSQL and one hardened Django service. Nginx forwards the portal to Django,
except for Admin and health routes. Admin remains tunnel-only.
With `authoritative: true`, PostgreSQL also supplies the node's user roster.
Returning to non-authoritative mode changes node configuration only, not portal authentication.
Read the [mode-transition warnings](CONTROL_PLANE.md#disable-without-removing-data) first.
Do not open the Django or database ports in ufw.

When the control plane is enabled, deployment also installs `/usr/local/bin/sbm`.
It uses the deployed Python environment and runtime inventory from any working
directory. For example, run `sudo -u sbm sbm control-plane createsuperuser` to
create an operator, or `sudo sbm control-plane sync` to apply database-managed
accounts to the node. See [command syntax and the wrapper](CONTROL_PLANE.md#command-syntax-and-vps-wrapper)
for aliases and configuration overrides.

## Snell v6 (optional)

Set `deployment.snell_port` and `snell.psk` together. Snell runs in the existing
node service and needs no TLS certificate or separate UDP port. Bootstrap opens
the selected TCP port. On an existing VPS, admit it in ufw and any provider
firewall during the authorized rollout. Ordinary deploy does not change ufw.
See [Snell setup and account backfill](SNELL.md) for migration, revocation,
verification and rollback. Do not reimport authoritative accounts.

## Hysteria2 Port Hopping (optional)

Port hopping makes the Hysteria2 client rotate its destination UDP port across
a range, which resists port-based throttling/blocking. The sing-box server
still listens on the single `deployment.hysteria2_port`, so the kernel must
**redirect the whole UDP range back to that port**. Opening the range in the
firewall is necessary but _not_ sufficient on its own — the NAT redirect is what
makes it work.

Enable it by setting the range in your inventory (both endpoints read the same
value):

```yaml
deployment:
  hysteria2_port: 4443
  hysteria2_port_range: 20000:50000
```

Run `sbm release` and then `sbm deploy` with the same inventory so new client
packages contain `server_ports`. Users must install or upgrade to those packages.
Bootstrap configures the firewall and redirect automatically. On an existing VPS,
apply the following rules manually or rerun bootstrap after reviewing its other
provisioning steps:

1. Open the UDP range:

   ```sh
   sudo ufw allow 20000:50000/udp comment 'Hysteria2 port hopping'
   ```

2. Add the NAT redirect. Edit `/etc/ufw/before.rules` and add a `*nat` block at
   the very top of the file, above the existing `*filter` line (match the port
   in `--to-ports` to `deployment.hysteria2_port`):

   ```
   *nat
   :PREROUTING ACCEPT [0:0]
   -A PREROUTING -p udp --dport 20000:50000 -j REDIRECT --to-ports 4443
   COMMIT
   ```

   If your clients reach the VPS over IPv6, add the same `*nat` block to
   `/etc/ufw/before6.rules`. Then reload:

   ```sh
   sudo ufw reload
   ```

Verify the redirect is active with `sudo iptables -t nat -L PREROUTING -n`.
To disable hopping later, remove `hysteria2_port_range` from the inventory,
rebuild and redeploy, then delete the `ufw` rule and the `before.rules` block
(and the corresponding IPv6 rules, if configured).

## UDP buffer tuning

`server-install.sh` installs `/etc/sysctl.d/99-sing-box-hysteria2.conf` raising
`net.core.rmem_max` / `net.core.wmem_max` to 16 MiB when installing the
single node service with a Hysteria2 inbound. This prevents QUIC packet drops at high throughput; no
manual action is needed.

## 1. One-Command Bootstrap

The server is **not** a git checkout. It has no git, no Git LFS, and no
credential that can read this repo — `sbm deploy` ships the code over `rsync`
along with the release. If the VPS currently holds a clone with a deploy key or
PAT, that credential can be revoked once the first `--bootstrap` deploy
succeeds.

The only prerequisites on the VPS are: SSH access as `root`, `pixi` at
`/usr/local/bin/pixi`, plus `systemctl` and `rsync` on `PATH`.

From the Linux build machine, build the release and bootstrap the server. Replace
`vpn-host` with your SSH alias and use the same inventory for both commands:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml release
pixi run sbm --config config/inventory/runtime.sops.yaml deploy --hostname vpn-host --bootstrap --certbot-email admin@example.com
```

`--bootstrap` handles the entire VPS setup automatically:

1. **Verifies** the remote is Ubuntu or Debian (fails fast otherwise)
2. **Creates** the `sbm` system user and the `/opt/sing-box-manager` directory,
   owned by `root:sbm` with mode `0750`
3. **Installs** nginx, certbot, and python3-certbot-nginx via apt
4. **Ships** a merged plaintext config file at `config/inventory/runtime.yaml`
   from your local encrypted inventory
5. **Configures ufw**: opens the ports sshd listens on and the port of the deploy's
   own connection (22 only when neither is found), then HTTP, HTTPS, and all
   protocol ports; writes the NAT redirect rules for Hysteria2 port hopping if
   `hysteria2_port_range` is set
6. **Installs** the nginx reverse-proxy site and reloads nginx
7. **Obtains** a TLS certificate via `certbot --nginx` (pass `--certbot-email`
   for renewal notifications, or omit to register without email)
8. **Syncs** project code and runs `pixi install --locked`
9. **Syncs** release artifacts and installs one `sing-box.service` with all packaged protocol inbounds
10. **Installs and enables** `sing-box-manager-traffic-stats.timer` when enabled and
    `certbot-renew.timer` for automatic certificate renewal
11. **Installs** Django and PostgreSQL when `control_plane.enabled` is true; migrates
    the database, collects static files, checks readiness, and routes Nginx to Django.
    It disables the retired Web service. Public Admin requests return 404
12. **Configures** Nginx token-redacted logs and compatibility-endpoint rate limits

Bootstrap can be rerun after a failure, but it reapplies configuration and runs
installation and service operations again. Inspect the failed step and remote
state before retrying; a rerun is not a rollback or a guarantee of no changes.
Use an ordinary `sbm deploy` for later releases. It reuses the managed `rsync` at
`<remote-root>/.pixi/envs/default/bin/rsync`.

## 2. Server Configuration

Deploy derives the server's `config/inventory/runtime.yaml` from your local settings
and encrypted inventory. The server never needs SOPS or your age key. Every ordinary
deploy stages, validates, and atomically publishes the derived file, so settings such
as `vps_info` and every `traffic_stats` key reach an already-bootstrapped server. The publisher retains the server's
current `web.session_secret`, `web.subscription_secret`, and Django secret key,
so legacy installer tokens, subscription links, and operator sessions stay valid.
Authoritative deployment omits legacy inventory user lists from this derived file.

Enable Django in infrastructure settings:

```yaml
control_plane:
  enabled: true
  authoritative: false # set true only after initial account import
```

Follow [account setup](CONTROL_PLANE.md#account-import-admin-management-and-node-sync) for database migration and import.
The portal uses PostgreSQL accounts even before node authority is enabled.
Grant Django staff `control_plane.view_traffic` to see all users' traffic at
`/admin/traffic/` through an SSH tunnel. The inventory `admin` flag grants no permissions.

`web.session_secret` signs legacy installer machine tokens. If the inventory leaves it unset, bootstrap
generates one for the server. Every later deploy keeps the server's value, even when
the inventory sets a different one. To rotate it, delete `web.session_secret` from the
server's `runtime.yaml`, set the new value in the inventory, and deploy. Rotation
invalidates saved installer tokens, such as
`${XDG_CONFIG_HOME:-$HOME/.config}/sing-box/portal-token`.

`web.subscription_secret` keys subscription links. If the inventory leaves it unset,
the next deploy generates one for the server, and later deploys keep it. Rotating it
the same way revokes every subscription link.

Configure Unix client upgrade notices in the same inventory:

```yaml
client_upgrade:
  policy: suggested # off, suggested, or required
  message: "" # optional public, single-line text; never put secrets here
```

`suggested` hints once for each newly offered client build. `required` emits a louder
warning in every new shell until the client is upgraded, but remains non-blocking.
Neither shell notice prints the message; `proxy check update` reports it so new shells
stay quiet. The policy, message, release build ID, commit, and successful-deploy
timestamp are published without authentication at `/api/client-update`. A release that carries `sbc` reports the `sbc` build, so a policy other
than `off` asks every bash client to run `proxy upgrade`, which moves it to `sbc`.

Edit the runtime inventory with `sops` on your **local machine**. See
[`docs/SECRET_HANDLING.md`](./SECRET_HANDLING.md) for the encrypted-secret
workflow. Bootstrap and subsequent deploys decrypt and ship a plaintext copy to the
server. The remote file is owned by `root:sbm` with mode `0640`.

`deployment.host` is the canonical hostname for the Nginx `server_name`, Django
allowed hosts, and CA-signed sing-box `tls.server_name`.

## 3. Subsequent Deploys

After the initial bootstrap, rebuild and redeploy with:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml release
pixi run sbm --config config/inventory/runtime.sops.yaml deploy --hostname vpn-host
```

For authoritative deployments, do not pass `--protocol`.
Read [schema upgrades](CONTROL_PLANE.md#schema-upgrade) and
[deployment ordering](REMOTE_DEPLOYMENT.md#what-the-command-does) before deployment.

A deploy carries every `ssh` and `rsync` over a single shared connection and
retries failed connection setup up to five times. No extra `~/.ssh/config` settings
are needed. If a deploy fails partway through, code, config, or services may already
have changed. Inspect the failed step and service state before rerunning. See
[`REMOTE_DEPLOYMENT.md`](./REMOTE_DEPLOYMENT.md#the-ssh-connection) for what is
retried and what deliberately is not.

The deploy also synchronizes the merged runtime config. This is what activates local
portal settings such as `vps_info.kiwi_veid` and `vps_info.kiwi_api_key` on an existing
VPS before the web service restarts.

Every deploy rewrites the managed systemd units too, so a release that changes a unit
reaches the host without a second `--bootstrap`. Any hand edit you make to
`/etc/systemd/system/sing-box-manager-*.service` or `.timer` is overwritten on the next
deploy; change the generators in `sing_box_manager/provision.py` instead. When traffic
stats are enabled, deploy then runs the collector once and fails if that collection
does not succeed, so a collector that cannot reach its database or its APIs stops the
deploy instead of failing quietly every five minutes afterwards.

After service checks succeed, deploy atomically publishes
`config/generated/deployment-info.json`. `/api/client-update` reads this marker;
a failed deploy cannot advance the timestamp. Mismatched release IDs hide the timestamp.

## Manual Steps (for reference)

The sections below describe what `--bootstrap` does automatically. They are
useful for understanding the server layout or troubleshooting, but you do not
need to run them when using `--bootstrap`.

### Install sing-box Services

There is no tarball to extract — the server package is a directory:

```sh
cd /opt/sing-box-manager/releases/<release-dir>/server
```

Install the node service with all packaged protocol inbounds:

```sh
sudo ./server-install.sh
```

The packaged installer writes `/usr/local/etc/sing-box/config.json` as root-only
and installs `/etc/systemd/system/sing-box.service`. Repeat `-p PROTOCOL` to select
an exact subset of packaged inbounds; omitting it installs all available ones.
The release omits Naive when `naive.users` is empty or every entry is disabled,
because sing-box rejects a Naive inbound without users. Snell is omitted without
enabled users to prevent PSK-only authentication. Explicitly selecting
an omitted inbound fails before services are changed.

Do not run an old packaged installer directly on a database-managed node. Use
`sbm deploy` and follow the [control-plane guide](CONTROL_PLANE.md).

The installer stages and checks the binary and config against the node's actual
TLS files before stopping services. It stops and disables the three managed legacy
protocol units, installs the single unit and restarts it. A failed installation
restores the previous files and running/enabled service state. If recovery itself
fails, it reports the private recovery directory and retains the backups for manual
repair. Old protocol configs
are removed only after the new unit passes its health check. The portal and traffic
units are not migration targets. Back up the traffic database before a planned
migration; no traffic collection can recover bytes that were not polled before a
sing-box restart.

### Install the Web Service

Use `sbm deploy` to install `sing-box-manager-control-plane.service` together with
PostgreSQL permissions, static assets, and the sync trigger. The unit generator is
`control_plane.deployment.service_unit()`. It uses the deployed Python interpreter,
loopback, and the unprivileged `sbm` identity with systemd hardening.
Do not enable the retired `sing-box-manager-web.service`.

### Install the Traffic Stats Collector

If `traffic_stats.enabled: true`, install the example collector service and timer:

```sh
sudo cp /opt/sing-box-manager/docs/examples/systemd/sing-box-manager-traffic-stats.service /etc/systemd/system/
sudo cp /opt/sing-box-manager/docs/examples/systemd/sing-box-manager-traffic-stats.timer /etc/systemd/system/
sudo cp /opt/sing-box-manager/docs/examples/systemd/sing-box-manager-traffic-stream.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sing-box-manager-traffic-stats.timer
sudo systemctl enable --now sing-box-manager-traffic-stream.service
```

The example timer runs `stats-collect` every 5 minutes as the `sbm` user. The web,
poller, and stream units all select `config/inventory/runtime.yaml` explicitly,
matching the generated units. Adjust the config path and working directory before
enabling them if your install layout differs.

The stream unit runs continuously and collects the domain breakdown. It uses
`Restart=always` because sing-box's bounded replay history may not cover a collector
outage. Quota accounting uses the separate poller. See
[RELEASE_AND_PORTAL.md](./RELEASE_AND_PORTAL.md#two-collectors-one-authority).

All three units declare `StateDirectory=sing-box-manager`, so systemd creates
`/var/lib/sing-box-manager` owned by `sbm` and keeps it across restarts. That is where
the usage database lives by default, which is what lets you wipe and reprovision
`/opt/sing-box-manager` without losing traffic history. Keep the `StateDirectory` lines
if you edit these units.

### Install the Nginx Reverse Proxy

Bootstrap creates the HTTP site for `deployment.host`. Deployment then updates the
managed portal block and `/etc/nginx/conf.d/sing-box-manager-log.conf`.
The generators are in `provision.py` and `control_plane/deployment.py`.

The final proxy targets `127.0.0.1:47071` by default. It sets forwarded headers,
returns 404 for public Admin and health requests, redacts subscription tokens from
access logs, and applies a shared rate limit to `/auth`, `/api/token`, and `/api/sub`.
Use `sudo nginx -t` before reloading any manual change. Deployment replaces managed blocks.

Legacy Unix and Windows installers exchange saved credentials through `POST /api/sub`.
Keep this endpoint reachable. New `sbc` installs use a subscription URL, not portal login.

### Obtain the CA-Signed Certificate

```sh
sudo apt-get update
sudo apt-get install -y certbot python3-certbot-nginx
```

Once the HTTP Nginx site is live, ask Certbot to update the Nginx config and install
the certificate:

```sh
sudo certbot --nginx -d vpn.example.com
```

After Certbot succeeds, the inventory example paths match the generated certificate
layout under `/etc/letsencrypt/live/<deployment.host>/`.

## Verify the Deployment

Check the local web service:

```sh
curl -fsS http://127.0.0.1:47071/health/
```

Check the public HTTPS portal:

```sh
curl -fsS -o /dev/null https://vpn.example.com/install.sh
curl -sS -o /dev/null -w '%{http_code}\n' https://vpn.example.com/admin/
```

The Admin request must return 404. Test an existing subscription privately; do not
put the token in shared logs or shell history.

Check the systemd services:

```sh
sudo systemctl status sing-box.service
sudo systemctl status sing-box-manager-control-plane.service
sudo systemctl status sing-box-manager-traffic-stats.timer
```

## Certbot Renewal

Bootstrap and ordinary deploys install `certbot-renew.service` and enable its timer.
The generated service runs `certbot renew --quiet` with a deploy hook that reloads
Nginx and uses `systemctl try-restart sing-box.service` after renewal. A stopped
node service is not started by the renewal hook. The timer schedules
two runs daily with a randomized delay.

The definitions live in `sing_box_manager/provision.py` and are rewritten by
deploy. The manual examples below match those definitions; they are only needed
when maintaining the service outside the deploy workflow.

If Certbot came from your distro package, check whether the built-in timer already exists:

```sh
sudo systemctl status certbot.timer
sudo systemctl enable --now certbot.timer
```

For a manual setup without a renewal timer, add a dedicated service and timer.

Create `/etc/systemd/system/certbot-renew.service`:

```ini
[Unit]
Description=Renew Let's Encrypt certificates

[Service]
Type=oneshot
ExecStart=/usr/bin/env certbot renew --quiet --deploy-hook "systemctl reload nginx && systemctl try-restart sing-box.service"
```

Create `/etc/systemd/system/certbot-renew.timer`:

```ini
[Unit]
Description=Run Certbot renewal twice daily

[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=43200
Persistent=true

[Install]
WantedBy=timers.target
```

Enable the custom timer:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now certbot-renew.timer
```

Verify the schedule and run a manual check:

```sh
sudo systemctl list-timers certbot-renew.timer
sudo systemctl start certbot-renew.service
sudo journalctl -u certbot-renew.service -n 50
```

Notes:

- `certbot renew` only renews certificates that are close to expiry, so checking twice
  daily is normal and low overhead.
- The `--deploy-hook` runs only when a certificate was actually renewed.
- `systemctl reload nginx` makes Nginx pick up the new certificate without a full
  restart.
- Keep the renewal hook pointed at the single managed `sing-box.service`.
