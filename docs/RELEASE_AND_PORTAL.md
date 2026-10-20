# Release Build and Portal Usage

Checked on 2026-10-19.

This guide covers the local operator workflow for:

- building release artifacts with `pixi run sbm release`
- starting the authenticated download portal with `pixi run sbm serve`
- downloading a published release from the portal

For the encrypted inventory and secret-handling rules, read
[`docs/SECRET_HANDLING.md`](./SECRET_HANDLING.md) first.

## Before Building a Release

1. Create or update the encrypted runtime inventory using
   [the SOPS setup instructions](SECRET_HANDLING.md#age-and-sops-setup).
   Copying the example to a `.sops.yaml` filename does not encrypt it.

1. Manage accounts in Django Admin. Inventory account records are initial-import inputs,
   not a second live roster. New accounts do not need portal passwords.

1. Update the config file if you need a different sing-box version, web port, or allowed hosts.
   - `upstream_cache_root` defaults to `.cache/sing-box-manager/upstream`
   - `traffic_stats.enabled` defaults to `false`
   - when `traffic_stats.enabled: true`, the server package is rebuilt from upstream source with `with_v2ray_api`
   - `traffic_stats.database_path` defaults to `/var/lib/sing-box-manager/traffic-stats.sqlite3`,
     which the systemd units own as their `StateDirectory` so usage history survives wiping and
     reprovisioning the deploy root; set a relative path to keep the database beside the code
     when running the portal locally
   - `traffic_stats.timezone` defaults to `Asia/Shanghai`
   - `traffic_stats.relay_multiplier` defaults to `2`; set it to `1` on a host that bills
     egress only (see [Plan usage and the relay multiplier](#plan-usage-and-the-relay-multiplier))
   - `traffic_stats.api_listen` is the shared V2Ray counter listener, defaulting to
     `127.0.0.1:19080`; it must stay on loopback because it has no authentication
   - `traffic_stats.connection_api_listen` is the shared connection stream listener,
     defaulting to `127.0.0.1:19090`; it must not collide with `api_listen`
   - for one migration release, old `*_api_listen` and `*_connection_api_listen`
     keys are accepted; only the Trojan addresses are retained. Replace them with
     the two shared keys before the compatibility period ends
   - `traffic_stats.connection_api_secret` is empty by default, which disables
     authentication on those listeners. That is acceptable only while they stay on
     loopback; set one before moving any of them off `127.0.0.1`
   - `traffic_stats.domain_top_n` defaults to `50` and caps how many domains are retained
     per user per day (see [Per-domain Traffic Stats](#per-domain-traffic-stats))
   - `traffic_stats.show_user_domains` defaults to `false`; set it to `true` to show each
     user their own top domains on their download page
   - `client_upgrade.policy` defaults to `suggested`; choose `off`, `suggested`, or
     `required`, and optionally set a short public `client_upgrade.message`

1. Edit the configured inventory with `sops`, not a plain text editor:

   ```sh
   pixi run sops config/inventory/runtime.sops.yaml
   ```

1. Keep real passwords, obfs secrets and TLS material in the inventory or its referenced TLS files. Never hardcode them in config builders or fixtures.

`pixi run sbm release` also auto-loads a repo-root `.env` for `SOPS_AGE_KEY` and
`GITHUB_TOKEN` when those variables are not already exported. Treat that `.env` as a
plaintext local secret file.

## TLS Inventory Notes

For CA-signed TLS:

- set `deployment.host`
- set `deployment.tls.key_path`
- set `deployment.tls.certificate_path`
- keep `deployment.tls.self_signed_cert: false`

For self-signed TLS:

- keep using `deployment.tls.key_path` and `deployment.tls.certificate_path` as PEM source files
- set `deployment.tls.self_signed_cert: true`
- expect rendered release artifacts to contain inline TLS material, including the private key in server configs
- avoid self-signed TLS for production Naive deployments; upstream sing-box docs note that it changes Naive traffic behavior

## GitHub Token

Maintainer release runs can supply `GITHUB_TOKEN` through the shell, a secret
manager, or the local `.env` described in the secret-handling guide.

`pixi run sbm release` can still read public release metadata from `SagerNet/sing-box`
without a token, but it may warn and can hit lower anonymous GitHub API rate limits.

If you keep `GITHUB_TOKEN` in a repo-root `.env`, `sbm release` will pick it up
automatically. Raw `sops` commands still need their own exported `SOPS_AGE_KEY` or Age
key file.

## Build the Release

Install dependencies if needed:

```sh
pixi install
```

Build the release artifacts:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml release
```

When traffic stats are enabled, the release build still downloads the official client
archives for end users, but it swaps the server package over to a custom-built Linux
`sing-box` binary so the single node config can use `experimental.v2ray_api`.
It contains Trojan and Hysteria2 inbounds, plus Naive only when at least one
`naive.users` entry is enabled. An empty or entirely disabled Naive roster omits
that inbound: sing-box rejects a Naive listener without users.

The config builders target [sing-box 1.14.3](https://github.com/SagerNet/sing-box/releases/tag/v1.14.3).
Set `sing_box_version: 1.14.3` in the selected inventory and run `pixi install`
before building. The custom server build requires Go 1.25.5 or later in the 1.25
series, as declared in `pixi.toml`.

After a successful build, the main generated outputs are:

- `config/generated/release-info.json`
- release artifacts under `releases/`, including `server/config.json` (which resolves
  hostnames through local DNS first, pins UDP associations with `udp_connect`, and
  rejects private and reserved IPv4/IPv6 ranges) and one `server/sing-box.service`

Before publishing, the builder runs `sing-box check` on mobile profiles, desktop
configs for Linux, macOS and Windows, the Linux TUN config, and the server config.
Client validation uses synthetic credentials rather than the inventory roster.
Any failed check aborts the build.
Server CA certificate paths usually exist only on the VPS: when absent locally,
the check uses an ephemeral ECDSA certificate in a private copy. Inline self-signed
material is checked as rendered. The installer checks the real node certificate
and key before stopping any running service. Checker output is not logged because
it can contain credentials.

Releases contain shared artifacts, not per-user archives or authentication snapshots.
In authoritative mode, the packaged server config has no inventory users. Deployment
preserves the live config and then applies PostgreSQL accounts.
Linux, macOS and Windows install `sbc`. Android and iOS use remote subscription
profiles or current JSON downloads from the subscription page. The page provides
[official-app import buttons, local QR codes, and copyable URLs](README_MOBILE.md).

With `client_signing_key` set, the release also builds the downloads the `sbc`
client installs, under `client/` in the release directory. Without it, the build
warns and leaves them out.

| Path                                            | Contents                                                                                                      |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `sbc/<platform>/sbc`                            | `sbc` for linux-amd64, linux-arm64, darwin-amd64, darwin-arm64 and windows-amd64, which saves it as `sbc.exe` |
| `sing-box/sing-box-<version>-<platform>.tar.gz` | The upstream archive, already checked against GitHub's digest, kept in the store; `.zip` for Windows          |
| `rules/<hash>.srs`                              | The rule snapshots this release took                                                                          |
| `manifest.json`                                 | Every file above with its size and SHA-256, the `sbc` version and the sing-box version                        |
| `manifest.json.sig`                             | The base64 ed25519 signature of `manifest.json`                                                               |

`sbc` is built with Go from `cmd/sbc`, with cgo off and `-trimpath`, and its version
is the release's 8-character commit hash, plus `-dirty` for uncommitted changes. An
unchanged tree builds the same bytes and signs the same manifest, so the release keeps
its id. The store holds each sing-box archive once, about 110 MB for the four
platforms, and `sbm gc` treats them like the server binary.

`/api/client-update` reports the signed client manifest's hash as the build ID.
Existing releases remain readable. The shared store and `sbm gc` retain support
for old manifest references until those releases are deliberately removed.

The subscription page offers a current mobile profile for each enabled protocol. The config `sbc` fetches offers `direct`, `ai-only`,
`gfw-proxy`, `china-direct` and `global` as modes. `ai`, `gfw` and `china` remain
alias modes so stored selections survive refreshes. The default is `china-direct`.
See [route strategies](README_SBC.md#route-strategies) for their meanings.
Every desktop route uses two community AI rule sets:
`AI-Anthropic` for Anthropic and `AI-Geosite` for other AI vendors, including OpenAI.
Both ship as validated snapshots, so the first start does not require a successful
rule download. Both refresh daily through the proxy.

Routing and DNS use the same AI sets. In desktop proxy strategies, AI matches
precede AdGuard rejection and China-direct rules. `direct` sends AI traffic and
DNS directly through the system resolver, without a proxy override. Listed AI
hosts still precede AdGuard rejection. This order permits community-listed AI
hosts that AdGuard also blocks. Android uses the same AI sets before mode overrides
and China-direct routing, with proxied Google DNS before FakeIP. Android downloads
the sets into GUI-managed storage instead of receiving desktop snapshots.

`china-direct` proxies unknown destinations and sends recognized China destinations
directly. `gfw-proxy` proxies GFWList and AI destinations and sends other destinations
directly. `ai-only` proxies listed AI destinations and sends the rest directly.
`global` proxies traffic except LAN and configured direct exceptions.
`direct` retains traffic restrictions but sends all allowed destinations directly. The Android mobile profile retains its FakeIP and China-direct rules.

Desktop profiles retain Dreista's narrow China-domain, Apple-China, GFWList,
and AdGuard sets. MetaCubeX's `sing/geo/geoip/cn.srs` supplies one combined
China IPv4/IPv6 set. Domain rules precede IP matching without resolving unknown
hostnames merely to test GeoIP. GFWList and AI matches precede China-direct rules.
These five binaries plus the two AI sets form the seven bundled remote snapshots.
All remote desktop sets refresh daily through the proxy.

### Community AI rules

| Tag            | Community source                                                                                                                       | Purpose                                |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| `AI-Anthropic` | [MetaCubeX `anthropic.srs`](https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/sing/geo/geosite/anthropic.srs)                 | Dedicated Anthropic and Claude domains |
| `AI-Geosite`   | [SagerNet `geosite-category-ai-!cn.srs`](https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-category-ai-!cn.srs) | Broader AI coverage, including OpenAI  |

MetaCubeX rebuilds its binaries daily. The readable
[v2fly Anthropic list](https://github.com/v2fly/domain-list-community/blob/master/data/anthropic)
covers API, authentication, MCP, content, and website CDN domains. The repository
keeps no personal domain catalog or keyword fallback.

Community coverage is not a complete Anthropic firewall allowlist. Neither checked
AI set includes `claude.app`, which the
[official Desktop requirements](https://code.claude.com/docs/en/desktop#network-access-requirements)
list. Shared sign-in, captcha, and external artifact CDN hosts are not all included.
Use the [official network requirements](https://code.claude.com/docs/en/network-config#network-access-requirements)
when diagnosing a missing host. Prefer a correction in the community source to a
local domain list.

Configured direct exceptions and private-network rules retain priority over AI
routing. DNS interception and encrypted-DNS, QUIC, and STUN restrictions also retain
priority. A rule applies only to traffic that enters sing-box. An application that
ignores the shell and desktop proxy settings can still connect directly.

Every client profile includes direct exceptions for the exact `deployment.host`
and explicit `deployment.direct_domain_suffixes`. Suffixes include the apex and
subdomains, without inferring parent domains from the host. IP-valued hosts get
an exact IP exception. Exceptions precede AI, AdGuard, Clash overrides and
geographic rules; DNS interception and transport restrictions remain in effect.
Matching DNS rules precede mobile FakeIP and use direct Aliyun, or the system
resolver for desktop `ai-only` and `direct`. Direct outbounds use the same resolver, including in `global`.
Desktop configs are rendered per platform. The system resolver used by `ai-only`
and `direct` is the one setting that differs between them: Linux names systemd-resolved's stub
address, while macOS and Windows use sing-box's `local` transport. That
transport queries the link's upstream servers itself and binds the query socket
to the link, which kernels that require `CAP_NET_RAW` for the bind refuse in an
unprivileged client service, failing every lookup the route sends direct.

The Linux TUN config (`config_tun`) is the exception: it never uses the
systemd-resolved stub, because captured upstream queries would loop back into
it. It resolves direct and `ai-only` names through Aliyun, sets
`dns.strategy: prefer_ipv4`, keeps NTP direct, and rejects other non-private UDP.
The TUN service reads the staged rule snapshots and does not download rule
updates; `sbc tun on` stages the installed snapshots again.

The rule sets ship as binary snapshots under `sing-box/rules/`, with a
`files.txt` inventory containing one filename per line.

Every release build resolves each rule repository and branch to a commit, then
downloads all required files from that commit. The build validates every snapshot
with the target Linux sing-box binary before publishing the release. A failed download or invalid rule file fails the build;
there is no fallback to files from an earlier build. This requires access to the
GitHub API and GitHub Raw even when the upstream binary archives are cached.
`GITHUB_TOKEN` authenticates API requests only.

The public snapshots are shared by all desktop packages and covered by the release
manifest hashes. Each rule's `initial_path` points into the installed state
directory. On first start, sing-box can open the local inbound using these files
even if rule downloads fail. Later updates use the named `rule-downloads` HTTP
client through the proxy. Mobile profiles use the same explicit HTTP client but
keep GUI-managed storage and do not receive desktop snapshots.

The `sbc` desktop config enables `store_dns` and uses one `cache.db` under the
private client state directory. This file also retains the mode and protocol
selection. Alias modes keep existing selections valid across route renames.
DNS answers retain normal TTL expiry, with `optimistic: false`.
Saved answers survive service restarts and reinstalls. Uninstall removes the cache. Writes are asynchronous, so an answer received just
before shutdown may need another lookup after restart.

## Start the Portal

The [Django/PostgreSQL control plane](CONTROL_PLANE.md) is the only portal.
It serves subscriptions, signed downloads, installers, and legacy-client compatibility APIs.
PostgreSQL supplies all portal accounts and subscription counters. There is no runtime
inventory authentication or generated user snapshot.

Enable the control plane, migrate PostgreSQL, and collect static files before starting it.
A built release is needed for signed binary downloads, not for account management.

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml serve
```

## Collect Traffic Stats

When `traffic_stats.enabled: true`, collect per-user traffic deltas from the single
node's Trojan, Hysteria2 and Naive inbounds with:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml stats-collect
```

The collector stores usage totals in `traffic_stats.database_path` using the configured
timezone, bucketed two ways from the same delta: per calendar day in `daily_usage`, and
per billing cycle in `monthly_usage`. Both keys derive from the local day, so they always
agree — the sum of a cycle's daily rows equals its monthly row exactly.

### The billing cycle boundary

The cycle follows the VPS, not the calendar. Many plans reset partway through the month,
and a cycle that rolled over on the 1st would tell users they had a fresh allowance while
the VPS was still days from resetting.

The collector derives the reset day from the KiwiVM `data_next_reset` field, converted in
`traffic_stats.timezone`, and stores it in the stats database. There is no config key for
it. Once a day is known it refreshes at most once a day; until then the collector retries
every 15 minutes, so a fresh deployment converges within the hour but a KiwiVM that never
answers keeps being asked at that rate. Either way the lookup never fails a collection:
without `vps_info` credentials, or before the first successful lookup, the cycle is the
calendar month exactly as before.

`cycle_month` stays a `YYYY-MM` string and names the month a cycle _starts_ in. With a
reset day of 3, `2026-09` means 2026-09-03 through 2026-10-02. The portal and
`proxy check quota` render the real boundary from `cycle_start`/`cycle_end`, so clients
need no update. A reset day past the end of a short month is pulled back to the last day,
so cycles always tile without gap or overlap.

When the derived day changes, the collector recomputes `monthly_usage` from `daily_usage`
once, inside a single transaction, so the window the portal shows and the rows it sums
never disagree.

The subscription page reads SQLite and shows the account's current-cycle upload,
download, total, plan share, and last update time. Operators with `control_plane.view_traffic`
can open `/admin/traffic/` through an SSH tunnel for totals, rankings, trends, and protocols.
Each row links to `/admin/traffic/users/<username>/` for detailed history. The trend window
is chosen with the `7 天` / `30 天` / `90 天` links, which reload the page — the portal
ships no JavaScript for this, and the charts are server-rendered SVG. Dates use `traffic_stats.timezone`, including the billing-cycle boundary.

Daily buckets only start accumulating when this version is deployed; the monthly totals
and the protocol split work retroactively, because both read data already collected.
That asymmetry bounds the recompute above: cycles that start before the first `daily_usage`
row cannot be rebuilt from it, so they are left exactly as they were collected rather than
being blanked. Roughly 10k daily rows accrue per year for nine users across three
protocols, well under a megabyte, and nothing prunes them.

Deploy updates the remote environment when `pixi.lock` changes. If the portal shows
tables but chart rendering fails, check the web service logs and confirm that the
remote environment was updated with `pixi install --locked`.

For unattended collection, run the command from a `systemd` timer every 5 minutes. See:

- [`docs/examples/systemd/sing-box-manager-traffic-stats.service`](./examples/systemd/sing-box-manager-traffic-stats.service)
- [`docs/examples/systemd/sing-box-manager-traffic-stats.timer`](./examples/systemd/sing-box-manager-traffic-stats.timer)

## Per-domain Traffic Stats

The V2Ray stats API above counts three things and nothing else: bytes per inbound tag,
per outbound tag, and per user. It has no notion of where the traffic went. So the portal
can say a user burned 221 GiB this cycle but not what burned it.

sing-box 1.14.0 added a second API, configured as a `services` entry rather than under
`experimental`, whose `SubscribeConnections` RPC streams one record per connection
carrying the username, the destination and the byte totals together. That join is what
the domain breakdown is built from. `sbm release` adds the service to each rendered
node config when `traffic_stats.enabled` is true, one shared listener on
`traffic_stats.connection_api_listen`. Events are partitioned by inbound into the
existing protocol buckets. Trojan and Hysteria2 use protocol-qualified internal
authentication labels, which the collectors map back to usernames; Naive keeps
its wire-visible username. Passwords and client credentials do not change. No extra build tag is needed; unlike
`experimental.v2ray_api`, this one is in every sing-box build.

Collect it with:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml stats-stream
```

Unlike `stats-collect`, `stats-stream` runs continuously in the foreground.
Each subscription starts by replaying all live connections and up to 1,000 recent
closed connections. Earlier closed connections are unavailable, so an outage can
still leave gaps in the breakdown. The systemd service keeps the daemon running:

- [`docs/examples/systemd/sing-box-manager-traffic-stream.service`](./examples/systemd/sing-box-manager-traffic-stream.service)

### Two collectors, one authority

The five-minute poller alone writes `daily_usage` and `monthly_usage`, which supply
quota figures. Its cumulative counters survive collector downtime while the
sing-box process remains running. Traffic since the last successful poll can be
lost if sing-box restarts before another poll.

The stream daemon writes domain and destination usage, replay checkpoints, and a
live connection snapshot. Each flush commits all dated usage and the corresponding
checkpoints in one transaction. A failed batch remains available for retry, and
newer events wait for that batch to commit. Checkpoints also preserve the counted
state of connections with no payload. Process termination can still lose pending
attribution if its connections have already left sing-box's replay history.

Domain usage keeps the local date when the stream observes the bytes. The poller
assigns each interval's bytes to the date of its next successful poll. Daily domain
figures can therefore differ from daily quota figures around midnight, during
replay, and while either collector has unflushed data. Domain percentages describe
the domain breakdown's own total.

After a poll, reconciliation compares the signed difference for each user,
protocol, and byte direction. A poll that crosses a date boundary records its
starting day, ending day, and byte delta. Excess domain attribution on an earlier
day in that interval can offset a shortfall on its ending day, up to that poll's
delta. The offsets are stored separately and never move observed domain usage to
another day. Unused excess cannot cancel unrelated future usage, and counter
resets do not create boundary credit.

For example, the last poll before midnight may report 100 bytes, followed by 50
more bytes that the stream sees before midnight. The next poll assigns those 50
bytes to tomorrow. Reconciliation records a 50-byte boundary offset, so it does
not also add them as unattributed usage. Domain usage remains 150 bytes yesterday,
while quota usage remains 100 yesterday and 50 today.

The remaining shortfall becomes `__unattributed__`. Reconciliation recomputes the
estimate from stored source totals, including older days after delayed writes, so
it can shrink when the stream catches up. The offset is a bounded estimate because
the poller does not know the precise time of every byte. A domain window is not
guaranteed to equal the quota window, especially when its edge cuts a poll interval.

Database upgrades preserve usage and existing checkpoints. Older checkpoints stay
protected until a complete replay establishes which connections remain. Previous
versions did not record poll boundaries, so historical boundary offsets cannot be
reconstructed reliably. Domain names already folded into `__other__` also cannot
be recovered from the database.

Three domain keys are reserved and are never real hostnames:

| Key                | Portal label | Meaning                                           |
| ------------------ | ------------ | ------------------------------------------------- |
| `__other__`        | 其他域名     | The tail beyond `traffic_stats.domain_top_n`      |
| `__unattributed__` | 未归属       | Estimated shortfall after boundary reconciliation |
| `__ip__`           | 直连 IP      | Destinations reached by IP with no hostname       |

A small, steady `__unattributed__` is normal, since the two collectors flush on different
schedules. A large or growing one can indicate missed attribution; check
`journalctl -u sing-box-manager-traffic-stream`.

### Retention

The top `traffic_stats.domain_top_n` domains per user, protocol, and completed day
are kept by name (default 50). The remaining domains are folded into `__other__`,
preserving bytes and connection counts. Today remains uncompressed so a domain
that becomes busy later can still finish as the day's largest domain.

Compaction waits until pending batches for that day have committed. Completion is
recorded in SQLite, so a restart neither compacts today nor skips unfinished older
days. Rankings over several days use the retained daily names and `__other__`.

All active connection checkpoints are retained independently of the live view's
500-row limit per protocol. Up to 4,000 retired checkpoints per protocol are kept,
ordered by retirement time rather than their last byte update. A complete replay
removes checkpoints absent from both the live set and the closed replay history.
There is no full connection log.

Note that `Connection.domain` arrives empty unless a sniff action is configured, which
these server configs do not use. It does not need to be: trojan, hysteria2 and naive all
carry the target hostname in the proxy handshake, so sing-box puts it in `destination`
and the daemon reads it from there.

Because it comes from the handshake, that hostname is whatever the client chose to send
rather than anything sing-box validated. The daemon lowercases it, drops the root dot and
any non-printable character, and truncates it at 253 characters before storing it, so a
crafted name cannot break the admin page that charts the row.

### What the portal shows

`/admin/traffic/` shows a domain ranking chart and top destinations across all
users. Each `/admin/traffic/users/<username>/` page shows one user's breakdown, plus a
table of top destination IPs. `/admin/traffic/connections/` is a private live view of open connections for
debugging. It refreshes itself with a meta tag without JavaScript.
A subscription ending clears that service's live view while preserving replay
checkpoints. Clean daemon shutdown clears the view, and snapshots older than three
minutes are hidden if the daemon exits without cleanup.

`/api/usage` gains a `top_domains` array of raw byte counts and hostnames. Older clients
ignore it.

Ordinary users see only their own cycle totals by default. Setting
`traffic_stats.show_user_domains: true` adds their own top domains to their download
page. It is their own data either way, but it is more than the page disclosed before, so
the operator opts in rather than being opted in by an upgrade.

### Plan usage and the relay multiplier

The per-user counters and the VPS bandwidth counter measure different things, and the
gap between them is close to a factor of two.

sing-box records `uplink` and `downlink` per user on the **client leg only**: bytes
received from that client, and bytes sent back to it. KiwiVM's `data_counter` is the
host's accounting on the VM's interface, counting **both directions**. A relayed byte
crosses that interface twice, once arriving from the client and once leaving for the
origin, so the host charges about twice what the per-user counters record. Summing all
users and comparing against the VPS counter therefore looks like a 2x shortfall when
nothing is wrong.

`proxy check quota` reports both. The `Upload`, `Download`, and `Total` rows stay exactly
as sing-box measured them, so a user can check them against their own client. A derived
`Plan usage` row projects that total onto the host counter and names the multiplier
inline:

```
User
  Cycle         2026-09-03 ~ 2026-10-02
  Upload        12.40 GiB
  Download      98.10 GiB
  Total         110.50 GiB
  Plan usage    221.00 GiB / 1000.00 GiB (22.1%, x2 relay)
  Last updated  2026-09-09 14:30
```

The portal shows the same projection on subscription pages, `/admin/traffic/`,
and `/admin/traffic/users/<username>/`. A user who never runs
the client would otherwise read the measured total against the plan quota and conclude
they have used about half of what the host has charged them. The row is omitted when it
would say nothing the `Total` beside it does not: at a multiplier of 1 with no plan total
the projection is the measured total.

The same figures reach API clients as `billed_bytes`, `relay_multiplier`, and
`plan_total_bytes` on `/api/usage`; `/api/vps-info` carries `relay_multiplier` and
`bandwidth_total_bytes`. The measured fields are unchanged, so an older client keeps
working and simply omits the new row.

`plan_total_bytes` is the allowance as KiwiVM reported it, not the two-decimal gigabyte
display figure, so the percentage divides two exact byte counts. The portal
caches KiwiVM readings, failures included, and keeps serving the last good one while the
API is unreachable. When it has none, `plan_total_bytes` is absent and both clients print
the projected total and the multiplier without a plan share, rather than guessing a
denominator.

Set `traffic_stats.relay_multiplier` to match how the host bills:

| Host billing           | Value | Effect                                    |
| ---------------------- | ----- | ----------------------------------------- |
| Inbound plus outbound  | `2`   | Default. BandwagonHost and most VPS hosts |
| Egress only            | `1`   | Plan usage equals the measured total      |
| Calibrated to observed | `2.1` | Absorbs protocol overhead                 |

Expect the real ratio to sit slightly above 2. sing-box counts proxied payload while the
interface carries TCP/IP and TLS headers, ACKs, retransmissions, and handshakes, and the
VM also carries traffic no user owns: package updates, ACME renewals, DNS, and inbound
scan noise. If the observed ratio drifts well past about 2.1, suspect traffic that is not
being attributed rather than overhead. Collection targets come from enabled PostgreSQL
accounts and credentials in authoritative mode, or inventory otherwise.
Users absent from that source do not enter the per-user query.

## Download a Release

Open `/sub/<token>/` with the link provided by an operator.
Use `sbc` on Linux, macOS and Windows. Use a remote profile or download the current
protocol JSON on Android and iOS. Public password login, `/files`, and `/download`
are retired. Existing installed clients retain their proxy credentials.

## One-Liner Install

Linux and macOS users install `sbc` with one line:

```sh
curl -fsSL "https://vpn.example.com/install.sh" | sh
```

`/install/linux.sh` and `/install/macos.sh` serve the same script, because the bash
client's `proxy upgrade` downloads and runs them.
[Subscription links](#subscription-links) describes what the script does, including
how it moves a machine from the bash client.

Windows users install `sbc` from an unelevated PowerShell terminal:

```powershell
& ([scriptblock]::Create((irm 'https://vpn.example.com/install.ps1')))
```

The script runs on Windows PowerShell 5.1 and PowerShell 7. It prompts for a
subscription link without echoing it, downloads `sbc/windows-amd64/sbc` and passes
the link to `sbc install` on standard input. The initial executable trusts HTTPS;
`sbc` verifies the signed manifest for the remaining downloads. The script runs
in memory and does not change execution policy.

For an installed PowerShell client, it exchanges the saved machine token through
`POST /api/sub`, preserves port, route, protocol and whether local authentication
is enabled, and checks the new proxy before running the old uninstaller. It then
hands over the port and any desktop proxy owned by the old client. See
[Windows installation](README_WIN.md) for recovery behavior.

`/install/windows.ps1` serves `install.ps1` too, which migrates legacy PowerShell client
installs to `sbc`.

The subscription page shows both installation commands under the account's link.

Installed bash clients check the public `/api/client-update` endpoint at most once
per day and compare its `client_build_id` with the build they run. A release that
carries `sbc` reports the SHA-256 of its client manifest there, which no bash build
matches, so every bash client sees an update. `suggested` prints one hint per
offered build, while `required` prints a warning in every new shell until the user
runs `proxy upgrade`. Neither policy blocks commands or network access, and neither
notice prints `client_upgrade.message`; `proxy check update` reports it.

For installer flags, client commands, and shell integration, see
[the client guide](README_SBC.md). For Windows requirements and service details,
see [the Windows guide](README_WIN.md).

## Subscription Links

`GET /sub/<token>?format=sbc&os=<os>&sing-box=<version>` returns the envelope the
`sbc` client uses for its config. It needs `web.subscription_secret`; without it, requests return 404.

A token is the first 16 bytes of an HMAC-SHA256 of the username and a reset counter,
keyed by `web.subscription_secret` and written in URL-safe base64. The portal keeps
only the counters and recomputes each enabled portal user's token to identify the account.
Django stores counters in PostgreSQL. `web.subscription_database_path` is only a
read-only input for the initial import of legacy counters.
A user needs subscription access and at least one enabled protocol.

Operators reset links through Django Admin. This advances the PostgreSQL counter
and rejects old links. Users cannot reset links from subscription pages.
`POST /api/sub` with a machine token or legacy portal credentials returns
`{"url": ..., "username": ...}`, which lets an installed client move to its link
without a paste; a user without a protocol gets `404`.

Django renders each subscription from PostgreSQL accounts and runtime infrastructure settings.
Compatibility endpoints also query PostgreSQL. `/auth`, `/api/token`, `/api/usage`,
and authenticated `/api/vps-info` remain for legacy clients. Public `/api/client-update`
retains its line-oriented response. Nginx limits password/exchange endpoints across workers.
The JSON response holds:

| Field                               | Contents                                                                                                      |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `version`                           | The envelope schema, `1`                                                                                      |
| `config`                            | The mixed sing-box config, with every route as a Clash mode and every protocol behind the `proxy` selector    |
| `config_tun`                        | The Linux TUN variant of `config`. Additive; clients without TUN support ignore it                            |
| `default_route`, `default_protocol` | Where a new install starts. The protocol falls back to the user's first one when they lack `default_protocol` |
| `default_node`                      | The node the current config uses                                                                              |
| `nodes`                             | Each server, the protocols the user holds on it, and its optional `region`                                    |
| `latest.sing_box`                   | The sing-box version the inventory pins                                                                       |

`nodes[].region` carries `deployment.client_region` when it is set.
`timezone` is an IANA name and `language` is BCP 47 with a region, such as
`zh-CN`. `sbc privacy region` applies it on Linux. Clients that predate the
field ignore it, and a node without it reports the target as unavailable.

Responses carry an `ETag`, and a matching `If-None-Match` gets `304`.
`subscription-userinfo` reports this billing cycle's upload and download bytes when
traffic stats are enabled, and `sbc status` shows the count from its last refresh. A
client older than the pinned minor release, 1.14.0 for
1.14.3, gets `409` with `sing_box_minimum`. An unknown token, or a user without a
protocol, gets `404`. Without `format`, the endpoint serves a mobile profile.
Unsupported formats or invalid `sbc` platform/version parameters get `400`.

`GET /sub/<token>/files/<path>` serves a file the release's client manifest lists, such
as `manifest.json` or `sbc/linux-amd64/sbc`, to a valid link, and `404` for anything
else. The envelope's `latest.sbc` names the `sbc` version the release carries.

`GET /install.sh` serves the POSIX installer for `sbc`, run as
`curl -fsSL https://<portal>/install.sh | sh`. It reads the link from the terminal,
downloads `sbc` for the machine, and runs `sbc install`, which succeeds only once a
page loads through the new proxy. `-p NAME` picks the protocol. Like `sbc`, it
speaks Simplified Chinese under a `zh_CN` or `zh_SG` locale, or with `SBC_LANG=zh`.

The repository maintains `sbc`, not the legacy Bash client. The hosted installer
still migrates an installed Bash client through its own uninstaller and desktop
library. `proxy upgrade` and the one-liner use this path:

1. A legacy TUN unit, transaction marker, or unsafe status blocks installation
   before downloads or file changes, even when user-scoped client files are missing.
   Use the installed client's cleanup tools before installing `sbc`.
2. It trades the saved machine token for the link through `POST /api/sub`, and asks
   for the link only when that fails.
3. It installs `sbc` on a spare port with the bash client's route and protocol. If no
   page loads through it, the script removes `sbc` again and leaves the bash client
   as it was.
4. It runs the bash client's uninstaller with `--yes --no-rc`. Without
   `SBM_UNINSTALL_CLEAN_PROXY`, that keeps the Docker settings, which work on because
   `sbc` takes over the same port. The uninstaller reverts the desktop proxy it set.
   If it stops after removing the client's services, the script removes the rest
   of the client's files itself. It retries for up to 10 seconds, because an NFS
   home can keep a file the old sing-box had open for a moment after it exits.
5. It removes the rc lines the bash installer added, unless the user manages the rc
   file (`rc_enabled=false`), and the global Git proxy settings that point at the old
   port.
6. It moves `sbc` to the old port, and turns the desktop proxy back on with
   `sbc desktop on` when it was the bash client's. Then it names any rc lines that
   still load the bash client.

The bash client's `proxy upgrade` sources its own `setup.sh` after the installer.
When `-p` shows that `proxy upgrade` ran it, the script leaves a `setup.sh` that tells
`proxy upgrade` there is nothing left to restore and then deletes itself.

A link is a credential. The portal's and nginx's access logs show `/sub/[token]` in
its place; each deploy sets up the nginx log format. nginx's error log can still
show a full request line, for example when the portal does not answer. Rotating
`web.subscription_secret` revokes every link; see
[server configuration](VPS_DEPLOYMENT.md#2-server-configuration).
