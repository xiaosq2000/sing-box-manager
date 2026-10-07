# Refactoring spec

Checked on 2026-10-06.

This spec sets the target architecture for sing-box-manager and the milestones that
move the repo there. Each milestone ships on its own and keeps the service working
for current users. When a milestone lands, update its status and the docs it
affects. Before starting a milestone, reread its section and revise it if earlier
work changed the plan.

## Why

The current design couples the manager to a single-VPS deployment. Much of its
complexity comes from machinery built around premises that the target design drops.

| Premise at the start                            | Machinery it needs                                            | Target                                        |
| ----------------------------------------------- | ------------------------------------------------------------- | --------------------------------------------- |
| Each user's archive bundles the sing-box binary | Content-addressed store, manifest, archive splicing, `sbm gc` | The binary and the config download separately |
| One sing-box process per protocol               | Three units and configs, six stats listeners                  | One process per node with every inbound       |
| Two trackers count the same bytes               | A custom sing-box build, reconciliation tables                | The sing-box API stream only                  |
| The client is bash 3.2 and PowerShell 5.1       | A 7,178-line sourced `setup.sh`, the same logic written twice | One Go binary on every desktop OS             |
| Configs start as JSON templates                 | Nine near-identical templates that code then patches          | Configs built in code                         |
| Users live in a SOPS file                       | Four credentials per person, two validators for one file      | Users in a database                           |

## Decisions

| Decision                                                                                          | Status                                     |
| ------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| Build our own minimal control plane and node agent instead of adopting an existing panel          | Decided                                    |
| Accounts are free and invite-only first; paid plans come later                                    | Decided                                    |
| sing-box is the only proxy core on nodes and clients                                              | Decided                                    |
| Security fixes come first, then the client CLI                                                    | Decided                                    |
| The control plane uses Django and Postgres                                                        | Proposed                                   |
| The client CLI, `sbc`, is written in Go with only the standard library                            | Decided                                    |
| The node agent is written in Go                                                                   | Proposed                                   |
| Each user gets one subscription URL that carries a secret token; each device gets its own from M7 | Decided                                    |
| A client runs one config per user, with routes as Clash modes and protocols in a selector         | Decided                                    |
| Configs are built in code and pass `sing-box check` before release                                | Proposed                                   |
| Nodes run the official sing-box build                                                             | Proposed                                   |
| Per-user traffic comes from the sing-box API stream only                                          | Proposed, verify in M5                     |
| Nodes apply user changes by reloading sing-box in batches                                         | Proposed, revisit if reloads disturb users |
| Protocols stay trojan, hysteria2 and naive; VLESS-Reality is an option for nodes without a domain | Open until M6                              |

sing-box 1.14.0 supports VLESS-Reality as both server and client, and `sing-box check`
accepts a server config and a client config that use it.

## Target architecture

```text
browsers, client apps, sbc
        │ HTTPS through a CDN
        ▼
control plane (Django + Postgres)
accounts, invites, plans, nodes, traffic, subscriptions, admin
        ▲
        │ each node pulls its config and pushes traffic over HTTPS
        │
node agent + sing-box on each VPS  ◀── proxy traffic from clients
```

| Component     | Runs on                                             | Built with             | Job                                                                                                                                                                                                     |
| ------------- | --------------------------------------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Control plane | Its own host behind a CDN; the current VPS until M6 | Django, Postgres       | Accounts, invites, plans, nodes, credentials, traffic, subscriptions, admin and user pages                                                                                                              |
| Node agent    | Each VPS                                            | Go                     | Fetches the node's rendered config, checks it, writes it and reloads sing-box. Reports per-user traffic from the sing-box API stream. Keeps serving the last good config when the control plane is down |
| sing-box      | Each VPS and client                                 | Official release       | Proxies traffic                                                                                                                                                                                         |
| Client CLI    | Linux, macOS, Windows                               | Go (`sbc`)             | Installs from a subscription URL, runs the service, sets the system proxy, switches route and protocol without a restart, shows status and quota, updates and uninstalls. Linux TUN. Users type `sbc`   |
| Mobile        | Android, iOS                                        | Official sing-box apps | Import the subscription as a remote profile                                                                                                                                                             |

### Control plane data

