# Refactoring spec

Checked on 2026-10-15.

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
| The control plane uses Django and Postgres                                                        | Decided                                    |
| The client CLI, `sbc`, uses Go, Cobra and pflag, with cgo disabled                                | Decided                                    |
| The node agent is written in Go                                                                   | Proposed                                   |
| Each enrolled installation gets its own subscription token and proxy credentials from M4b         | Decided                                    |
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

| Table           | Holds                                                                          |
| --------------- | ------------------------------------------------------------------------------ |
| `users`         | Name, email, status, admin flag, inviter                                       |
| `invites`       | Code, creator, uses left, expiry                                               |
| `devices`       | User, installation name, revocation state, subscription reset counter          |
| `user_policies` | User, allowance, accounting period, expiry, node groups, enrolled-device limit |
| `credentials`   | Device, protocol and secret                                                    |
| `plans`         | Traffic quota, period, node groups, device limit, and a price from M8          |
| `subscriptions` | User, plan, start, expiry, reset day                                           |
| `nodes`         | Name, region, host, protocols and ports, group, capacity, agent token hash     |
| `traffic_daily` | User, node, day, upload bytes, download bytes                                  |
| `node_reports`  | Node and sequence number, so a repeated report changes nothing                 |

Each enrolled installation gets distinct proxy credentials. One secret may serve
its allowed protocols. Enrollment consumes a device slot in one transaction.
The limit counts enrolled installations, not physical devices. A copied profile
uses the same identity. The first live cutover issues fresh subscription links under
independent signing state and rejects old links. The later device rollout must map
shared access explicitly and preserve its valid links without restoring revoked ones.

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

Route strategies are `direct`, `ai-only`, `gfw-proxy`, `china-direct` and `global`.
`ai`, `gfw` and `china` remain aliases for saved selections and existing commands.
The default is `china-direct`. In M6, proxied AI traffic uses one fixed node to
avoid automatic egress changes. This does not guarantee provider acceptance.

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
- After the M4b ownership cutover, SOPS holds infrastructure secrets, not proxy-user records.
  The foundation uses local database peer authentication without a database password.

## Knowledge to keep

These rules came from real failures. Carry each one into the code that replaces its
current location.

| Area               | Rule                                                                                                                                                                                                                                                                                                                                                | Code or requirement                                                         |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| SSH                | One ControlMaster connection, retries only on known reset messages, `-T -o RequestTTY=no` for rsync, control path under 90 characters without spaces                                                                                                                                                                                                | `sing_box_manager/ssh.py`                                                   |
| Hysteria2 server   | Port hopping uses a nat PREROUTING REDIRECT of the range to one port, for IPv4 and IPv6. UDP buffers are 16 MiB                                                                                                                                                                                                                                     | `provision.py`, `scripts/server-install.sh`                                 |
| TLS                | Use an ECDSA or RSA certificate, not Ed25519, for Chrome QUIC parroting. Restart sing-box after renewal                                                                                                                                                                                                                                             | `docs/SECRET_HANDLING.md`, `provision.py`                                   |
| Client configs     | Ship rule snapshots through `initial_path` so the first start works when rule downloads fail                                                                                                                                                                                                                                                        | `release/rule_sets.py`, `release/renderers.py`                              |
| Client configs     | `sing-box check` does not prove DNS works. Check a page request through the new proxy before accepting installation                                                                                                                                                                                                                                 | `internal/cli/cli.go`, `internal/api/api.go`                                |
| `ai-only` route    | Direct DNS uses the systemd-resolved stub because the local transport needs CAP_NET_RAW. Use community Anthropic and AI sets for routing and DNS. Shared sign-in and captcha hosts depend on community coverage                                                                                                                                     | `release/renderers.py`, `desktop_config.py`                                 |
| Client runtime     | Mixed mode and TUN are never active together, or traffic loops. Check TCP and UDP when picking the mixed port. Run TUN with `-c`, not `-C`                                                                                                                                                                                                          | `internal/singbox/singbox.go`; deferred M1b requirements                    |
| System proxy       | Revert only settings that point at our port. A PAC setting belongs to someone else                                                                                                                                                                                                                                                                  | `internal/desktop/`, `internal/winsettings/`                                |
| API stream         | `interval` is in nanoseconds. Each subscription replays live connections, so deduplicate by connection ID with byte checkpoints. UPDATE carries deltas and CLOSED carries final totals. Events without a user are DNS lookups and health checks. sing-box drops events while nobody subscribes                                                      | `connection_stats.py`, `connection_tracking.py`                             |
| API service        | Disable its dashboard. Strip IPv6 brackets from the listen address                                                                                                                                                                                                                                                                                  | `release/renderers.py`, `settings.py`                                       |
| Billing            | Per-user counters see one leg of relayed traffic; a host that bills both directions charges about twice that. A reset day past a short month's end moves to its last day                                                                                                                                                                            | `settings.py`, `traffic_models.py`                                          |
| Windows            | Launch the hosted installer with `& ([scriptblock]::Create((irm URL)))`. Use an S4U task with `ExecutionTimeLimit=PT0S`, limited run level and an Interactive fallback. Refuse to run elevated. Set WinINet as one `host:port`, separate bypass entries with `;`, refresh with InternetSetOption 39 then 95. Stop the task before replacing the exe | `internal/service/`, `internal/winsettings/`, `web/client/install.ps1`      |
| macOS              | Run `launchctl bootout` before `bootstrap` and poll after start. Clear the quarantine attribute. Read the login shell with `dscl`. Pick the `networksetup` service from the default-route interface                                                                                                                                                 | `internal/service/`, `internal/desktop/`; login-shell detection requirement |
| Linux TUN          | Keep the hardened root unit, the systemd-run capability probe, and admin tools resolved from sbin                                                                                                                                                                                                                                                   | Deferred M1b requirements                                                   |
| Downloads in China | No CDN-hosted page assets. PowerShell `irm` needs `text/plain`. Send auth headers up front, since PowerShell 5.1 and wget wait for a challenge otherwise                                                                                                                                                                                            | `web/routes.py`, `web/client/install.ps1`                                   |

