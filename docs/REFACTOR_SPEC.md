# Refactoring spec

Checked on 2026-10-19.

This spec sets the target architecture for sing-box-manager and the milestones that
move the repo there. Milestones preserve current access until the planned
[clean break](#clean-break-and-legacy-removal). That cutover requires users to enroll
again and rejects old credentials and links. It needs separate production authorization.
When a milestone lands, update its status and affected docs. Review its scope before implementation.

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
| Build a minimal Django control plane instead of adopting an existing panel                        | Decided                                    |
| Operators create free accounts and issue single-use device enrollment codes                       | Decided                                    |
| Ordinary users have no login password, public registration, or password-recovery flow             | Decided                                    |
| sing-box is the only proxy core on nodes and clients                                              | Decided                                    |
| Security fixes come first, then the client CLI                                                    | Decided                                    |
| The control plane uses Django and Postgres                                                        | Decided                                    |
| The client CLI, `sbc`, uses Go, Cobra and pflag, with cgo disabled                                | Decided                                    |
| Add a node agent, with Go as the proposed language                                                | Planned after device management            |
| Give each enrolled installation its own subscription token and proxy credentials                  | Planned after Linux TUN                    |
| Support multiple nodes before invalidating all legacy user credentials and links                  | Decided                                    |
| A client runs one config per installation, with routes as Clash modes and protocols in a selector | Decided                                    |
| Configs are built in code and pass `sing-box check` before release                                | Implemented                                |
| Nodes run the official sing-box build                                                             | Proposed                                   |
| Per-user traffic comes from the sing-box API stream only                                          | Deferred; requires accounting parity       |
| Nodes apply user changes by reloading sing-box in batches                                         | Proposed, revisit if reloads disturb users |
| Protocols stay trojan, hysteria2 and naive; VLESS-Reality is an option for nodes without a domain | Open until M6                              |

sing-box 1.14.0 supports VLESS-Reality as both server and client, and `sing-box check`
accepts a server config and a client config that use it.

## Target architecture

The current system is one Django/PostgreSQL control plane and one sing-box node,
with systemd-triggered config sync and SQLite traffic history.
Linux TUN, device management, a node agent, and multiple nodes are the planned scope.
The diagram describes that target, not deployed functionality. Detailed schemas and
interfaces need review in their implementation PRs. Quotas, public registration,
plans, and payments remain outside this delivery sequence.

```text
browsers, client apps, sbc
        │ HTTPS
        ▼
control plane (Django + Postgres)
accounts, devices, nodes, traffic, subscriptions, private admin
        ▲
        │ each node pulls its config and pushes traffic over HTTPS
        │
node agent + sing-box on each VPS  ◀── proxy traffic from clients
```

| Component     | Runs on                                 | Built with                          | Job                                                                                                                                                                         |
| ------------- | --------------------------------------- | ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Control plane | Current VPS, hosting/CDN decision in M6 | Django, Postgres                    | Accounts, device enrollment, nodes, credentials, traffic, subscriptions, private admin and read-only user pages                                                             |
| Node agent    | Each VPS                                | Go                                  | Fetches, checks and applies config. Reports applied revision, health and traffic through reviewed interfaces. Keeps the last good config during control-plane outages       |
| sing-box      | Each VPS and client                     | Official or compatible custom build | Proxies traffic                                                                                                                                                             |
| Client CLI    | Linux, macOS, Windows                   | Go (`sbc`)                          | Enrolls with a single-use code, installs signed artifacts, manages the proxy, switches route and protocol, shows usage, updates and uninstalls. Linux TUN. Users type `sbc` |
| Mobile        | Android, iOS                            | Official sing-box apps              | Import the subscription as a remote profile                                                                                                                                 |

### Control plane data

These are logical entities, not a finalized migration schema.

| Entity               | Responsibility                                                                                    |
| -------------------- | ------------------------------------------------------------------------------------------------- |
| Operators (`User`)   | Private Django login and administrative permissions                                               |
| Accounts (`Account`) | Proxy user identity and master enable switch, without a user login password                       |
| Enrollment codes     | Operator-issued permission to enroll one device for one account, with expiry and single-use state |
| Devices              | Account, installation identity, enable switch, and independent subscription state                 |
| Credentials          | Device, protocol, and secret                                                                      |
| Nodes                | Endpoint, supported protocols, and separate agent authentication                                  |
| Traffic              | Account/device/node attribution and retained historical usage                                     |

Operators manage accounts, device enrollment, revocation, replacement codes, and
any enrollment limits through private Admin. Consuming a code and creating its
device must be atomic. A code grants no account-management or cross-device access.
A subscription grants read-only configuration/download access, not enrollment or management rights.
Users need no portal password or password recovery. Django operator authentication remains separate.
An account disable applies to every device. A device disable revokes its subscription
and protocol access. There is no independent portal-only switch in the target model.

Each enrolled installation gets distinct proxy credentials and a subscription.
A copied profile still uses the same device identity. An enrollment limit counts
registered installations, not physical devices. Keep existing traffic history and
account attribution. No PostgreSQL traffic migration is required for this scope.

### Interfaces

The device PR defines the enrollment request and response. It consumes an
operator-issued code and returns a device subscription. Mobile enrollment must
produce a device profile usable by a supported sing-box app.
The subscription paths below remain read-only. Their legacy account tokens expire
at the clean break, even if the URL shape stays the same.

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
- Enrollment codes and subscription tokens stay out of shell history, process arguments and logs.
  The device PR defines token storage and delivery without granting management rights.
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
| Client runtime     | Mixed mode and TUN are never active together, or traffic loops. Check TCP and UDP when picking the mixed port. Run TUN with `-c`, not `-C`                                                                                                                                                                                                          | `internal/singbox/singbox.go`; M1b requirements                             |
| System proxy       | Revert only settings that point at our port. A PAC setting belongs to someone else                                                                                                                                                                                                                                                                  | `internal/desktop/`, `internal/winsettings/`                                |
| API stream         | `interval` is in nanoseconds. Each subscription replays live connections, so deduplicate by connection ID with byte checkpoints. UPDATE carries deltas and CLOSED carries final totals. Events without a user are DNS lookups and health checks. sing-box drops events while nobody subscribes                                                      | `connection_stats.py`, `connection_tracking.py`                             |
| API service        | Disable its dashboard. Strip IPv6 brackets from the listen address                                                                                                                                                                                                                                                                                  | `release/renderers.py`, `settings.py`                                       |
| Billing            | Per-user counters see one leg of relayed traffic; a host that bills both directions charges about twice that. A reset day past a short month's end moves to its last day                                                                                                                                                                            | `settings.py`, `traffic_models.py`                                          |
| Windows            | Launch the hosted installer with `& ([scriptblock]::Create((irm URL)))`. Use an S4U task with `ExecutionTimeLimit=PT0S`, limited run level and an Interactive fallback. Refuse to run elevated. Set WinINet as one `host:port`, separate bypass entries with `;`, refresh with InternetSetOption 39 then 95. Stop the task before replacing the exe | `internal/service/`, `internal/winsettings/`, `web/client/install.ps1`      |
| macOS              | Run `launchctl bootout` before `bootstrap` and poll after start. Clear the quarantine attribute. Read the login shell with `dscl`. Pick the `networksetup` service from the default-route interface                                                                                                                                                 | `internal/service/`, `internal/desktop/`; login-shell detection requirement |
| Linux TUN          | Keep the hardened root unit, the systemd-run capability probe, and admin tools resolved from sbin                                                                                                                                                                                                                                                   | M1b requirements                                                            |
| Downloads in China | No CDN-hosted page assets. PowerShell `irm` needs `text/plain`. Send auth headers up front, since PowerShell 5.1 and wget wait for a challenge otherwise                                                                                                                                                                                            | `control_plane/views.py`, `web/client/install.ps1`                          |

## Milestones

| Milestone | Name                                       | Status                                   |
| --------- | ------------------------------------------ | ---------------------------------------- |
| M0        | Security fixes                             | Done                                     |
| M1a       | Client CLI for Linux and macOS, mixed mode | Done                                     |
| M1b       | Linux TUN in the client CLI                | Next feature                             |
| M2        | Client CLI for Windows                     | Done                                     |
| M3        | Configs in code, one server process        | Done                                     |
| M4b       | Control plane, devices and subscriptions   | Core deployed, devices planned after TUN |
| M5        | Node agent                                 | Planned after device management          |
| M6        | Multiple nodes                             | Planned after the node agent             |
| M7        | Invitation self-service and reusable plans | Deferred pending need                    |
| M8        | Paid plans                                 | Deferred pending need                    |

### Delivery order

1. **PR 1: control-plane reliability.** Merged and deployed as `4f1c115`.
   Isolated PostgreSQL, real sing-box, native systemd, and cross-platform CI passed.
   Production acceptance verified existing subscriptions and a database-only account's
   connectivity, disable/re-enable, and traffic accounting. The test account was removed.
   Protected recovery backups and verified encrypted off-host copies are retained.
2. **PR 2: portal and account-source consolidation.** Merged and deployed as `d4ae81c`.
   One Django service provides subscription pages, private operational views, and
   PostgreSQL-backed legacy-client migration. Public password login, FastAPI, runtime
   authentication snapshots, and new per-user archives are removed. Signed downloads,
   the shared store, SQLite traffic history, and installer compatibility remain.
   Production acceptance passed for existing subscriptions, legacy-client migration,
   signed downloads, private traffic views, and historical data. Measured proxy
   interruption stayed below 60 seconds. The retired Web service is stopped and disabled.
3. **M1b: Linux TUN.** A user now needs native Linux TUN. This is the highest-priority
   feature after those two delivery steps. Define activation, DNS, routing, privilege,
   and recovery behavior before implementation. Do not restore the legacy Bash client.

4. **M4b device management.** Add operator-issued single-use enrollment codes,
   device subscriptions, independent credentials, and revocation. Do not add public user login.
5. **M5 node agent.** Deliver authenticated config distribution, status, and safe
   application of changes. Keep proven collectors until replacements pass acceptance.
6. **M6 multiple nodes.** Validate device access and revocation across nodes.
7. **Clean break.** Issue new device identities and invalidate the entire legacy
   credential/subscription system. Remove its compatibility code and fields.

Quotas, invitation self-service, plans, and payments remain deferred.
Keep the current systemd sync path until the agent replaces it safely.

### Clean break and legacy removal

This is an approved product direction, not authorization to reset production credentials now.
The cutover follows Linux TUN, device management, the node agent, and multi-node acceptance.

- Preserve account records, operator authentication, infrastructure settings, signed
  downloads, and traffic history. Do not reset the database or rewrite proven config builders.
- Provide the new client, enrollment instructions, and operator-issued codes before
  withdrawing old access. Users must enroll again through the new flow.
- Revoke all old protocol credentials and terminate their existing sessions on every node.
  A node without the revocation must not remain an available legacy entry point.
- Invalidate old account subscriptions, portal passwords, and machine tokens together.
  Remove the legacy exchange path so an old token cannot obtain a new subscription.
- Remove `Account.portal_enabled`, `portal_password_hash`, and `is_portal_admin`.
  Use account and device enable switches. Remove account-level subscription state
  once device subscriptions replace it. Keep Django operator passwords and permissions.
- Delete legacy Basic/Bearer authentication, its signing settings, compatibility APIs,
  installer authentication migration, and obsolete inventory account-import code.
  Keep installer checks that prevent conflicts with existing services or legacy TUN state.
- Test that new devices work and that every old credential, token, and subscription fails.
  A recovery procedure must not restore revoked legacy access.

Before cutover, existing users retain access. After cutover, old-client refusal is
intentional, not an outage to repair by restoring compatibility. Infrastructure
interruptions must still stay below 60 seconds. Version checks enforce supported
contracts but do not prove which client binary runs on a user's device.

### M0: Security fixes

Fix the current code. Each item gets a regression test.

| Item | Change                                                                                             | Where                                                    |
| ---- | -------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| S1   | Client mixed inbounds use loopback unless authenticated sharing is enabled                         | `desktop_config.py`                                      |
| S2   | Deploy stops running or installing, as root, files that the portal account can write               | `deploy.py` near line 678, `scripts/server-install.sh`   |
| S3   | Do not allow credentialed cross-origin access                                                      | `control_plane/django_settings.py`                       |
| S4   | Keep the KiwiVM API key out of logs: httpx logs full URLs at INFO, and error text includes the URL | `kiwivm.py`, `web/vps_info.py`                           |
| S5   | Bootstrap allows the real sshd port before enabling ufw, instead of only 22                        | `provision.py` near line 387                             |
| S6   | Downloads reject disabled or removed accounts                                                      | `control_plane/services.py`                              |
| S7   | Bound password-authentication attempts and limiter memory                                          | `control_plane/compat.py`, `web/auth.py`, `provision.py` |
| S8   | Reject legacy machine-token authentication without the configured signing key                      | `control_plane/compat.py`                                |

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
  `web.subscription_secret`. PostgreSQL stores the counters.
  Operators issue and reset links through private Django Admin. Subscription pages
  are read-only; a user who loses the link must request it from an operator.
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
- `sbm release` builds shared client artifacts, not per-user archives.
  Mobile clients use remote profiles or current JSON downloads.

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
  tools must remove that state first. Native Linux TUN support is planned in M1b.

Done when CI on Linux and macOS runners installs from a test subscription, starts,
switches route and protocol without a restart, rolls back a bad refresh and
uninstalls, and when a bash install without TUN migrates.

These follow M1a as separate pull requests: `doctor`, a redacted report for support
requests; `lan on|off`, which listens on all interfaces with a
generated username and password; and the Docker container proxy, which listens on
the Docker bridge address with auth.

#### M1b: Linux TUN

Required after automatic client maintenance and official mobile-app import.
`sbc` supports mixed mode, not Linux TUN. The legacy implementation is not maintained.
Confirm the initial Linux environment and routing requirements before implementation.

Requirements for a new implementation:

- Add `sbc tun on|off` for native Linux. Keep root-owned binaries and configs,
  a hardened unit, a dedicated runtime account, and the capability probe.
- Mixed mode and TUN must never run together. Load one exact config with `-c`.
- Check interface capture, routing, DNS, and egress before accepting activation.
- Release the operation lock when `sbc` exits, including after Ctrl-C.
- Keep legacy TUN refusal in the hosted installer until a safe migration exists.

Done when a disposable Linux runner checks activation, deactivation, failed
activation recovery, and interrupted-operation cleanup. Keep `scripts/lib/ui.sh`
while the server scripts use it.

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
new inventories use `api_listen` and `connection_api_listen`. The server route
resolves hostnames through local DNS first, pins UDP associations with `udp_connect`,
and rejects private and reserved IPv4/IPv6 ranges (including IPv4-mapped IPv6).

Done when rendered configs pass `sing-box check`, a golden-output test covers each
profile, and the VPS runs one sing-box process.

The separately authorized live migration is complete: the VPS runs one
`sing-box.service`, the three legacy protocol units are inactive, and the portal
and both traffic collectors are healthy. An existing subscription refreshed
successfully, and every protocol available to that client passed connection tests.

### M4b: Control plane, devices and subscriptions

Reuse the config builders, signed-download contract, and hosted installers.
The single-node account, credential, and subscription core is deployed, including portal consolidation.
Device management remains unimplemented, so M4b is not complete under its original acceptance criteria.
Items 1–3 describe the deployed core. Item 4 is planned work.
Neither this status nor the roadmap authorizes a production cutover.

1. **Deployable foundation.** Implemented and deployed on the single VPS.
   Django/PostgreSQL provides the only portal. Private admin access
   uses an SSH tunnel. Deploy provisions the database, applies migrations, collects
   root-owned static files, and checks readiness. Repeat operations retain data.
   See [the control-plane guide](CONTROL_PLANE.md).
2. **Accounts, credentials, and idempotent import.** Implemented in
   `sing_box_manager/control_plane/`. `Account` and `Credential` store proxy accounts,
   portal password hashes, reset counters, and per-protocol secrets.
   Import verifies config parity and preserves subscription counters.
   See [the account guide](CONTROL_PLANE.md#account-import-admin-management-and-node-sync)
   for access switches and import restrictions.
3. **Admin management, subscription serving, and node sync.** Implemented in
   `sing_box_manager/control_plane/`. Operators manage `Account` and inline
   `Credential` rows in Django Admin (restricted to direct loopback/SSH tunnel), hash
   optional legacy-client passwords, and reset subscription links. Django serves `/sub/<token>`
   (both `sbc` envelopes and mobile remote profiles with `subscription-userinfo`),
   `/sub/<token>/` (browser user page), `/sub/<token>/files/<path>`, `/install.sh`,
   and `/install.ps1`. `control-plane --action sync-node` (and `sbm deploy` when
   `control_plane.authoritative: true`) renders the server config from PostgreSQL,
   checks it with `sing-box check`, and applies it to `sing-box.service`.
   The deployed reliability changes include transaction-safe sync requests, config preservation,
   shared locking, service recovery, and database-backed traffic collection.
   Migrations `0003_account_portal_enabled` and `0004_traffic_permission` are applied.
   Production acceptance passed.
   Operational details belong in [the control-plane guide](CONTROL_PLANE.md).
4. **Device management: planned after Linux TUN.** Operators issue single-use
   enrollment codes through private Admin. Each enrolled device gets its own
   subscription and protocol credentials. Operators manage revocation and enrollment limits.
   No ordinary-user login or password recovery is required. Codes and subscription
   tokens grant no account-management or cross-device permissions.

The single-node service uses a systemd path unit for credential sync. Keep it until
the node agent passes acceptance. Do not add quota fields without defined
suspension, manual-disable, reset, and reactivation rules.

PR 2 consolidates portal routes and account state. `/api/sub` and machine-token
compatibility remain because hosted installers use them. Mobile clients use remote
profiles or current JSON downloads, not new per-user release archives.
Traffic operators need a separate Django permission; old portal-admin flags grant nothing.
Keep the shared store, signed manifests, hosted installers, `sbm gc`, and historical data.
The next feature after consolidation acceptance is native Linux TUN.

### M5: Node agent

Planned after device management. Config distribution and node status are the core scope.
The agent does not require quotas, a traffic-storage rewrite, or an official-binary switch.

- Add a node agent, with Go as the proposed language. Use separate node authentication.
  Validate candidate configs, apply them safely, and report the applied revision and health.
- Preserve the last good config during control-plane outages. Define how stale or
  unreachable nodes are excluded from a credential-revocation cutover.
- Apply account/device revocation to new and existing connections. Measure propagation
  delay and service interruption before production acceptance.
- Reuse proven configuration builders, validation, locking, and recovery behavior.
  Replace systemd sync and deployment duties only where the agent assumes them.
- Preserve traffic collection, account attribution, and historical data. Define the
  reporting contract before moving collection into the agent. Report replay must not double-count usage.
- Keep code, binaries, and applied configs root-owned on nodes.

Done when authenticated distribution, repeat application, invalid-config rejection,
outage recovery, revocation, and node status pass fixture and disposable native checks.
The legacy client interfaces remain until the later clean break.

API-stream-only accounting is separate work. Compare it with existing counters
before removing the poller, reconciliation logic, or custom server build.
Quota enforcement and a PostgreSQL traffic migration are not prerequisites for M5 or M6.

### M6: Multiple nodes

Planned after the node agent and required before the clean break.

- Enroll a node with one command and a one-time token. A provisioning script sets up
  the agent, the hardened units and the firewall, and reads the real sshd port.
- Decide control-plane hosting and CDN placement before provisioning additional infrastructure.
  Review the separation between the public control-plane domain and node endpoints.
- Add node groups. Subscriptions list every node in the user's groups, with an
  auto-select group for normal traffic and a fixed node for proxied AI traffic.
- Track each node's traffic against its provider allowance and reset day. KiwiVM
  becomes one optional provider adapter.
- Decide on VLESS-Reality for nodes without a domain.

Done when a second VPS serves users and a blocked node can be replaced without
touching clients.

### M7: Invitation self-service and reusable plans

Deferred and outside the clean-break scope. The approved user flow has no public
registration, login password, or password recovery. Operators manage accounts and devices.
User self-service would require a new identity, authorization, and recovery design.
Do not infer approval for it from device enrollment.

Plans and automated quotas need separate requirements for accounting periods,
suspension, manual disablement, resets, and reactivation.

### M8: Paid plans

Add prices, orders and a payment provider. Decide whether registration opens without
invites. Details are planned when M7 lands.

## Working rules

- Each milestone lands in small pull requests. Preserve current access until the
  explicitly authorized clean break, then reject the retired credentials and contracts.
- Delete replaced code in the same milestone. Remove the legacy compatibility layer
  at the clean break rather than carrying it into the new device model.
- Update the docs a milestone touches, and delete docs for removed parts.
- Test behavior. Real sing-box binaries run in CI, and tests don't assert source
  text.

## Open questions

| Question                                                  | Decide by           |
| --------------------------------------------------------- | ------------------- |
| First Linux TUN environment, DNS and routing requirements | M1b                 |
| Enrollment code expiry, delivery and safe retry behavior  | Device PR           |
| Agent outage handling and revocation acknowledgement      | M5                  |
| Control plane host and CDN provider                       | M6                  |
| VLESS-Reality for nodes without a domain                  | M6                  |
| Production cutover date and recovery procedure            | After M6 acceptance |
| Payment provider                                          | M8                  |