| Table           | Holds                                                                      |
| --------------- | -------------------------------------------------------------------------- |
| `users`         | Name, email, status, admin flag, inviter                                   |
| `invites`       | Code, creator, uses left, expiry                                           |
| `credentials`   | User, protocol and secret; the counter that derives the subscription token |
| `plans`         | Traffic quota, period, node groups, device limit, and a price from M8      |
| `subscriptions` | User, plan, start, expiry, reset day                                       |
| `nodes`         | Name, region, host, protocols and ports, group, capacity, agent token hash |
| `traffic_daily` | User, node, day, upload bytes, download bytes                              |
| `node_reports`  | Node and sequence number, so a repeated report changes nothing             |

A new user gets one secret that serves every protocol. Imported users keep their
current per-protocol passwords until they reset.

### Interfaces

| Request                                           | Caller      | Purpose                                                                   |
| ------------------------------------------------- | ----------- | ------------------------------------------------------------------------- |
| `GET /sub/<token>?format=sbc&os=...&sing-box=...` | `sbc`       | Returns the envelope with the user's config, nodes and latest versions    |
| `GET /sub/<token>`                                | Mobile apps | Returns the user's sing-box config                                        |
| `GET /sub/<token>/`                               | Browsers    | User page with the subscription link, QR code, install commands and usage |
| `GET /node/config`                                | Node agent  | Returns the node's rendered sing-box config with an ETag                  |
| `POST /node/traffic`                              | Node agent  | Per-user byte counts since the last report, with a sequence number        |
| `POST /node/status`                               | Node agent  | Health, sing-box version and load                                         |

Subscription responses carry a `subscription-userinfo` header with upload, download,
quota and expiry. Nodes authenticate with a per-node bearer token that the control
plane stores hashed. Binaries for sing-box and `sbc`, rule-set snapshots and their
signed manifest are served under the subscription path, so no public page identifies
the host.

Route strategies stay `china`, `gfw`, `ai` and `global`. Traffic on the `ai` route
always leaves through one fixed node, because AI vendors reject sessions whose
egress IP changes.

### Security properties

- The local proxy on clients listens on `127.0.0.1`. On hosts with other user
  accounts, it requires authentication. Sharing it with a LAN or with Docker
  containers is opt-in and requires authentication.
- Subscription tokens stay out of shell history, process arguments and nginx logs.
  Servers store only the counters that derive them.
- Clients install a binary only when it matches a manifest signed by a key that
  never reaches a server.
- Nodes only make outbound requests to the control plane. The control plane holds
  no SSH keys for nodes.
- On servers, root owns code, binaries and configs. Service accounts write only
  their state directories.
- From M7, per-destination traffic is not recorded for users who aren't admins.
- Public pages don't name the software or list node hosts.
- From M5, SOPS holds only infrastructure secrets, such as the database password,
  the CDN token and the node enrollment secret.

## Knowledge to keep

These rules came from real failures. Carry each one into the code that replaces its
current location.

