# Refactoring spec

Checked on 2026-10-22.

This spec sets the target architecture for sing-box-manager and the milestones that
move the repo there. Prefer clean breaks that reduce lifetime complexity, maintenance or runtime cost.
Use mature frameworks instead of preserving custom machinery for compatibility alone.
The [cutover boundaries](#clean-break-and-legacy-removal) separate client-contract upgrades from future device enrollment.
Each production cutover needs separate authorization.
When a milestone lands, update its status and affected docs. Review its scope before implementation.
The [node agent and multi-node specification](MULTI_NODE_SPEC.md) defines the complete M5/M6 implementation contract.
Its commands and interfaces are planned, not deployed functionality.

## Why

The current design couples the manager to a single-VPS deployment. Much of its
complexity comes from machinery built around premises that the target design drops.

| Premise at the start                            | Machinery it needs                                            | Target                                                        |
| ----------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------- |
| Each user's archive bundles the sing-box binary | Content-addressed store, manifest, archive splicing, `sbm gc` | The binary and the config download separately                 |
| One sing-box process per protocol               | Three units and configs, six stats listeners                  | One process per node with every inbound                       |
| Accounting spans collectors and stores          | Separate collector processes and central SQLite               | One agent, local transactional storage and central PostgreSQL |
| The client is bash 3.2 and PowerShell 5.1       | A 7,178-line sourced `setup.sh`, the same logic written twice | One Go binary on every desktop OS                             |
| Configs start as JSON templates                 | Nine near-identical templates that code then patches          | Configs built in code                                         |
| Users live in a SOPS file                       | Four credentials per person, two validators for one file      | Users in a database                                           |

## Decisions

| Decision                                                                                          | Status                                    |
| ------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| Build a minimal Django control plane instead of adopting an existing panel                        | Decided                                   |
| Operators create free accounts and issue single-use device enrollment codes                       | Decided                                   |
| Ordinary users have no login password, public registration, or password-recovery flow             | Decided                                   |
| sing-box is the only proxy core on nodes and clients                                              | Decided                                   |
| Security fixes come first, then the client CLI                                                    | Decided                                   |
| The control plane uses Django, Django Ninja and PostgreSQL                                        | Decided for M5/M6                         |
| PostgreSQL is the only central policy and traffic database                                        | Decided for M5/M6                         |
| Local sbm uses Ansible/OpenSSH for role-specific deployment                                       | Decided for M5/M6                         |
| The client CLI, `sbc`, uses Go, Cobra and pflag, with cgo disabled                                | Decided                                   |
| Add a Go node agent before device management                                                      | Next priority, followed by multiple nodes |
| Give each enrolled installation its own subscription token and proxy credentials                  | Deferred for review after M6              |
| Support multiple nodes before invalidating all legacy user credentials and links                  | Decided                                   |
| A client runs one config per installation, with routes as Clash modes and protocols in a selector | Decided                                   |
| Configs are built in code and pass `sing-box check` before release                                | Implemented                               |
| Nodes run the official sing-box build                                                             | Proposed                                  |
| Per-user traffic comes from the sing-box API stream only                                          | Deferred; requires accounting parity      |
| Render policy transactionally and apply validated node configs forward only                       | Decided for M5/M6                         |
| Use node-scoped account credentials and a v2 desktop contract before devices                      | Decided for M5/M6                         |
| Support trojan, hysteria2, naive and optional Snell v6. Defer VLESS-Reality beyond M6             | Decided                                   |
| The subscription envelope carries the Linux TUN config beside the mixed config                    | Decided                                   |
| Linux TUN answers DNS through its route rules, not through the systemd-resolved stub              | Decided                                   |
| Linux TUN is refused in WSL2, containers and remote shells                                        | Decided                                   |
| Linux TUN uses IPv4-only DNS and rejects public IPv6 without removing capture                     | Decided                                   |

sing-box 1.14.0 supports VLESS-Reality as both server and client, and `sing-box check`
accepts a server config and a client config that use it.

## Target architecture

The current system is one Django/PostgreSQL control plane and one sing-box node,
with systemd-triggered config sync and SQLite traffic history.
Linux TUN meets the agreed M1b acceptance scope. The next priorities are the node
agent and multiple nodes, in that order. Device management is deferred for review
after multi-node acceptance. Extended TUN network compatibility remains separate work. The
[diagnostic contract](CLIENT_DIAGNOSTICS.md) defines implemented mixed/TUN checks,
capture limits, and acceptance gates.
The diagram describes that target, not deployed functionality.
The [M5/M6 specification](MULTI_NODE_SPEC.md) defines data ownership, interfaces, recovery, deployment and acceptance.
Review migrations and implementation details against that contract in each PR.
Quotas, public registration, plans, and payments remain outside this delivery sequence.

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

| Component     | Runs on                             | Built with                          | Job                                                                                                                                                                         |
| ------------- | ----------------------------------- | ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Control plane | Current VPS, no CDN required for M6 | Django, Ninja, PostgreSQL           | Accounts, nodes, credentials, traffic, subscriptions and private admin. Devices follow M6 review                                                                            |
| Node agent    | Each VPS                            | Go, gRPC, local SQLite              | Applies config forward only, collects traffic and reports status. Keeps local service during control-plane outages                                                          |
| sing-box      | Each VPS and client                 | Official or compatible custom build | Proxies traffic                                                                                                                                                             |
| Client CLI    | Linux, macOS, Windows               | Go (`sbc`)                          | Enrolls with a single-use code, installs signed artifacts, manages the proxy, switches route and protocol, shows usage, updates and uninstalls. Linux TUN. Users type `sbc` |
| Mobile        | Android, iOS                        | Official sing-box apps              | Import the subscription as a remote profile                                                                                                                                 |

### Control plane data

These are logical entities, not a finalized migration schema.
M5 and M6 retain accounts and account subscriptions, with node-scoped protocol credentials.
Original-node secrets and subscription URLs survive migration.
Device entities and enrollment interfaces are deferred for review after M6.

| Entity               | Responsibility                                                                                    |
| -------------------- | ------------------------------------------------------------------------------------------------- |
| Operators (`User`)   | Private Django login and administrative permissions                                               |
| Accounts (`Account`) | Proxy user identity and master enable switch, without a user login password                       |
| Enrollment codes     | Operator-issued permission to enroll one device for one account, with expiry and single-use state |
| Devices              | Account, installation identity, enable switch, and independent subscription state                 |
| Credentials          | Account, node, protocol and secret in M5/M6. Device ownership follows later review                |
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
account attribution. M5 migrates central traffic history into PostgreSQL.
Node-local SQLite remains necessary for durable collection during control-plane outages.
Device-specific paragraphs above describe the deferred target, not an M5/M6 prerequisite.

#### Node data ownership

In M5 and M6, control-plane PostgreSQL is authoritative for node domains, IP addresses, supported protocols, and ports.

- Operators change desired node settings through private Admin or an authorized management command.
- Local `sbm` can register initial node settings during the first deployment.
  Repeat deployments read current control-plane settings. They must not overwrite those settings with an old local inventory.
- Keep node SSH credentials and the build signing private key on the operator's machine, not in the control plane.
- Keep desired settings separate from reported node state. A database edit does not prove that a node applied it.

Define deployment checks and subscription publication rules before changing client-facing endpoints.
A domain or port change also requires checks for certificate and firewall readiness.

#### Operator node management

In M5 and M6, local `sbm` invokes control-plane management commands over SSH.
Reuse existing host SSH and sudo authorization. These commands do not require a Django login session.
Do not add a separate operator HTTPS API or operator management tokens for this scope.

- Management commands register node identities and API secret digests through existing SSH trust.
- Local `sbm` invokes Ansible roles to install node components over SSH.
  Nodes generate and retain their API secrets locally. No public enrollment endpoint is needed.
- Node agents use separate node credentials over HTTPS. They can access only their own node interfaces, not operator management commands.
- Keep Django Admin restricted to loopback and SSH tunnels.

Operator management requires SSH access to the control-plane host. Routine agent operation does not require that SSH connection.

### Interfaces

The device PR defines the enrollment request and response. It consumes an
operator-issued code and returns a device subscription. Mobile enrollment must
produce a device profile usable by a supported sing-box app.
The subscription paths below remain read-only.
M6 preserves account tokens. A future device credential cutover expires them when device subscriptions replace them.

| Request                                           | Caller      | Purpose                                                                   |
| ------------------------------------------------- | ----------- | ------------------------------------------------------------------------- |
| `GET /sub/<token>?format=sbc&os=...&sing-box=...` | `sbc`       | Returns the envelope with the user's configs, nodes and latest versions   |
| `GET /sub/<token>`                                | Mobile apps | Returns the user's sing-box config                                        |
| `GET /sub/<token>/`                               | Browsers    | User page with the subscription link, QR code, install commands and usage |
| `GET /node/v1/config`                             | Node agent  | Stored versioned bundle with an ETag                                      |
| `POST /node/v1/traffic`                           | Node agent  | Absolute versioned accounting snapshots                                   |
| `POST /node/v1/status`                            | Node agent  | Applied generation, process identity and health                           |
| `POST /node/v1/live`                              | Node agent  | Bounded current connection snapshot                                       |

Subscription responses carry a `subscription-userinfo` header with upload, download,
quota and expiry. Nodes authenticate with a per-node bearer token that the control
plane stores hashed. Binaries for sing-box and `sbc`, rule-set snapshots and their
signed manifest are served under the subscription path, so no public page identifies
the host.

Route strategies are `direct`, `ai-only`, `gfw-proxy`, `china-direct` and `global`.
Desktop retains `ai`, `gfw` and `china` aliases for saved selections and commands.
Mobile offers only the five canonical names in one multi-protocol profile.
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
| Linux TUN          | Keep the hardened root unit, the systemd-run capability probe, and admin tools resolved from sbin. Never use the systemd-resolved stub as a DNS server under TUN: captured resolver upstream queries loop back into it                                                                                                                              | M1b requirements                                                            |
| Downloads in China | No CDN-hosted page assets. PowerShell `irm` needs `text/plain`. Send auth headers up front, since PowerShell 5.1 and wget wait for a challenge otherwise                                                                                                                                                                                            | `control_plane/views.py`, `web/client/install.ps1`                          |

## Milestones

| Milestone | Name                                       | Status                                              |
| --------- | ------------------------------------------ | --------------------------------------------------- |
| M0        | Security fixes                             | Done                                                |
| M1a       | Client CLI for Linux and macOS, mixed mode | Done                                                |
| M1b       | Linux TUN in the client CLI                | Done within the defined acceptance scope            |
| M2        | Client CLI for Windows                     | Done                                                |
| M3        | Configs in code, one server process        | Done                                                |
| M4b       | Control plane, devices and subscriptions   | Core deployed; devices deferred for review after M6 |
| M5        | Node agent                                 | Next priority                                       |
| M6        | Multiple nodes                             | Planned after the node agent                        |
| M7        | Invitation self-service and reusable plans | Deferred pending need                               |
| M8        | Paid plans                                 | Deferred pending need                               |

### Delivery order

Milestone numbers identify scope, not delivery order. M5 and M6 precede the review
of device management in M4b.

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
3. **Automatic client maintenance.** Merged as `c19a43c`. Independent refresh and
   signed-upgrade policies, startup serialization, and recovery passed native CI.
   This change has no published client release yet.
4. **Official mobile profiles.** One import button, local QR code and manual URL
   import provide a unified multi-protocol profile. Mobile shares the five PC route
   strategies and uses real DNS answers. Per-protocol URLs return `400`.
   This consolidation does not rotate account tokens or protocol credentials.
   iOS unified-profile device acceptance is operator-reported complete, without
   device or version details. Android acceptance is optional for Snell code
   completion. Snell-specific iOS checks remain pending. See the
   [acceptance boundary](README_MOBILE.md#acceptance-boundary).
5. **M1b: Linux TUN.** Complete within the [defined acceptance scope](#m1b-acceptance-scope).
   Workstation console checks cover activation with a private umask, five route
   modes, real Trojan/Hysteria2/Naive traffic, refresh and check-only workers.
   Isolated QEMU checks cover installation, native timer triggers, a signed
   engine upgrade from 1.14.2 to 1.14.3, committed/pending/dirty state across
   reboot, explicit recovery and uninstall. Both KVM and TCG pass locally.
   Product client code needs no further change for these lifecycle checks.
   No new client release or server deployment is implied. Do not restore the
   legacy Bash client.
6. **M5 node agent.** Deliver SSH bootstrap, Ansible deployment, typed HTTPS interfaces,
   forward-only config application and integrated Go collectors. Migrate central history to PostgreSQL.
   Preserve original-node credentials, account links and accounting semantics.
7. **M6 multiple nodes and client contract.** Deliver node-scoped credentials, ordinary auto selection,
   fixed AI egress and a v2 desktop envelope. Retire v1 and password/machine-token exchange at an explicit upgrade boundary.
   Keep account subscriptions. Device management is not a prerequisite.
8. **M4b device-management review.** Review scope and scheduling after M6 acceptance.
   The target uses operator-issued codes and independent device access. Do not add public user login.
9. **Device credential cutover.** After device acceptance, separately authorize enrollment and invalidation of account-level proxy access.
   This is not the M6 client-contract cutover.

Quotas, invitation self-service, plans, and payments remain deferred.
Keep the current systemd sync path until the agent replaces it safely.

### Clean break and legacy removal

These are design boundaries, not authorization to change production access.
Preserve accounts, operator authentication, infrastructure settings, signed downloads and useful traffic history.

**M6 client-contract boundary:**

- Ship a transition client that reads v1 and v2 before enabling v2-only server responses.
- Require an explicit client upgrade. Do not maintain a single-node v1 projection indefinitely.
- Keep account subscriptions and original-node protocol secrets. Import existing subscription-token digests without changing URLs.
- Remove password/machine-token exchange, HMAC token derivation and their compatibility code after the upgrade window.
- Remove legacy password/admin-parity fields. Preserve the portal switch as a renamed subscription switch.
- Keep signed download access and installer service/TUN safety checks.
- Test the upgrade path, old-client refusal and unchanged account URLs.

**Future device credential boundary:**

- First complete device management and cross-node device revocation acceptance.
- Provide enrollment instructions and operator-issued codes before withdrawing account-level access.
- Revoke old protocol credentials and terminate existing sessions on every affected node.
- Invalidate account subscriptions only when device subscriptions replace them.
- Keep unreachable nodes pending until confirmed removal or verified external isolation.
- Test that new devices work and all retired credentials fail. Recovery must not restore retired access.

Client-contract refusal does not revoke cached proxy profiles.
Device deferral does not delay the M6 compatibility deletion.
Each boundary needs an authorized production date and recovery procedure.
Measure planned infrastructure interruptions against the 60-second gate.

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
and replaces the `proxy` command with `sbc`. M1a ships mixed mode, and M1b adds
Linux TUN.

| Area         | Bash client                                                                  | `sbc`                                                                                 |
| ------------ | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Client code  | `setup.sh` and four libraries, about 8,500 lines, sourced into every shell   | A Go binary with a Cobra command layer, and one `sbc init` line in the rc file        |
| Install      | `bash <(curl …/install/linux.sh)` or `macos.sh` downloads a per-user archive | `curl -fsSL …/install.sh \| sh` downloads `sbc`, which asks for the subscription URL  |
| Auth         | Portal password, then a machine token                                        | A subscription token                                                                  |
| Config       | The archive holds a config for each protocol and route                       | The server renders one config per user, and `sbc` fetches it                          |
| Switching    | Swaps the config and restarts sing-box                                       | Sets a Clash mode or a selector through sing-box's local API, without a restart       |
| Updates      | `proxy upgrade` reinstalls the archive; a daily check prints a hint          | Independent refresh and signed-upgrade jobs default to on, every 6 hours              |
| Local proxy  | Loopback without auth                                                        | Loopback, with auth on shared hosts. LAN and Docker bridge sharing require auth       |
| Integrations | Shell, git, desktop, Docker daemon and containers                            | Shell, desktop, Docker daemon and containers                                          |
| Diagnostics  | 13 `proxy check` subcommands                                                 | `sbc status`, which shows traffic, `sbc ip` and `sbc speed`; `sbc doctor` follows M1a |

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
- `sbc refresh` refreshes configuration. `sbc upgrade` installs signed binaries
  and any required configuration adaptation. Each action has independent
  `auto [on|off]` and `interval [duration]` controls. Both default to on, every 6h.
  Saved policies survive reinstall and upgrades. Policy changes do not immediately
  upgrade or restart the proxy. Manual actions ignore automatic switches.
  A background task registration failure warns but does not block manual refresh
  or upgrade. Invalid settings and failed recovery still block these actions.
- Refresh sends the ETag. A changed config must pass `sing-box check` and retain
  a working service. Upgrades validate candidates before replacing live files.
  Failed upgrades stop the candidate before restoring binaries and configuration.
  An engine lifetime lock spans Unix exec or the Windows wrapper's child lifetime.
  Shared/exclusive startup permission separates file writes from controlled restarts.
  An operation lock prevents
  conflicting maintenance writes. A recovery record protects interrupted commits.
- The installed build receipt prevents a successful upgrade from recreating its
  own notification. The old `update` task entry only migrates schedules. Remove
  that entry after the v2 client upgrade window. See
  [automatic maintenance](README_SBC.md#automatic-maintenance) for migration and
  recovery details.
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
  Diagnostics use the [current command contract](CLIENT_DIAGNOSTICS.md#cli-contract).
  Retired diagnostic flags fail before requests.
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
  `sbc lang en|zh` saves a client-only choice, and `sbc lang auto` removes it;
  `SBC_LANG` overrides that choice too. `install.sh` reads only the environment.
- Commands: `install`, `init`, `link`, `lang`, `upgrade`, `uninstall`, `on`, `off`, `env`,
  `status`, `ip`, `check`, `speed download`, `route`, `protocol`, `port`, `refresh`, `start`, `stop`,
  `restart`, `run`, `desktop on|off`, `docker on|off` and `version`. `on` and `off`
  point shells, and the desktop after `desktop on`, at the proxy or away from it,
  and leave sing-box running, so an agent that runs `sbc off` keeps its own
  connection. `start` and `stop` control the service. `refresh` refreshes the config
  now, and `link set` replaces the subscription URL.
- `sbc ip` retains separate IPify and ident.me observations, with optional
  ipwho.is estimates. `sbc check` measures website first responses and HTTP access.
  A private list, read-only presets, and explicit URLs support common destinations.
  Both modes follow active rules. Default checks make no AI-provider request.
- `sbc speed download [--size 20MB]` measures one Cloudflare response stream under
  active rules. It has byte, time, and idle limits. Bare `speed` sends no request.
  There is no protocol ranking, forced diagnostic inlet, or provider-region scraping.
- Diagnostics preserve selections and installed configuration. Versioned JSON
  separates transport, HTTP, capture, and IP validation. Missing TUN evidence
  retains HTTP observations but fails scope verification. See
  [the CLI guide](README_SBC.md#network-diagnostics).
- `sbc status` shows this billing cycle's traffic from the `subscription-userinfo`
  header of the last refresh.
- `sbc` lives in a directory of its own, which `sbc init` puts on PATH.
- `sbc desktop on` points the GNOME or macOS proxy settings at `sbc`.
  WebRTC leak protection is Linux Chrome only. `sbc privacy browser webrtc on|off`
  writes or removes the mandatory Chrome `WebRtcIPHandling` policy and records
  an opt-out. `sbc privacy browser language on|off` separately matches personal
  profiles' language lists to the node and restores owned fields. Chrome must
  be closed. `sbc privacy browser geolocation on|off` separately manages the
  system-wide `DefaultGeolocationSetting=2` policy. It defaults to off and needs
  explicit activation. Local diagnostics distinguish choice, configuration and
  policy conflicts. `sbc privacy browser on|off` attempts all three components
  independently.
  See [Chrome privacy](WEBRTC.md) for ownership, recovery, sync risks and limits.
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
  tools must remove that state first. Native Linux TUN support is provided by
  M1b; see [Linux TUN](README_SBC.md#linux-tun).

Done when CI on Linux and macOS runners installs from a test subscription, starts,
switches route and protocol without a restart, rolls back a bad refresh and
uninstalls, and when a bash install without TUN migrates.

These follow M1a as separate pull requests: `doctor`, a redacted report for support
requests; `lan on|off`, which listens on all interfaces with a
generated username and password; and the Docker container proxy, which listens on
the Docker bridge address with auth.

#### M1b: Linux TUN

Required after automatic client maintenance and official mobile-app import.
`sbc` supports mixed mode and, on native Linux, TUN. The legacy implementation is not maintained.

`sbc tun on|off` runs for native Linux. The portal renders the Linux TUN config
beside the mixed config in the `sbc` subscription envelope, and release validation
runs `sing-box check` on both. The `config_tun` field is additive, so the envelope
version stays 1 and older clients ignore it. `sbc` selects the profile for the mode
it runs and keeps its local patches in both.

Environment:

| Check       | Requirement                                                                                                         |
| ----------- | ------------------------------------------------------------------------------------------------------------------- |
| Host        | Native Linux, a running systemd system manager, and `/dev/net/tun`                                                  |
| Refused     | WSL2, containers, and a remote shell (`SSH_CONNECTION`, `SSH_CLIENT` or `SSH_TTY` is set)                           |
| Tools       | Resolve `ip`, `useradd` and `userdel` through sbin, and fail closed when one is missing                             |
| Capability  | Create and remove a disposable interface with `systemd-run` under the unit's user, capability set and device policy |
| Names       | `sbc-tun0`, `/etc/sbc/tun`, `/var/lib/sbc-tun` and `sbc-tun.service`                                                |
| Ownership   | One owner per machine. Record the owner UID in a manifest and refuse automatic takeover                             |
| Shared host | Refused when another login account exists, by the check that also gives the mixed proxy a password                  |

Root owns binaries, configs, rule snapshots and the unit. `sbc-tun` runs sing-box
with `CAP_NET_ADMIN` only and writes only its state directory. Stage the signed
binary and record its hash, and re-sync it on every `tun on`. Copy rule snapshots
into the root-owned config directory. Replace remote rule sets with local sets
that use absolute paths, so the first start works without a download.

Set final directory modes explicitly after creation, including candidate binary
and rule directories. Repair existing public control-directory modes before
taking the root lock. Support a private `umask 077` without changing the caller's
umask. Keep configuration and rule access service-group-only and engine state
private. Require a separate native console cycle before accepting these modes
on an operator machine.

Routing:

- One exact config with `-c`. Mixed mode and TUN never run together. The user unit
  refuses to start while TUN is active, including after login, so a delayed start
  cannot restore mixed mode.
- Use `auto_route: true`, `strict_route: true`, `auto_redirect: true`,
  `auto_detect_interface: true` and interface `sbc-tun0`. Do not set
  `route.default_mark` or a dial `routing_mark`; `auto_redirect` conflicts with them.
- Require nftables support. Verify it under the unit's capability set before
  publishing, and refuse when it is missing. Docker bridge networking and
  `SO_BINDTODEVICE` traffic remain outside the validated compatibility scope.
- Use `172.19.0.1/30` and `fdfe:dcba:9876::1/126`, and refuse activation when a
  foreign interface or route claims them.
- Capture every destination, including IPv6. Send `ip_is_private` traffic to
  `direct`; do not exclude private ranges. Reject remaining IPv6 before every
  direct exception, route mode and protocol selection. Do not disable system
  IPv6 or remove IPv6 capture. Public IPv6 access is disabled.
- Keep the five Clash modes and the protocol selector through the local API.
  `sbc tun on` generates the API port and secret and writes them to the owner's
  state (0600) and the root config (0640 `root:sbc-tun`). `sbc route` and
  `sbc protocol` use that API without sudo. Native diagnostics require owner
  metadata bound to the committed generation, including inlet identity and known policy.
- Accept activation only when a representative request works under the current
  mode. Never switch modes for verification. Resolve with the system resolver,
  match the new TUN connection by its source and destination socket, and obtain
  a verified HTTPS response on that socket. `direct` does not require the unused
  proxy. Other modes also require the selected proxy's latency request to work.
  No automatic probe contacts an AI provider. Native fixtures check each mode's
  direct and proxy branches with synthetic rule-set contents.
- Do not check interface packet counters: `auto_redirect` redirects captured
  TCP to a local socket before routing. Read capture from the Clash API instead.
- Stop the mixed service and clear owned proxy settings before publishing. Warn
  about inherited Mixed variables before shutdown. Existing processes can need
  a restart from a clean environment. Shell hooks remove recognized values without
  exporting ownership or capture markers. They preserve unrelated values and
  block exports during active or uncertain capture. `sbc env` also refuses exports.
  See [environment migration](TUN_ENV_MIGRATION.md). `sbc on`, `desktop on` and
  `docker on` refuse while TUN is active.
- Prepare a complete candidate before replacing live files. Retain superseded
  config and binary directories with `.prev-` suffixes. Keep the transaction
  journal and persistent mixed-mode block under root-owned `/etc/sbc/tun-control`,
  outside the engine's writable state directory.
- Apply leaves the transaction pending. Commit requires saved user API state,
  a live service and an unexpired three-minute recovery deadline. A transient
  root service stops uncommitted capture independently of the controlling
  process. Stale recovery tasks cannot stop a committed or different generation.
  Refuse pending startup after reboot without an armed recovery timer.
- Report `off`, `active` or `dirty`. A failed stop retains the mixed-mode block.
  Confirm service state, MainPID, interface, routes and the sing-box nftables
  table before clearing it. Never restore mixed mode or start an old TUN on
  failure. Root operations use an OS lock, separate from the user's operation
  lock. Both locks release when their process exits.
- `sbc tun off` does not restore mixed mode. Under TUN, `sbc off` reports that
  capture remains, and `sbc status` reports the TUN service.
- `sbc tun on` enables the unit at boot after verification, and `sbc tun off`
  disables it. There is no fail-open: while the unit runs, a failed proxy leaves
  captured traffic without an exit. `sbc status` and the docs name the local
  recovery command.

DNS:

- Set `dns_mode: hijack`, so sing-box answers captured queries with its DNS.
  `auto_redirect` with `dns_mode: disabled` did not capture DNS from applications
  and systemd-resolved on the target kernel. Do not add a resolved stub server.
- Resolve proxied names through Google over the proxy and direct names through
  Aliyun (`223.5.5.5`). Never use the systemd-resolved stub as a DNS server in a
  TUN config: captured upstream queries would loop back into it.
- Give the direct outbound the Aliyun resolver, and keep the deployment host and
  `direct_domain_suffixes` exceptions direct.
- Set `dns.strategy: ipv4_only` in the TUN config. Return successful empty AAAA
  answers even when the upstream offers both address families. Keep mixed and
  mobile DNS unchanged. Reject captured public IPv6 independently of DNS, so
  cached addresses, IPv6 literals and encrypted DNS cannot select an IPv6 exit.
- Route NTP (UDP 123) direct and reject every other non-private UDP destination
  after DNS and NTP. The proxy's own UDP connections are unaffected.
- Verify with `auto_redirect` and `dns_mode: hijack` that DNS from applications
  and from systemd-resolved is captured and answered.

Runtime commands:

- Query and switch routes and protocols through the committed active TUN API
  using the owner's saved state. Use the operation lock for mutations.
  Do not restart capture or rewrite installed files for a switch.
- Reject pending, dirty or inconsistent metadata, invalid owner state and
  unavailable APIs. Never fall back to mixed mode after a TUN error.
  Status uses the same owner-state checks. Offline lists and completion remain
  based on the installed configuration.
- IP, website, and download diagnostics use native TUN connections and bounded,
  asynchronous capture evidence. Unknown evidence never becomes a direct-path claim.
  Unsafe state refuses requests. Read the [diagnostic contract](CLIENT_DIAGNOSTICS.md).

Maintenance:

- While TUN is active, automatic refresh and upgrade jobs only check updates.
  Changed configuration, signed rule hashes or signed builds produce a pending
  hint. `sbc status` asks the user to update manually. Workers do not apply
  updates, invoke sudo or change services.
- Manual `sbc refresh` and `sbc upgrade` use one TUN maintenance path at the
  machine's own console. A short interruption is permitted. Prepare and check
  the complete candidate before stopping the previous capture. Refresh keeps
  the installed binaries. Upgrade selects signed client and engine builds.
- Preserve the active route and protocol through startup defaults and a fresh
  cache namespace. Verify that selection and representative current-policy
  traffic before publishing user files and API state. Commit root last.
- Keep a typed user recovery record with both root transaction IDs. A committed
  root generation retires that record without rollback. Root retains the commit
  fact after successful or failed shutdown. A stopped committed generation keeps
  its published user files. Recheck transaction identity after privileged
  recovery. Unknown or different identities retain the recovery files.
  Otherwise, confirm capture is off before restoring user files, unless the previous generation
  never stopped. A failed shutdown retains recovery data and the mixed-mode
  block. Never call mixed restart or rollback hooks from this path.

Stop an active TUN before install or uninstall. Pending or dirty capture needs
repair with `sbc tun off` before a new maintenance transaction.

The core `e2e-tun` suite runs in a disposable QEMU guest through
`scripts/dev/test-tun-vm.sh` and cannot capture the runner's network. It uses
product-rendered routing, a guest router namespace, distinct DNS answers, and a
proxy-only target that cannot succeed by direct access. It includes native stop
failure, failed activation, an unused dead proxy under `direct`, and controller
death after startup. Maintenance fixtures check signed candidate rejection,
a client upgrade, changed rule snapshots, preserved CLI selections and
check-only workers. They observe independent timer recovery after both
maintenance controllers die, before restoring user files. Alternate selector
transports remain SOCKS fixtures, not real Naive acceptance. CLI switches cover
five modes, a route alias and both selector paths without changing generation,
PID or installed files. Keep `scripts/lib/ui.sh` while the server scripts use it.

##### M1b acceptance scope

M1b closes on functional console acceptance plus the three lifecycle gates below.
The lifecycle suite uses `scripts/dev/test-tun-vm.sh lifecycle` and the same
isolated QEMU network and product configuration renderer as the core suite.
It boots the same disk four times and checks distinct kernel boot IDs.

| Gate                        | Required evidence                                                                                                                                                                                                                                                                |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Reboot and recovery         | A committed generation starts without changing files or selections. Pending and dirty generations refuse startup and retain the mixed block. Explicit `tun off` confirms cleanup and permits mixed use.                                                                          |
| Real engine upgrade         | Signed official 1.14.2 and 1.14.3 archives have pinned hashes. Root and owner binaries, libraries and version receipts agree after upgrade. Current selections and captured traffic work.                                                                                        |
| Installation and scheduling | The hosted Unix installer installs into a fresh guest. Real user timers report an available update without sudo or runtime changes. Active TUN blocks install/uninstall. After explicit shutdown, uninstall removes user files and schedules while retaining root recovery data. |

The VM uses ephemeral signing trust in test builds only. It does not change
production trust or contact the operator portal. Its alternate protocol selector
is a SOCKS fixture, not another real-protocol acceptance claim.

Native IP, website, and download checks have separate diagnostic acceptance.
Public IPv6 access is disabled. Isolated checks cover AAAA suppression and public IPv6 rejection, not
full IPv6 connectivity. Docker bridge and interface-bound traffic are unvalidated
compatibility areas, not M1b completion gates. Document these limits rather than treating missing
tests as passes. Physical workstation reboot, production timer migration and a
published client release are not implied by isolated lifecycle acceptance.

### M2: Client CLI for Windows

The same `sbc`, with the three parts that touch the operating system swapped.
Everything that goes through sing-box's API is unchanged. The installer, proxy
service and ordinary commands run unelevated.

| Piece         | Linux and macOS                          | Windows                                                                                                                                                                                                                                                                                                      |
| ------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Files         | `~/.config/sbc`, `~/.local/share/sbc`    | One folder, `%LOCALAPPDATA%\sbc`, whose permissions already exclude other users. A scheduled task may lack the profile variables, so an `sbc` under `cli\` finds the folder from its own path                                                                                                                |
| Service       | systemd user unit, LaunchAgent           | Scheduled tasks `sbc-proxy`, `sbc-config-refresh`, and `sbc-upgrade`, written as XML and registered with `schtasks.exe`. They run whether the user is logged on or not (S4U), so there is no console window and sign-out keeps them                                                                          |
| `sbc run`     | execs sing-box                           | Runs sing-box as a child in a job object, so ending the task ends sing-box, and records its process id, which stopping the task waits on: Task Scheduler gives an ended task's process a few seconds before it terminates it. Without a console, output goes to `sing-box.log`, which starts over past 10 MB |
| Service state | `systemctl is-active`, `launchctl print` | PowerShell's `Get-ScheduledTask`, whose states are the same in every display language, where `schtasks` prints them in the display language                                                                                                                                                                  |
| Programs      | replaced in place                        | `WriteFile` moves a running program to a unique retired path. This permits rollback while an older updater still runs. A later `sbc run` removes unused retired files. Uninstall removes the folder sbc runs from after sbc exits                                                                            |
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
   HKCU and WinINet, `on|off` through persistent user environment variables,
   and the user PATH entry. `sbc env` prints PowerShell commands for the current
   session. Port changes and uninstall preserve foreign settings; desktop proxy
   use remains opt-in. Uninstall stops on cleanup failure before deleting the
   client. WebRTC protection is now Linux Chrome only and is not part of this
   milestone; see [WebRTC protection](WEBRTC.md).
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
Naive and Snell are omitted when their enabled-user rosters are empty. Snell
requires configured shared infrastructure. See [Snell v6](SNELL.md) for the
explicit credential backfill and remaining acceptance. Default installs use all
packaged inbounds; an explicitly selected inbound must exist in the package.

The single counter listener uses protocol-qualified internal labels for Trojan,
Hysteria2 and Snell, while Naive retains its wire-visible username. The single connection
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
Device management remains unimplemented, so M4b is incomplete.
Review its scope and scheduling after M6 acceptance.
Items 1–3 describe the deployed core. Item 4 describes the deferred target.
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
4. **Device management: review after M6.** This target is not a prerequisite for M5 or M6.
   Operators issue single-use enrollment codes through private Admin. Each enrolled device gets its own
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
The next delivery is M5, followed by M6. Neither requires device management.

### M5: Node agent

Next priority. The [full specification](MULTI_NODE_SPEC.md) defines implementation slices and acceptance.
Device management, quotas and an official-server-binary switch are not prerequisites.

- Use Django Ninja and synchronous PostgreSQL policy transactions, without a render worker.
- Install node identity through operator SSH. Use Ansible for host provisioning and role-specific artifacts.
- Run one cgo-free Go agent with integrated collectors and local SQLite. No project Python runtime is required on nodes.
- Validate before stopping the working engine. Publish complete generations forward only.
  Startup failure after publication requires repair, not automatic replay of old credentials.
- Keep local proxy service during control-plane outages. Track offline revocation as pending.
- Migrate central traffic history to PostgreSQL. Preserve counter authority, stream attribution and reconciliation semantics.
- Use versioned absolute snapshots instead of a delta outbox and per-batch receipt history.
- Adopt the original node without changing its secrets or subscription URLs. Remove the retired writer after acceptance.

Keep the custom counter-enabled server build until a separate accounting-parity project permits its removal.
Prove history parity and crash recovery before replacing deployed collectors.
Measure revocation against existing sessions, not only new-login refusal.

#### Identity and node boundaries

- Keep account permissions in the control plane. The agent consumes opaque credential IDs and a local roster.
- Use node-scoped account credentials. Keep immutable identity mappings for historical and delayed traffic.
- Separate subscription authentication from rendering and routing. Future devices must not require another node API.
- Node UUIDs identify installation lifetimes. Replacement uses a new UUID and retains old revocation/history records.
- Require confirmed process termination and durable roster exclusion for revocation, or verified external isolation.

#### Deferred server permission management

Retain root-based operations and root ownership of code, binaries, configs and private state.
Privilege reduction and an agent/applier split are separate work, not M5/M6 prerequisites.

### M6: Multiple nodes

Planned after M5 and before device review. Retain account subscriptions and use independent credentials on each node.

- Deploy a second node with one operator-local command. The control plane holds no SSH keys.
- Use existing VPS hosting and the HTTPS origin. No CDN is required.
- Grant node access through groups and publish only prepared, applied endpoint/credential pairs.
- Offer all node/protocol pairs manually and one automatic ordinary pool.
- Pin proxied AI traffic and DNS to an explicit node with verified static egress. Never fail over to another node implicitly.
- Deliver a v2 desktop envelope and an explicit client upgrade boundary. Remove permanent v1 projection and password/machine-token exchange.
- Preserve mixed/TUN safety, native mobile profiles, traffic history and per-node provider calendars.
- Keep VLESS-Reality, new no-domain TLS options and devices outside this milestone.

Done when two nodes pass routing, accounting, revocation, replacement and recovery gates.
Replacement preserves subscription links, not immediate delivery of unknown endpoints to stale profiles.
New endpoints require refresh, including console refresh for active Linux TUN.
Review device management after this acceptance.

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

- Each milestone lands in bounded pull requests with acceptance evidence.
- Prefer a clean contract and mature framework over indefinite compatibility or custom infrastructure.
- Separate technical upgrades from production access changes. Execute only the authorized cutover.
- Delete replaced code after acceptance in the same milestone. Keep data and migration fixtures, not parallel maintained implementations.
- Update the docs a milestone touches, and delete docs for removed parts.
- Test behavior. Real sing-box binaries run in CI, and tests don't assert source
  text.

## Open questions

M5/M6 design decisions are specified in [MULTI_NODE_SPEC.md](MULTI_NODE_SPEC.md).
Implementation and native acceptance remain pending. These later decisions stay outside that scope.

| Question                                                        | Decide by               |
| --------------------------------------------------------------- | ----------------------- |
| Device enrollment code expiry, delivery and safe retry behavior | Device PR               |
| Device-management scope and scheduling                          | Review after M6         |
| Production v2 client-contract cutover and recovery              | M6 acceptance           |
| Production device credential cutover and recovery               | After device acceptance |
| Payment provider                                                | M8                      |
