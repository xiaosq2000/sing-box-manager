# VPS Deployment

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
- `47070/tcp`: local FastAPI service bound to `127.0.0.1`

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
10. **Installs and enables** `sing-box-manager-web.service`,
    `sing-box-manager-traffic-stats.timer` (if traffic stats are enabled), and
    `certbot-renew.timer` for automatic certificate renewal
11. **Switches** the nginx site to an access log that records `/sub/[token]` in
    place of subscription tokens, as every later deploy also checks

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
current `web.session_secret` and `web.subscription_secret`, so browser sessions,
installer machine tokens and subscription links stay valid.

To allow an operator account to see all collected users' traffic totals, set
`web_portal.users[].admin: true` for that account in the runtime inventory and open
`/admin` after logging in.

`web.session_secret` signs browser sessions and installer machine tokens, and
`sbm serve` refuses to start without it. If the inventory leaves it unset, bootstrap
generates one for the server. Every later deploy keeps the server's value, even when
the inventory sets a different one. To rotate it, delete `web.session_secret` from the
server's `runtime.yaml`, set the new value in the inventory, and deploy. Rotation signs
out every browser and invalidates saved installer tokens, such as
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
timestamp are published without authentication at `/api/client-update` and in the
portal footer. A release that carries `sbc` reports the `sbc` build, so a policy other
than `off` asks every bash client to run `proxy upgrade`, which moves it to `sbc`.

Edit the runtime inventory with `sops` on your **local machine**. See
[`docs/SECRET_HANDLING.md`](./SECRET_HANDLING.md) for the encrypted-secret
workflow. Bootstrap and subsequent deploys decrypt and ship a plaintext copy to the
server; the plaintext remote file remains mode `0600`.

`deployment.host` is the canonical hostname for the Nginx `server_name`, FastAPI
trusted hosts, and CA-signed sing-box `tls.server_name`.

## 3. Subsequent Deploys

After the initial bootstrap, rebuild and redeploy with:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml release
pixi run sbm --config config/inventory/runtime.sops.yaml deploy --hostname vpn-host
```

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

After the web service restart and health check succeed, deploy atomically publishes
`config/generated/deployment-info.json`. The portal uses this marker for “last
updated”; a failed or interrupted deploy cannot advance the timestamp. Times are stored
as UTC and rendered in the visitor's local timezone in the browser. If the marker does
not match the currently served release ID, the portal omits the time instead of showing
stale deployment metadata.

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
because sing-box 1.14.2 rejects a Naive inbound without users. Explicitly selecting
an omitted inbound fails before services are changed.

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

Install the generic web unit:

```sh
sudo cp /opt/sing-box-manager/docs/examples/systemd/sing-box-manager-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sing-box-manager-web.service
```

The example unit assumes:

- repo root: `/opt/sing-box-manager`, owned by `root:sbm` and not writable by `sbm`
- service user: `sbm`
- interpreter: `/opt/sing-box-manager/.pixi/envs/default/bin/python`, created by
  `pixi install`

Edit the unit before enabling it if your host layout differs.

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

Copy the example site config and replace `vpn.example.com` with your actual
`deployment.host`:

```sh
sudo cp /opt/sing-box-manager/docs/examples/nginx/sing-box-manager.conf /etc/nginx/sites-available/sing-box-manager.conf
sudo editor /etc/nginx/sites-available/sing-box-manager.conf
sudo ln -sf /etc/nginx/sites-available/sing-box-manager.conf /etc/nginx/sites-enabled/sing-box-manager.conf
sudo nginx -t
sudo systemctl reload nginx
```

The example proxy forwards to `http://127.0.0.1:47070` and preserves the forwarded
headers that the FastAPI app needs for HTTPS-aware cookies.

The Windows hosted installer also depends on `POST /api/token` working through the
same reverse proxy. If one-line installs succeed but do not write
`%LOCALAPPDATA%\sing-box\config\portal-token`, verify `/api/token` with a real portal
username and password instead of checking only `/auth` or `/download`. The Linux and
macOS one-liner installs `sbc` from the user's subscription link and needs neither.

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
curl -fsS -o /dev/null -H 'Host: vpn.example.com' http://127.0.0.1:47070/
```

Check the public HTTPS portal:

```sh
curl -fsS -o /dev/null https://vpn.example.com/
```

Check the systemd services:

```sh
sudo systemctl status sing-box.service
sudo systemctl status sing-box-manager-web.service
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