| Area               | Rule                                                                                                                                                                                                                                                                                                                                                | Original location                                    |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| SSH                | One ControlMaster connection, retries only on known reset messages, `-T -o RequestTTY=no` for rsync, control path under 90 characters without spaces                                                                                                                                                                                                | `sing_box_manager/ssh.py`                            |
| Hysteria2 server   | Port hopping uses a nat PREROUTING REDIRECT of the range to one port, for IPv4 and IPv6. UDP buffers are 16 MiB                                                                                                                                                                                                                                     | `provision.py`, `scripts/server-install.sh`          |
| TLS                | Use an ECDSA or RSA certificate, not Ed25519, for Chrome QUIC parroting. Restart sing-box after renewal                                                                                                                                                                                                                                             | `docs/SECRET_HANDLING.md`, `provision.py`            |
| Client configs     | Ship rule snapshots through `initial_path` so the first start works when rule downloads fail                                                                                                                                                                                                                                                        | `release/rule_sets.py`, `release/renderers.py`       |
| Client configs     | `sing-box check` doesn't prove DNS works. Probe with `sing-box tools fetch` against a temporary cache directory                                                                                                                                                                                                                                     | `scripts/setup.sh` near line 3242                    |
| `ai` route         | Direct DNS goes through the systemd-resolved stub, because the local transport needs CAP_NET_RAW. Keep sign-in, captcha and OAuth domains in the AI list                                                                                                                                                                                            | `release/renderers.py`, `scripts/client-install.sh`  |
| Client runtime     | Mixed mode and TUN are never active together, or traffic loops. Check TCP and UDP when picking the mixed port. Run TUN with `-c`, not `-C`                                                                                                                                                                                                          | `scripts/client-install.sh`, `scripts/setup.sh`      |
| System proxy       | Revert only settings that point at our port. A PAC setting belongs to someone else                                                                                                                                                                                                                                                                  | `scripts/lib/system-proxy.sh`, `system-proxy.ps1`    |
| API stream         | `interval` is in nanoseconds. Each subscription replays live connections, so deduplicate by connection ID with byte checkpoints. UPDATE carries deltas and CLOSED carries final totals. Events without a user are DNS lookups and health checks. sing-box drops events while nobody subscribes                                                      | `connection_stats.py`, `connection_tracking.py`      |
| API service        | Disable its dashboard. Strip IPv6 brackets from the listen address                                                                                                                                                                                                                                                                                  | `release/renderers.py`, `settings.py`                |
| Billing            | Per-user counters see one leg of relayed traffic; a host that bills both directions charges about twice that. A reset day past a short month's end moves to its last day                                                                                                                                                                            | `settings.py`, `traffic_models.py`                   |
| Windows            | Launch the hosted installer with `& ([scriptblock]::Create((irm URL)))`. Use an S4U task with `ExecutionTimeLimit=PT0S`, limited run level and an Interactive fallback. Refuse to run elevated. Set WinINet as one `host:port`, separate bypass entries with `;`, refresh with InternetSetOption 39 then 95. Stop the task before replacing the exe | `scripts/client-install.ps1`, `lib/system-proxy.ps1` |
| macOS              | Run `launchctl bootout` before `bootstrap` and poll after start. Clear the quarantine attribute. Read the login shell with `dscl`. Pick the `networksetup` service from the default-route interface                                                                                                                                                 | `scripts/setup.sh`, `lib/system-proxy.sh`            |
| Linux TUN          | Keep the hardened root unit, the systemd-run capability probe, and admin tools resolved from sbin                                                                                                                                                                                                                                                   | `scripts/lib/tun.sh`, `scripts/setup.sh`             |
| Downloads in China | No CDN-hosted page assets. PowerShell `irm` needs `text/plain`. Send auth headers up front, since PowerShell 5.1 and wget wait for a challenge otherwise                                                                                                                                                                                            | `web/routes.py`, `install_windows.ps1`               |

## Milestones

| Milestone | Name                                       | Status   |
| --------- | ------------------------------------------ | -------- |
| M0        | Security fixes                             | Done     |
| M1a       | Client CLI for Linux and macOS, mixed mode | Done     |
| M1b       | Linux TUN in the client CLI                | Deferred |
| M2        | Client CLI for Windows                     | Done     |
| M3        | Configs in code, one server process        | Done     |
| M4        | Control plane and subscriptions            | Planned  |
| M5        | Node agent and traffic                     | Planned  |
| M6        | Multiple nodes                             | Planned  |
| M7        | Invites, plans and quota enforcement       | Planned  |
| M8        | Paid plans                                 | Planned  |

### M0: Security fixes

Fix the current code. Each item gets a regression test.

| Item | Change                                                                                             | Where                                                  |
| ---- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| S1   | Client mixed inbounds listen on `127.0.0.1` instead of `::` without authentication                 | `config/templates/client/*-client.json`                |
| S2   | Deploy stops running or installing, as root, files that the portal account can write               | `deploy.py` near line 678, `scripts/server-install.sh` |
| S3   | Remove the CORS middleware, which allows any origin with credentials                               | `web/app.py` near line 107                             |
| S4   | Keep the KiwiVM API key out of logs: httpx logs full URLs at INFO, and error text includes the URL | `kiwivm.py`, `web/vps_info.py`                         |
| S5   | Bootstrap allows the real sshd port before enabling ufw, instead of only 22                        | `provision.py` near line 387                           |
| S6   | `/files` and `/files/{name}` reject users removed from the inventory                               | `web/routes.py` near lines 1061 and 1089               |
| S7   | Run argon2 checks off the event loop, and bound the login rate limiter's memory                    | `web/routes.py` near line 1015, `web/auth.py`          |
| S8   | The portal refuses to start without `web.session_secret` instead of generating a random one        | `settings.py`                                          |

Done when all items are fixed, tested, released and deployed.

S1 limits two setups. WSL2 reaches a Windows-side proxy only with mirrored
networking, and a helper inside a Linux Docker Engine container can't reach the
host's proxy.

### M1: Client CLI for Linux and macOS

Replace the sourced bash client with `sbc`, one Go binary that reads a subscription
and replaces the `proxy` command with `sbc`. M1a ships mixed mode, and M1b moves
Linux TUN.