## Milestones

| Milestone | Name                                       | Status      |
| --------- | ------------------------------------------ | ----------- |
| M0        | Security fixes                             | Done        |
| M1a       | Client CLI for Linux and macOS, mixed mode | Done        |
| M1b       | Linux TUN in the client CLI                | Deferred    |
| M2        | Client CLI for Windows                     | Done        |
| M3        | Configs in code, one server process        | Done        |
| M4b       | Control plane, devices and subscriptions   | In progress |
| M5        | Node agent, traffic and budget enforcement | Planned     |
| M6        | Multiple nodes                             | Planned     |
| M7        | Invitation self-service and reusable plans | Planned     |
| M8        | Paid plans                                 | Planned     |

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
| Client code  | `setup.sh` and four libraries, about 8,500 lines, sourced into every shell   | A Go binary with a Cobra command layer, and one `sbc init` line in the rc file                           |
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
  protocols, and route and DNS rules match `clash_mode` values `direct`, `ai-only`,
  `gfw-proxy`, `china-direct` and `global`. Alias modes retain saved `ai`, `gfw` and
  `china` selections. Every rendered config passes `sing-box check` in CI.
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
  archives stay until the M4b migration.

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
  port and secret, and `sbc route` and `sbc protocol` call it. sing-box clears its DNS cache when
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
- Cobra and pflag define commands, flags, validation, help and completion.
  Existing valid syntax stays supported, including `sbc speed --download [MB]`.
  `sbc <command> --help` and `sbc help <command>` show localized help without
  performing the command's action. Unknown flags and extra arguments fail.
  `sbc completion bash|zsh|powershell` prints completion code. Bash and Zsh
  load it through `sbc init` in interactive shells. PowerShell users add a
  profile command themselves. Route and protocol choices come from the local
  config without a network request or running service. See [the CLI guide](README_SBC.md).
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
- `sbc ip` asks ipinfo.io and cip.cc through the active route. `--ai` adds
  unauthenticated Gemini, ChatGPT and Claude checks through the proxy-only
  diagnostic inlet. It skips `direct` unless `--allow-direct-ai` is supplied.
  That override uses the active route and permits exposure of the direct IP.
  Checks follow no redirects and use no account credentials or automatic retries.
  Gemini's undocumented page supplies a region code. ChatGPT's edge trace can
  supply an IP and country. Claude reports public-page reachability, denial,
  challenge or redirect, not a region. None proves service or account eligibility.
  See [the CLI guide](README_SBC.md#exit-ip-and-ai-checks).
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
  resets the four Firefox WebRTC profile preferences regardless of origin and
  disables automatic setup.
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
- The legacy Unix client implementation, its tests, and its full guides are removed.
  `README_SBC.md` is the client guide. Hosted migration uses the installed client's
  own uninstaller and desktop library. Small fixtures cover that interface.
- Legacy Linux TUN state blocks installation before downloads or file changes,
  even when user-scoped client files are missing. The installed client's cleanup
  tools must remove that state first. No current invited user uses Linux TUN.

Done when CI on Linux and macOS runners installs from a test subscription, starts,
switches route and protocol without a restart, rolls back a bad refresh and
uninstalls, and when a bash install without TUN migrates.

These follow M1a as separate pull requests: `doctor`, a redacted report for support
requests; `lan on|off`, which listens on all interfaces with a
generated username and password; and the Docker container proxy, which listens on
the Docker bridge address with auth.

#### M1b: Linux TUN

Deferred. `sbc` supports mixed mode, not Linux TUN. The legacy implementation is
not maintained, and no current invited user uses Linux TUN. Recheck the scope before
starting this milestone.

Requirements for a new implementation:

- Add `sbc tun on|off` for native Linux. Keep root-owned binaries and configs,
  a hardened unit, a dedicated runtime account, and the capability probe.
- Mixed mode and TUN must never run together. Load one exact config with `-c`.
- Check interface capture, routing, DNS, and egress before accepting activation.
- Release the operation lock when `sbc` exits, including after Ctrl-C.
- Keep legacy TUN refusal in the hosted installer until a safe migration exists.

Done when a disposable Linux runner checks activation, deactivation, failed
activation recovery, and interrupted-operation cleanup. Keep `scripts/lib/ui.sh`
for the server scripts until M5.

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
   Firefox cleanup resets the four supported preferences in both `prefs.js` and
   `user.js`, including orphaned values and pre-existing user choices. It deletes
   obsolete snapshots without restoring them, refuses active profiles that need
   changes, and keeps cleanup available for retry. Unrelated preferences and
   administrator policies remain untouched. The Windows guide gives manual reset
   steps only for policies outside this scope. Fixture tests cover permission failures,
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
- Use one strict Pydantic model for settings and inventory, without a compatibility loader.

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

### M4b: Control plane, devices and subscriptions

**Next delivery: basic live migration of existing users on the single VPS.**
The operator manages access through Django admin. PostgreSQL owns the accounts.
Existing clients connect and refresh subscriptions. A checked non-serving copy is
an intermediate checkpoint, not the delivery target. Device enrollment follows
this delivery and does not block account ownership.

Build from current main. Reuse Django/PostgreSQL, the importer checks, config
builders, signed-download contract, and hosted installers. Use the private admin
as the initial management interface. Use a supervised config-apply step for the
single node before building a general node agent. Keep the service working between
these incremental PRs:

1. **Deployable foundation.** Implemented and deployed on the single VPS.
   Opt-in Django/PostgreSQL runs beside the existing portal. Private admin access
   uses an SSH tunnel. Deploy provisions the database, applies migrations, collects
   root-owned static files, and checks readiness. Repeat operations retain data.
   Django accounts are operators only. This deployment imports no proxy users
   or credentials.
   See [the foundation guide](CONTROL_PLANE.md).
2. **Checked real-data copy.** Import accounts and credentials idempotently without
   activating subscription links. Inventory remains authoritative during this checkpoint.
   Check every copied field and rendered config. The fixture-only
   importer remains separate. The [account-copy path](CONTROL_PLANE_ACCOUNT_COPY.md)
   implements export/admission/preview/apply locally. It passed independent source
   review and synthetic tests. It is not deployed. Use the [one-time plan](CONTROL_PLANE_CAPTURE.md),
   not the deferred capture protocol. Do not require enrolled devices for this copy.
3. **Basic live serving and account ownership.** Django serves subscriptions and
   protected signed downloads using the existing renderers. Preserve authentication,
   ETags, version negotiation, and download path checks. Add basic proxy-account
   management for operators through private Django admin. PostgreSQL becomes authoritative for
   proxy accounts and credentials. Prevent inventory/deploy/import from overwriting
   database-managed access after cutover. SOPS retains infrastructure secrets.
   Account changes reach sing-box through a supervised, validated config-apply step.
   This serving/management path is not implemented. Copy success alone permits no
   handover. Reconcile fresh source state before the separately approved cutover.
4. **Device management, after basic live migration.** Add account-authenticated
   enrollment, revocation, transactional slot checks, and per-user policies. Define
   how shared legacy access moves to enrolled installations. Add mobile QR/profile
   conveniences where needed. PostgreSQL retains account ownership throughout.

**Defer from basic live migration:** traffic dashboards and history migration,
quotas and usage-based suspension, device enrollment/limits, the general node agent,
multi-node support, invitations, payments, and the capture protocol prototype.
Preserve existing data and backups. Existing traffic collectors may remain unchanged
or be disabled during a separately approved cutover. Do not migrate traffic history
merely to complete account migration. Do not present missing usage as measured zero.
The selected policy issues fresh links under independent signing state at cutover.
Preserve proxy credentials and portal verifiers. Update every installation's saved
URL through a private authenticated handoff. Reject old links on subscriptions and
protected downloads, including any retained legacy routes. Missing old reset history
does not block account copying. Never manufacture verified legacy zero counters.
Durable reset/revocation of new links remains required, not optional traffic data.

Before cutover, test how account changes reach the current node without the M5 agent.
A database change alone is not applied proxy access. Show pending/failed application
rather than claiming success. Define and check revocation of new and existing
connections, including any required restart. Validate configs before application,
retain recovery artifacts, and test client connectivity. Keep Django operator
permissions separate from imported portal flags. Subscription tokens cannot authorize
account management. Fresh-link policy approval is not authorization to rotate secrets
now. Preserve current access until an approved cutover. Test the operator's connection
and each current client after updating saved URLs. Account-password or proxy-credential
rotation is outside this policy.

**Basic live migration is done when:** real accounts are verified in PostgreSQL,
Django serves their subscriptions, operator access changes take effect on sing-box,
and inventory can no longer overwrite those accounts. Backups, recovery, valid/revoked
access, signed downloads, and current-client connectivity must pass the cutover checks.
Devices, traffic accounting, and quotas are not completion conditions.

After all installations migrate, remove per-user archives, the archive store and
its manifest, archive packing, `sbm gc`, and replaced download routes/templates.
Keep the signed binary/rule-set manifest and working hosted installers.

The later device checkpoint is done when every current installation has a device
subscription, enrollment limits and revocation work, and the database owns device
policy. It does not delay the basic live migration above.

#### M4b-2: safe-copy contract

Status: fixture-only importer implemented and tested locally, not deployed.
The active [one-time first-copy plan](CONTROL_PLANE_CAPTURE.md) targets stored account
state, followed by fresh subscription links at cutover. It does not prove current
loaded-process state. The [account-copy implementation](CONTROL_PLANE_ACCOUNT_COPY.md)
reuses the fixture importer's account validation, projections, and field/render checks.
Its independent admission migration depends on the existing account tables, not the
capture prototype. Local synthetic checks do not authorize live import. Never enable the operational fixture marker or invent fixture evidence.
The existing fixture importer still tests legacy-token preservation. Its format,
legacy records, and counter tests do not define the fresh-link production path.

The capture protocol prototype is deferred and is not part of this implementation.
Preserve any separately retained prototype code, schema, and tests.
Do not build that machinery as a prerequisite for the first real copy. Live operations
need separate approval. See [the fixture importer guide](CONTROL_PLANE_IMPORT.md).

First copy existing access records into PostgreSQL. Check the copy before using it.
Inventory and the existing portal remain authoritative throughout this stage.
The copy must not serve clients, configure sing-box, or grant live access.

Build from current main. Reuse the Django foundation, config builders, token
derivation, and signed-download checks. Do not resume the older control-plane draft.

**Retained fixture import records**

| Record          | Purpose                                                                                                |
| --------------- | ------------------------------------------------------------------------------------------------------ |
| Import source   | Stable namespace, authoritative source binding, accepted capture sequence, and imported-state revision |
| Proxy subject   | Stable ID and an exact, case-sensitive inventory username within that namespace                        |
| Imported access | Portal verifier and flags, protocol credentials and flags, and membership presence                     |
| Legacy access   | Original token username, reset counter, secret identity, and source evidence; no enrolled device       |
| Import run      | Bundle digest, source sequence, reviewed database revision, result counts, and retry receipt           |

Django operators remain separate. A portal admin flag must not grant Django staff
or superuser permissions. Do not infer a person's identity, email, or devices from
an inventory username. Only the importer may change copied records before cutover.
Admin views may show redacted metadata, not usable credentials or verifiers.

**One selected stored-source snapshot**

The operator approves the source and its private artifacts. Bind the exact input
bytes, stable source/namespace, capture time/window, rendering context, fresh-link
policy, and destination. Copy every account record and flag, including disabled
records. Activate no links. Record missing old counters as unavailable, not verified
zeros or usable legacy-token records. Retain available history privately. If counter
artifacts are captured, validate a consistent SQLite snapshot with WAL state.

Pause operator configuration edits during extraction. Old link resets may continue:
the copy serves no clients and the cutover rejects the entire old link family.
No portal/proxy restart or loaded-process attestation is required for the first copy.

Private operator custody and explicit artifact selection supply trust for this
one-time procedure. Checksums bind bytes, not authenticity alone. An empty database,
new timestamp, changed filename, or new source label does not authorize an old
snapshot. Retain source ordering and destination conflict checks, but do not claim
the deferred protocol's automated rollback-proof freshness.

The copy proves selected snapshot parity, not loaded-state equality or complete
lifetime history. Record unknowns and stop on known identity ambiguity. The fresh-link
policy removes old reset history as a prerequisite, but does not permit lowering
retained counters. Keep the old subscription secret unchanged until cutover. New
links use independent signing state, not a Django session key or proxy password.

Before M4b-3 serving, reconcile fresh account/credential state, deliver new links, and
test old-link rejection. The shadow copy alone permits no serving switch or silent
activation of pending disk configuration.

**Counters and identity lifecycle**

Legacy-counter rules below remain requirements for the fixture importer and any
future link-preserving import. The selected production path does not preserve old
links. It must neither fabricate legacy state nor bypass these rules by relabeling
input. Account identity rules apply to both paths.

- Match the namespace and exact validated username. Preserve IDs across accepted
  updates. Do not add case folding or infer renames from credentials.
- Mark missing subjects and removed access memberships absent/inactive in the copy.
  Retain subject IDs and counter history. Disabled membership and absent membership
  are distinct states. Do not generate credentials or silently reactivate records.
- The source has no immutable person IDs. Copy exact username bindings without
  inferring people. Record unknown history and stop on known ambiguity. Do not
  claim to detect undeclared reassignment from names or changed credentials.
- The first implementation must reject declared rename/reassignment mappings and
  known retired-name reappearance. Define explicit continuity approval before
  supporting these cases. A new unseen username creates a new proxy subject,
  not proof that an existing person changed their name.
- Keep the legacy token username separate from the subject ID. Future identity
  mappings must not silently change token inputs or transfer access to another person.
- Keep counter-only tombstones for usernames absent from inventory. They retain
  revocation history but create no proxy subject or access entitlement.
- Never lower a stored counter. Validate unique usernames and nonnegative integer
  counters. A missing row in a validated complete table can mean zero only when
  no retained counter contradicts it. Do not use the permissive portal reader as
  the import validator.
- Missing, unreadable, incomplete, or corrupt counters block link-preserving import.
  The selected fresh-link account copy records unavailable legacy state instead.
  Never silently assume initial zero. A restore or new source label must not erase
  an existing destination counter boundary.

Keep old shared access separate from enrolled installations. Preserve credentials
and counters without inventing device identities. Legacy access consumes no device
slots. Strict device limits require its retirement. Set that policy before cutover.

**Approved preview/apply workflow**

The workflow and command interface are implemented for synthetic fixtures only.
Every `sbm` operation must select its config explicitly.

1. **Preview** validates an immutable bundle against one consistent database view.
   It writes no imported records or source revision. Produce a protected report
   with additions, changes, removals, conflicts, and comparison results.
2. Bind the report to the bundle digest, namespace, source sequence, rendering
   context, destination database identity, and expected imported-state revision.
   Review that exact report.
   A report is not itself operator authorization to apply.
3. **Apply** requires the reviewed bundle and report. Read and validate the exact
   bytes used for application. A changed bundle or context requires a new preview.
4. Under a per-source lock, recheck the expected imported-state revision and source
   sequence. Reject an invalidated preview or an older capture, even when counters
   are equal. Equal sequences with different bundle digests are conflicts.
5. Validate the full change, then commit records, source head, revision, and receipt
   in one transaction. Any failure leaves all imported state unchanged.
6. An exact retry after a lost response returns the committed receipt without
   applying again. A superseded receipt does not authorize replay of its snapshot.
   Reapplying the latest identical bundle is a no-op. That successful no-op stores
   a report-bound receipt without advancing the imported revision.
   A newer verified capture may advance the source head even when its access fields
   are unchanged.

For example, capture A enables a user. Newer capture B disables that user without
changing the counter. After B, applying A must fail rather than restore enabled access.
The revision covers imported state for this source, not operator logins or sessions.

Use synthetic bundles in development. Store production bundles and reports outside
public source, with private directories (`0700`) and files (`0600`). PostgreSQL does
not automatically encrypt usable credentials. Reports, logs, errors, and admin
pages must not disclose credentials, verifiers, tokens, or full subscription URLs.
General logs may contain counts and generic error codes, not source records.

**Approved command interface**

Fixture-only commands. `CONFIG`, `SNAPSHOT`, and `REPORT` are private file-path
placeholders. Quote paths that contain spaces. Do not use production inputs.
The fixture target marker defaults to disabled and has no CLI enable option.

```sh
pixi run sbm --config CONFIG control-plane-import \
  --action preview --snapshot SNAPSHOT --report REPORT

pixi run sbm --config CONFIG control-plane-import \
  --action apply --snapshot SNAPSHOT --approved-report REPORT
```

- `--action` and `--snapshot` are required. There is no default action.
- Preview requires `--report` as an output path and forbids `--approved-report`.
  Apply requires `--approved-report` as an input path and forbids `--report`.
  Reject invalid combinations before opening the database.
- `--config`, or exported `SBM_CONFIG`, selects the local destination database and
  protected rendering inputs. Access records come only from `--snapshot`, not
  from that config's user lists or a generated authentication snapshot.
- Bind the report to the configured and connected destination database, not just
  a snapshot-supplied label. Another database or changed rendering inputs requires
  another preview. Read snapshot/report inputs as protected regular files.
- Publish preview reports atomically, with private permissions and no overwrite.
  A rejected preview may write redacted conflict details, but Apply accepts only
  a successful, matching reviewed report. Apply stores its receipt in the database.
- Help needs no config and performs no file or database operations. Neither action
  runs migrations, captures live state, contacts a host, or restarts a service.
  Provide no force, stale-check bypass, or automatic-approval option.
- Exit `0` means a successful preview, committed apply, or confirmed no-op. Exit `2`
  means invalid arguments, validation failure, stale inputs, or a binding conflict.
  Exit `1` means an operational failure. Errors, including config-load errors,
  must be redacted. An uncertain commit requires an exact retry to resolve it.

**Preserve each access case**

For valid requests with subscriptions configured, retain the current distinctions:

| Operation                   | Current requirement                                                          |
| --------------------------- | ---------------------------------------------------------------------------- |
| Portal login                | Enabled portal account and valid portal credentials                          |
| Config retrieval            | Current token for an enabled portal account, plus an enabled protocol        |
| Link display/exchange/reset | Authenticated enabled portal account, plus an enabled protocol               |
| Signed client download      | Current token for an enabled portal account; no protocol membership required |
| Direct proxy access         | Enabled credential in that protocol; no portal account required              |

A portal-only user gets no config or displayed link, but an already-known current
token can still fetch manifest-listed files. A protocol-only user keeps direct
proxy access but gains no portal account. Import must not tighten or expand these
rules. A subscription token alone must not authorize link or device management.

**Acceptance checks for the implementation**

- Operator-selected private inputs reject mixed stored configuration, unapproved
  artifacts, and policy/context/target mismatches. Record snapshot limits, not a
  loaded-state claim. A fresh-link account copy permits unavailable old counters,
  but must not create working legacy access or claim verified zero history.
- Identical retries preserve IDs and counts. Different-capture races, invalidated
  previews, equal-counter flag changes, and lost responses cannot replay old state.
- A validation or commit failure changes no imported records, source head, or receipt.
- Case-sensitive identities, removals, disabled memberships, counter-only tombstones,
  missing continuity evidence, and rejected rename/reuse/reappearance cases follow
  the lifecycle rules.
- Every copied field round-trips, including disabled credentials, portal verifiers,
  portal admin flags, enabled flags, and membership presence. Removed memberships
  remain inactive. Compare the complete expected change with database-loaded rows,
  including retained payloads and existing IDs. Post-copy corruption must roll back.
  Reject PostgreSQL-unrepresentable access strings before a ready preview.
  Operators and enrolled-device counts stay unchanged.
- Render the comparison side from database-loaded user data only. Use the same
  frozen infrastructure context for both sides. Deliberately mutate database fields
  and require field or render checks to fail. Do not compare inventory with itself.
- The fixture importer retains token parity and counter-boundary tests. The fresh-link
  production copy checks account/config parity and activates no links. Its later
  serving checks must accept new links, reject old links everywhere, and preserve
  durable new-link revocation. Synthetic HTTP checks retain every access case above,
  ETags, versions, signed downloads, and path/authentication rejection. Live routes
  do not change during the copy. Serving handover needs fresh client checks.
- Redaction checks cover stdout, stderr, validation errors, reports, and admin views.
  The existing portal, proxy configuration, and Django operators remain unchanged.

**First implementation and remaining gates**

The local implementation adds isolated import records, strict fixture validation,
the approved command interface, transactional preview/apply logic, and comparisons
from database-loaded records. Tests use synthetic snapshots and disposable
PostgreSQL only. Snapshot assertions are not production provenance.
The fixture importer does not inspect real SQLite backups or deployed manifest files.
The separate account-copy command exports stored account inputs and applies them
through its own admission. It does not read counters, activate links, or implement
the deferred capture protocol. It passed independent source review and synthetic tests.
It is not deployed.

The active [one-time first-copy plan](CONTROL_PLANE_CAPTURE.md) starts with actual
source preflight, then a small production admission/extraction implementation,
private backups, preview, and an explicitly approved non-serving apply. Preflight,
support installation, capture, and import each need live-operation scope. No such
operation follows from plan approval.

The capture protocol prototype remains deferred and outside this implementation.
Its witness, source journal, RPC, writer gates, and launch attestation are not the
next milestone. Retain the code and tests without expanding or deploying them.
Use the checked copy as a checkpoint toward basic live serving and account ownership.
Serving still needs fresh reconciliation and separate approval. Devices remain later.
Rename/reuse support needs its own contract.

No subscription-route replacement, mobile page, device enrollment, credential
rotation, ownership cutover, traffic migration, node agent, payments, or multiple
nodes belongs here. Remove no legacy artifacts, services, releases, or worktrees.
Release and deploy must not run an import automatically. Commit/PR shipping, a live
import, and any deployment or ownership cutover require separate authorization.

### M5: Node agent, traffic and budget enforcement

- Add `cmd/sbm-node` to the Go module. The agent fetches its config, runs
  `sing-box check`, writes the config and reloads sing-box. It reports per-user
  traffic from the API stream with sequence numbers and keeps reports on disk while
  the control plane is unreachable.
- Read users, devices, and policies from the database after the M4b ownership cutover.
  Suspend over-budget or expired users across their devices. Nodes must stop existing
  sessions as well as reject new ones. Reevaluate access after a reset or allowance change.
  Measure enforcement delay and interruption before rollout.
- Traffic lands in `traffic_daily`. The user page, `sbc status` and the
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

Done when the VPS runs sing-box, the agent, and the control plane without the old
portal or collectors. Usage must match the old counters within an accepted gap.
Fixture checks must cover report replay, outages, suspension of existing sessions,
and resumption after reset. Define report intervals and the long-outage policy
before rollout.

### M6: Multiple nodes

- Enroll a node with one command and a one-time token. A provisioning script sets up
  the agent, the hardened units and the firewall, and reads the real sshd port.
- Move the control plane to its own host behind a CDN, on a domain unrelated to
  node IPs.
- Add node groups. Subscriptions list every node in the user's groups, with an
  auto-select group for normal traffic and a fixed node for proxied AI traffic.
- Track each node's traffic against its provider allowance and reset day. KiwiVM
  becomes one optional provider adapter.
- Decide on VLESS-Reality for nodes without a domain.

Done when a second VPS serves users and a blocked node can be replaced without
touching clients.

### M7: Invitation self-service and reusable plans

- Registration by invite code, with email verification, login and password reset.
- Add reusable free plans when shared per-user policies justify them.
- Expose device enrollment, revocation, and quota information to authenticated users.
  Use the device limits from M4b and enforcement from M5.
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