| Area         | Bash client                                                                  | `sbc`                                                                                                    |
| ------------ | ---------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Client code  | `setup.sh` and four libraries, about 8,500 lines, sourced into every shell   | A Go binary that uses only the standard library, and one `sbc init` line in the rc file                  |
| Install      | `bash <(curl …/install/linux.sh)` or `macos.sh` downloads a per-user archive | `curl -fsSL …/install.sh \| sh` downloads `sbc`, which asks for the subscription URL                     |
| Auth         | Portal password, then a machine token                                        | A subscription token                                                                                     |
| Config       | The archive holds a config for each protocol and route                       | The server renders one config per user, and `sbc` fetches it                                             |
| Switching    | Swaps the config and restarts sing-box                                       | Sets a Clash mode or a selector through sing-box's local API, without a restart                          |
| Updates      | `proxy upgrade` reinstalls the archive; a daily check prints a hint          | A timer refreshes the config every 6 hours; `sbc upgrade` installs binaries that match a signed manifest |
| Local proxy  | Loopback without auth                                                        | Loopback, with auth on shared hosts. LAN and Docker bridge sharing require auth                          |
| Integrations | Shell, git, desktop, Docker daemon and containers                            | Shell, desktop, Docker daemon and containers                                                             |
| Diagnostics  | 13 `proxy check` subcommands                                                 | `sbc status`, which shows traffic, `sbc ip` and `sbc speed`; `sbc doctor` follows M1a                    |

#### M1a: Mixed mode

Portal:

- `GET /sub/<token>?format=sbc&os=<os>&sing-box=<version>` returns a JSON envelope
  with an ETag. It holds a schema version, the user's config, the default route and
  protocol, a `nodes` list with one node, and the latest `sbc` and sing-box versions.
  Quota comes in the `subscription-userinfo` header. When the portal can't render a
  config for the client's sing-box version, it names the version needed.
- The portal builds each user's desktop config in code. A `selector` holds the
  protocols, and route and DNS rules match `clash_mode` values `china`, `gfw`, `ai`
  and `global`. Every rendered config passes `sing-box check` in CI.
- A token is an HMAC-SHA256 of the username and a reset counter, keyed by a new
  `web.subscription_secret`. The portal's SQLite database stores only the counters.
  After a password login, the portal shows the link and can issue a new one, which
  cuts off the old link.
- `POST /api/sub` exchanges a machine token for the subscription URL, so migration
  needs no paste.
- nginx logs `/sub/` requests without the token, and the portal sends
  `Referrer-Policy: no-referrer`.
- The portal serves `install.sh` for Linux and macOS. `/install/linux.sh` and
  `/install/macos.sh` serve it too until every bash install has migrated, because
  the bash `proxy upgrade` fetches them. `install.ps1` and `/install/windows.ps1`
  serve the Windows installer and legacy migration.
- `sbm release` builds `sbc` for linux-amd64, linux-arm64, darwin-amd64,
  darwin-arm64 and, since M2, windows-amd64. It signs a SHA-256 manifest of
  `sbc`, sing-box and the rule-set
  snapshots with an ed25519 key from the inventory. The key stays on the build
  machine, and deploy leaves it out of the server's config. The portal serves the
  files and the manifest under the subscription path.
- `sbm release` stops packing Linux, macOS and Windows archives. Android
  archives stay until M4.

Client:

- Add a Go module at the repo root with `cmd/sbc`. Pixi already provides Go.
- `install.sh` is POSIX sh. It detects the platform, downloads `sbc` and runs
  `sbc install`, which reads the subscription URL from the terminal. The token stays
  out of shell history and process arguments. `install.sh` accepts the flags the
  bash `proxy upgrade` passes: `-p`, `--no-rc` and `--shell`.
- `sbc install` downloads sing-box and the rule-set snapshots, so the first start
  works before rule downloads succeed. It succeeds only once a page loads through
  the proxy, which sing-box's delay test checks. A reinstall keeps the port.
- `sbc` stores the token in `~/.config/sbc/` on Linux and in
  `~/Library/Application Support/sbc/` on macOS, and writes the sing-box config
  there. Both files have mode 0600.
- sing-box runs as one systemd user unit with linger, or as one LaunchAgent. Where
  systemd is missing, `sbc run` runs it in the foreground.
- `sbc` patches the listen port, inbound auth, extra listeners, the Clash API and the
  speed test into the portal's config. The API listens on `127.0.0.1` with a random
  port and secret, and `proxy route` and `proxy protocol` call it. sing-box clears its DNS cache when
  the mode changes, and its cache file keeps the mode and the selector choice across
  restarts. This was checked in the sing-box 1.14.2 source.
- A timer refreshes the config every 6 hours after a random delay and sends the
  ETag. A changed config must pass `sing-box check`. `sbc` keeps the last good
  config and restores it if sing-box fails to restart.
- `sbc` installs a downloaded binary only when it matches the signed manifest. It
  carries a list of public keys, so a new key can ship before the old one retires.
  The first download, by `install.sh`, relies on TLS.
- On a host other people log in to, the local proxy requires a generated username
  and password, and the proxy variables carry them. Another account with a login
  shell and a UID of at least 1000, or 501 on macOS, in `/etc/passwd` counts, and so
  does a home directory beside the user's that someone else owns, which is how LDAP
  accounts show up. `sbc install --auth on|off` overrides the check, and a
  reinstall keeps the earlier choice. If port 1080 is taken for TCP or UDP,
  `sbc install` picks a free port.
- Users type `sbc`. A program cannot change the shell that started it, so the rc
  file runs `sbc init bash|zsh`, whose output puts `sbc` on PATH and adds a prompt
  hook. Each prompt applies `sbc on` and `sbc off` from a small file, so every open
  shell follows without starting a process. `export SBC_PROXY=off` keeps one shell
  off, and `eval "$(sbc env)"` sets the variables in a script.
- Messages from `sbc` and `install.sh` are in Simplified Chinese when the first of
  `LC_ALL`, `LC_MESSAGES` and `LANG` that is set starts with `zh_CN` or `zh_SG`,
  and in English otherwise. `SBC_LANG=zh` or `SBC_LANG=en` overrides the locale.
- Commands: `install`, `init`, `link`, `upgrade`, `uninstall`, `on`, `off`, `env`,
  `status`, `ip`, `speed`, `route`, `protocol`, `port`, `update`, `start`, `stop`,
  `restart`, `run`, `desktop on|off`, `docker on|off` and `version`. `on` and `off`
  point shells, and the desktop after `desktop on`, at the proxy or away from it,
  and leave sing-box running, so an agent that runs `sbc off` keeps its own
  connection. `start` and `stop` control the service. `update` refreshes the config
  now, and `link set` replaces the subscription URL.
- `sbc ip` asks three sites through the local proxy, so the answers follow the
  route. Gemini's start page names the region Google places the exit in, ipinfo.io
  the address foreign sites see, and cip.cc the address Chinese sites see. The
  Gemini page is not an API, so its format can change. `sbc` reads the region as the
  three-letter code after `,2,1,200,` (checked on 2026-10-03), and each check
  downloads about 150 KB.
- `sbc speed` tests the protocol in use, and `sbc speed --all` tests every protocol
  without switching the one in use. It runs sing-box's delay test three times and
  reports the median. It suggests no switch, because the proxy mostly carries
  downloads rather than real-time traffic.
- `sbc speed --download [MB]` downloads 20 MB, or 1 to 99 MB, from Cloudflare's
  speed test through the protocol in use, or with `--all` through each protocol in
  turn, and reports Mbit/s from the first byte. The download goes through a
  loopback `speed-test` inbound, with the proxy's password where it has one. Its
  route rule comes first and leads to a `speed-test` selector with the proxy
  selector's protocols, so `sbc` can test any protocol on any route without
  switching the one in use. A refresh adds these to a config from an earlier `sbc`.
- Each download stops after 15 seconds and fails when nothing arrives for 5. The
  server's monthly transfer grows by twice the bytes downloaded, which is why the
  default tests one protocol. With `--all`, `sbc` suggests a switch only when a
  protocol downloads at least 1.5 times as fast as the current one, since a single
  download varies.
- `sbc status` shows this billing cycle's traffic from the `subscription-userinfo`
  header of the last refresh.
- `sbc` lives in a directory of its own, which `sbc init` puts on PATH.
- `sbc desktop on` points the GNOME or macOS proxy settings at `sbc`.
  WebRTC protection persists across proxy toggles on Linux, macOS and Windows.
  `sbc webrtc off` removes owned settings, cleans exact legacy Linux policy files,
  restores Firefox's saved preferences and disables automatic setup.
  Firefox cleanup requires a closed profile; active-profile failures retain
  ownership for retry. `sbc webrtc on` enables protection again, independently of
  desktop detection.
  Browser policies use native mandatory settings. Firefox uses managed profile
  preferences. Safari is not managed. See [WebRTC protection](WEBRTC.md) for
  platform scope, ownership, restart requirements and manual cleanup.
  Firefox setup and cleanup continue through healthy profiles and report failures.
  `sbc docker on` writes the Docker daemon's systemd drop-in on Linux and restarts
  Docker after confirmation, or with `--yes`. Desktop and Docker integration refuse
  authenticated local proxies. Desktop settings cannot hold the password, and all
  accounts share the Docker daemon.
- The git proxy integration is dropped, because git reads `https_proxy`.
- Running `install.sh` over a bash install migrates it, side by side so the machine
  never loses its proxy. It exchanges the saved machine token for the subscription
  URL and installs `sbc` on a spare port with the old route and protocol, which
  checks that it proxies. Then it removes the bash client with the client's own
  uninstaller and moves `sbc` to the old port. The uninstaller keeps the Docker
  settings, which work on at the same port, and reverts the desktop proxy, which the
  migration sets again with `sbc desktop on`. `install.sh` removes the old rc lines
  and Git proxy settings itself, because the uninstaller's rc edit fails on macOS.
  The `proxy` command goes with the bash client, with no shim.
- The portal's `/api/client-update` reports the `sbc` build, so bash clients are
  told to run `proxy upgrade`, which runs the new installer. `install.sh` leaves
  `proxy upgrade` a one-time `setup.sh` to source, so it ends without an error.
- An install with TUN set up keeps the bash client until M1b.
- The rewrite fixes these known bugs:
  - `proxy protocol` requires `systemctl` on macOS (`setup.sh` near line 3130).
  - Credentials are passed on the command line.
  - About 400 helper functions are defined in every shell.

Done when CI on Linux and macOS runners installs from a test subscription, starts,
switches route and protocol without a restart, rolls back a bad refresh and
uninstalls, and when a bash install without TUN migrates.

These follow M1a as separate pull requests: `doctor`, a redacted report for support
requests; shell completion; `lan on|off`, which listens on all interfaces with a
generated username and password; and the Docker container proxy, which listens on
the Docker bridge address with auth.

#### M1b: Linux TUN

- `proxy tun on|off` moves into `sbc`. It keeps the hardened root unit and its
  verify step, and drops the journal, generations, manifest, safeguard timer and
  attestation file.
- The TUN lock is released when `sbc` exits, so Ctrl-C during `proxy tun on` can't
  leave it held (`tun.sh` near line 585).
- Bash installs with TUN migrate.

Removes `scripts/setup.sh`, the Unix install and uninstall scripts, `scripts/lib/*.sh`
except `ui.sh`, which the server scripts use until M5, and their shell tests. `README_LINUX.md`, `README_MACOS.md` and `TUN_MODE.md` become
one client guide.

Done when a Linux runner turns TUN on and off with the verify step, Ctrl-C during
`proxy tun on` leaves no lock, and a bash install with TUN migrates.

### M2: Client CLI for Windows

The same `sbc`, with the three parts that touch the operating system swapped.
Everything that goes through sing-box's API is unchanged. The installer, proxy
service and ordinary commands run unelevated. A registry-only WebRTC helper requests
administrator approval for protected browser policies and targets the original
user's hive, not the administrator's `HKCU`.

| Piece         | Linux and macOS                          | Windows                                                                                                                                                                                                                                                                                                      |
| ------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Files         | `~/.config/sbc`, `~/.local/share/sbc`    | One folder, `%LOCALAPPDATA%\sbc`, whose permissions already exclude other users. A scheduled task may lack the profile variables, so an `sbc` under `cli\` finds the folder from its own path                                                                                                                |
| Service       | systemd user unit, LaunchAgent           | Scheduled tasks `sbc-proxy` and `sbc-refresh`, written as XML and registered with `schtasks.exe`. They run whether the user is logged on or not (S4U), so there is no console window and sign-out keeps them                                                                                                 |
| `sbc run`     | execs sing-box                           | Runs sing-box as a child in a job object, so ending the task ends sing-box, and records its process id, which stopping the task waits on: Task Scheduler gives an ended task's process a few seconds before it terminates it. Without a console, output goes to `sing-box.log`, which starts over past 10 MB |
| Service state | `systemctl is-active`, `launchctl print` | PowerShell's `Get-ScheduledTask`, whose states are the same in every display language, where `schtasks` prints them in the display language                                                                                                                                                                  |
| Programs      | replaced in place                        | A running program cannot be replaced, so `WriteFile` moves it to `.old`, which the next `sbc run` deletes. Uninstall removes the folder sbc runs from after sbc exits                                                                                                                                        |
| Desktop proxy | gsettings, networksetup                  | `ProxyEnable`, `ProxyServer` and `ProxyOverride` under `HKCU\...\Internet Settings`, plus a WinINet refresh so open browsers notice                                                                                                                                                                          |
| Shells        | rc file runs `sbc init`, prompt hook     | User environment variables `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`, which new terminals, VS Code, git, pip and curl read; `sbc install` adds `cli\` to the user PATH                                                                                                                                      |
| Installer     | `install.sh`                             | `install.ps1` for Windows PowerShell 5.1: ask for the link, download `sbc.exe`, run `sbc install`. A native program needs no execution policy                                                                                                                                                                |

Where Task Scheduler refuses the S4U logon, which a policy can, the tasks run in
the user's session instead: a console window stays open and sign-out stops the
proxy, and `sbc install` says so.

Pull requests, in order:

1. Done. Windows basics: paths, the zip, the tasks, `sbc run`, uninstall; `sbm release`
   builds `sbc/windows-amd64/sbc`; the end-to-end test runs on a Windows runner.
2. Done. Native Windows validation runs in CI. `desktop on|off` through
   HKCU and WinINet (including WebRTC leak prevention policies for Chrome,
   Edge, Brave, and Firefox preferences), `on|off` through persistent user
   environment variables, and the user PATH entry. `sbc env` prints PowerShell
   commands for the current session. Port changes and uninstall preserve foreign
   settings; desktop proxy use remains opt-in. WebRTC protection persists across
   proxy toggles. `sbc webrtc off` removes owned settings and opts out of automatic
   setup; `sbc webrtc on` enables it again. Setup verifies policy writes.
   Edge uses `WebRtcLocalhostIpHandling` and requires a browser restart. Migration
   removes an owned obsolete `WebRtcIPHandling` value without claiming manual policies.
   A per-user lock serializes mode state, policy changes and profile changes across
   concurrent setup and cleanup commands. Uninstall removes owned policies. It stops
   on cleanup failure before deleting the client. `sbc` preserves pre-existing policies.
   Firefox cleanup restores original saved preferences from a per-profile snapshot,
   or resets matching values for legacy managed blocks. It refuses active profiles
   and retains ownership for retry. The Windows guide gives manual reset steps only
   for settings without ownership evidence. Fixture tests cover permission failures,
   ownership and retries. Native unelevated UAC validation is still pending.
3. Done. `/install.ps1` downloads `sbc` and migrates the PowerShell client:
   exchange its saved machine token for a link, keep port, route and protocol,
   check the new proxy before removing the old tasks and module, then hand over
   the port and owned desktop proxy. Fixture tests run on PowerShell 5.1 and 7.
   Legacy URL `/install/windows.ps1` routes to `install.ps1`.
4. Done. The end-to-end test installs through `install.ps1`.
5. Done. Remove the PowerShell module, its libraries, the install and uninstall scripts,
   the legacy template, and obsolete tests; rewrite README_WIN.md; stop packing
   the Windows archive.

Done when a Windows runner installs, starts, sets the proxy and uninstalls, and the
hosted installer works in Windows PowerShell 5.1.

### M3: Configs in code, one server process

- Build the server configs and the remaining client profiles in code, with the
  functions M1a added for the desktop config. Remove `config/templates/`.
- Run one sing-box process with all inbounds, from one config and one unit.
- `sbm release` runs `sing-box check` on every rendered config. CI downloads the
  pinned sing-box binary, so the real-binary tests in `tests/test_sing_box_114.py`
  always run.
- Merge `settings.py` and `config_loader.py` into one pydantic model.

Removes the JSON templates, three per-protocol unit files, the path properties in
`settings.py`, and `docker/` with its doc.

The repository implementation is complete: `release/profiles.py` builds the remaining
profiles and one server config; release validates every rendered profile; CI fetches
both pinned binaries; `Settings` validates settings and inventory together. Deploy
installs one `sing-box.service`, checks the node's real TLS files before stopping
services, and restores the previous files and service state on installation failure.
The old protocol units, JSON templates and experimental containers are removed.
Naive is omitted when its enabled-user roster is empty. Default installs use all
packaged inbounds; an explicitly selected inbound must exist in the package.

The single counter listener uses protocol-qualified internal labels for Trojan and
Hysteria2, while Naive retains its wire-visible username. The single connection
stream is partitioned by inbound, so existing per-protocol traffic history remains
usable. Old listener keys are accepted for one release, using the Trojan addresses;
new inventories use `api_listen` and `connection_api_listen`.

Done when rendered configs pass `sing-box check`, a golden-output test covers each
profile, and the VPS runs one sing-box process.

The separately authorized live migration is complete: the VPS runs one
`sing-box.service`, the three legacy protocol units are inactive, and the portal
and both traffic collectors are healthy. An existing subscription refreshed
successfully, and every protocol available to that client passed connection tests.

### M4: Control plane and subscriptions

- Add a Django project with Postgres on the current VPS behind nginx. The SOPS
  inventory stays the source of truth for users. `sbm deploy` imports it into the
  control plane, with the subscription counters, so links keep working.
- The control plane takes over `/sub/<token>` from the portal, renders with the M3
  code, and serves the mirrored binaries and the signed manifest.
- The user page at `/sub/<token>/` shows the subscription link, a QR code for the
  mobile apps and install commands. Admins use the Django admin.
- Android uses a remote profile instead of the zip.
- Usage stays on the old portal until M5.

Removes per-user archives, the release store, manifest, packing and `sbm gc`, the
download routes, and the hosted installer templates.

Done when every current user runs from a subscription URL.

### M5: Node agent and traffic

- Add `cmd/sbm-node` to the Go module. The agent fetches its config, runs
  `sing-box check`, writes the config and reloads sing-box. It reports per-user
  traffic from the API stream with sequence numbers and keeps reports on disk while
  the control plane is unreachable.
- The database becomes the source of truth for users. SOPS keeps only
  infrastructure secrets.
- Traffic lands in `traffic_daily`. The user page, `proxy status` and the
  `subscription-userinfo` header read it. Migrate history from the SQLite database.
- Before dropping the V2Ray counters, compare them with the stream. The live
  database's `__unattributed__` rows measure the gap between the two.
- `sbm deploy` ships the control plane and the agent. Code and binaries on the VPS
  are root-owned.

Removes the V2Ray poller, the custom sing-box build and its Go toolchain use, the
reconciliation tables, the SQLite store, the stats units, the FastAPI portal with its
templates, CSS and fonts, portal passwords, machine tokens and `POST /api/sub`, the
server install and uninstall scripts with `scripts/lib/ui.sh`, and most of
`deploy.py` and `provision.py`.

Done when the VPS runs only sing-box, the agent and the control plane, and usage
matches the old counters within the accepted gap.

### M6: Multiple nodes

- Enroll a node with one command and a one-time token. A provisioning script sets up
  the agent, the hardened units and the firewall, and reads the real sshd port.
- Move the control plane to its own host behind a CDN, on a domain unrelated to
  node IPs.
- Add node groups. Subscriptions list every node in the user's groups, with an
  auto-select group for normal traffic and a fixed node for the `ai` route.
- Track each node's traffic against its provider allowance and reset day. KiwiVM
  becomes one optional provider adapter.
- Decide on VLESS-Reality for nodes without a domain.

Done when a second VPS serves users and a blocked node can be replaced without
touching clients.

### M7: Invites, plans and quota enforcement

- Registration by invite code, with email verification, login and password reset.
- Free plans with a traffic quota, period, node groups and device limit.
- A user over quota or past expiry drops out of node configs at the next reload.
- Stop recording per-destination traffic for users who aren't admins.
- Reject SMTP port 25 and BitTorrent on nodes. Add rate limits and captcha to
  registration.

Done when an invited stranger can register, use the service and hit their quota
without operator action.

### M8: Paid plans

Add prices, orders and a payment provider. Decide whether registration opens without
invites. Details are planned when M7 lands.

## Working rules

- Each milestone lands in small pull requests, and the service keeps working between
  them.
- Delete replaced code in the same milestone. Compatibility code lives for at most
  one release.
- Update the docs a milestone touches, and delete docs for removed parts.
- Test behavior. Real sing-box binaries run in CI, and tests don't assert source
  text.

## Open questions

| Question                                 | Decide by |
| ---------------------------------------- | --------- |
| Control plane host and CDN provider      | M6        |
| VLESS-Reality for nodes without a domain | M6        |
| Payment provider                         | M8        |
