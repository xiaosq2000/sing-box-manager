# Sing-box VPN Client for Linux

> [!NOTE]
> This guide covers the bash client, whose command is `proxy`. Releases no longer
> pack it, so a new machine installs `sbc` with the
> [hosted one-liner](#hosted-portal-install). The guide stays for machines that
> still run the bash client, including those with TUN mode.

## Features

- Smart installation for `bash` and `zsh`
- Chinese and English terminal output
- Global, shell-local, and Docker proxy helpers
- Built-in service, environment, IP, and port checks
- Supports Trojan, Hysteria2, and Naive client packages
- Protocol switching when the authenticated client package includes multiple allowed protocols
- Persistent China, GFW, AI-only, and Global routing strategies for mixed mode

## Installation

> [!TIP]
> The installer will:
>
> 1. Detect your configured login shell and wire `proxy` into `~/.bashrc` or `~/.zshrc`
> 1. Remove any previous client install first, including legacy `/usr/local` installs
> 1. Install the selected `sing-box` client as a user-scoped `systemd --user` service
> 1. Print the exact shell RC changes applied during install
> 1. Let you override the target RC file with `--shell bash|zsh`
> 1. Explain linger and offer to enable it so the client can keep running after logout

### Quick Install

```bash
./client-install.sh
```

### Hosted Portal Install

The portal's one-liner installs `sbc`, the client that replaces this one:

```bash
curl -fsSL "https://vpn.example.com/install.sh" | sh
```

On a machine with this client installed, the one-liner and `proxy upgrade` both move
the machine to `sbc` and remove this client; see
[Subscription links](RELEASE_AND_PORTAL.md#subscription-links). A machine with TUN
mode set up keeps this client until `sbc` supports TUN. Releases no longer pack this
client, so a machine that doesn't have it already can't get TUN mode for now.

### Installation Options

```bash
# Install with a specific protocol
./client-install.sh -p hysteria2
./client-install.sh -p naive

# Install and target the bash RC file explicitly
./client-install.sh --shell bash

# Install without modifying shell configuration
./client-install.sh --no-rc

# Install with verbose output
./client-install.sh -V

# Show help
./client-install.sh -h
```

### Install Layout

The user-scoped client install uses standard XDG paths:

- binary: `~/.local/bin/sing-box`
- config: `${XDG_CONFIG_HOME:-~/.config}/sing-box/<protocol>/config.json`
- route variants: `${XDG_CONFIG_HOME:-~/.config}/sing-box/<protocol>/config-{china,gfw,ai,global}.json`
- TUN config: `${XDG_CONFIG_HOME:-~/.config}/sing-box/<protocol>/config-tun.json`
- helper scripts: `${XDG_DATA_HOME:-~/.local/share}/sing-box/`
- state: `${XDG_STATE_HOME:-~/.local/state}/sing-box`
- user unit: `${XDG_CONFIG_HOME:-~/.config}/systemd/user/sing-box-<protocol>.service`
- selected protocol: `${XDG_CONFIG_HOME:-~/.config}/sing-box/selected-protocol`
- selected route: `${XDG_CONFIG_HOME:-~/.config}/sing-box/selected-route`
- selected port: `${XDG_CONFIG_HOME:-~/.config}/sing-box/selected-port`
- installed client build: `${XDG_DATA_HOME:-~/.local/share}/sing-box/client-version`
- installer preferences: `${XDG_DATA_HOME:-~/.local/share}/sing-box/install-preferences`

Installation and reinstallation select `china`. `config.json` is the active copy;
the route variants remain beside it so switching never needs another download.

The installer checks the packaged `rules/files.txt` inventory and its files before
removing an existing installation, then copies them into the state directory's
`rules/` subdirectory. These initial rule files allow a new desktop installation to
start even when background rule downloads fail.

Desktop DNS caches use `cache-<protocol>-<route>.db` in the same state directory,
which is accessible only to the installing user. Services create private files
with `UMask=0077`. Each protocol and route has its own cache, and saved answers
survive service restarts until their normal DNS TTL expires. Reinstall and
uninstall clear these caches. Route validation uses the same `-D` state directory
as the service, including when custom XDG paths are configured.

At the end of install, the script explains linger and asks whether it should run the command for you.
If you decline or run the installer non-interactively, you can still enable it later:

```bash
sudo loginctl enable-linger <username>
```

### Supported Options

- `-h, --help` - Display help messages
- `-V, --verbose` - Enable debug logging
- `-p, --protocol PROTOCOL` - Specify protocol (`trojan`, `hysteria2`, or `naive`)
- `--shell SHELL` - Target a specific RC file (`bash` or `zsh`)
- `--portal-base-url URL` - Persist the portal base URL for `proxy check quota`
- `--no-rc` - Skip shell RC file configuration

## Usage

After installation, all client-side shell commands go through `proxy`.
There are no short aliases and no supported top-level helper commands besides `proxy`.
When `setup.sh` is sourced from your `~/.bashrc` or `~/.zshrc`, tab completion for `proxy` is registered automatically.
Open a new terminal, or re-source your shell RC file, if completion does not appear immediately after install.

```bash
proxy help
```

### Command Tree

```bash
proxy help
proxy version

proxy on [--force] [--port <PORT>]
proxy off

proxy protocol <trojan|hysteria2|naive>
proxy route [china|gfw|ai|global]
proxy port [<PORT>]

proxy service on
proxy service off

proxy tun on [--persist]
proxy tun off

proxy shell on [--force]
proxy shell off
proxy env on [--force]
proxy env off

proxy git on [--force]
proxy git off
proxy git on --local [--force]
proxy git off --local

proxy desktop on [--force]
proxy desktop off

proxy docker on [--force]
proxy docker off

proxy check
proxy check system
proxy check service
proxy check route
proxy check tun
proxy check shell
proxy check env
proxy check git
proxy check desktop
proxy check quota
proxy check update
proxy check ip
proxy check ip cn
proxy check ip private

proxy upgrade
proxy uninstall [--yes]
```

### Installed versions

Run `proxy version` to show the bundled sing-box version and the sing-box-manager
commit SHA recorded in the installed package:

```text
sing-box: 1.14.0
sing-box-manager: 9237d372
```

The SHA ends in `-dirty` if the package was built with uncommitted changes. Missing
metadata fields display `unknown`. The command reads the local `client-version`
file and works offline, even when the service is stopped.

### Global Proxy Management

These commands apply the composite proxy profile: the detected user-scoped `sing-box` client service, current shell environment variables, Git global proxy config, and GNOME desktop proxy settings when available.
On non-GNOME desktops, the desktop step warns and the shell and Git steps still apply.
If you only want to toggle the user service without touching proxy settings, use `proxy service on` or `proxy service off`.

```bash
# Enable global proxy
proxy on

# Enable global proxy even if no active sing-box service is detected
proxy on --force

# Pin the mixed inbound to a specific port instead of auto-detecting one
proxy on --port 1085

# Disable global proxy
proxy off
```

By default `proxy on` places the mixed inbound on port 1080, or the next free port
above it when 1080 is taken, and tells you which one it picked. A client that is
already running keeps the port it is already serving. `--port` pins a specific
port instead and fails if that port is in use.

The chosen port is recorded in `selected-port`, next to `selected-route` and
`selected-protocol` under the config directory. Reinstalling rewrites the route
configs from the downloaded package, which would otherwise reset the inbound to
1080; the marker is what carries your port across an upgrade, and the next
`proxy on` writes it back into the configs and restarts the client if the port
moved. Removing the file makes the next `proxy on` start from 1080 again.

### Mixed Inbound Port

The mixed inbound listens on `127.0.0.1` only, so other devices on the network
can't use it as a proxy.

Run the bare command to show the saved selection, or choose an unprivileged
port explicitly:

```bash
proxy port
proxy port 1085
```

Ports must be decimal integers from `1024` through `65535`. Before changing
anything, the command verifies that both TCP and UDP are free; selecting the
port already served by this client is a successful no-op. A port held by
another process is rejected without changing the marker, configs, services, or
proxy settings.

If a mixed client service is running, the command updates every installed
protocol and route config, restarts exactly the services that were active, and
waits for the new listener. If no service is running, it updates the saved
selection and configs but does not start one. Shell, Git, desktop, and Docker
settings currently enabled for this client are moved to the new port; disabled
and third-party-owned settings stay untouched. Any write, restart, readiness,
or integration failure restores the previous port and service state.

`proxy on --port <PORT>` uses the same range, TCP/UDP collision checks, and
transactional update path. A later `proxy service on` or `proxy on` checks the
port again in case another process claimed it while the client was stopped.

### Protocol Switching

If your authenticated Linux client package includes more than one allowed protocol, the installer installs each included protocol config and service, then starts only the selected protocol.
Use `proxy protocol ...` to switch later.

```bash
# Switch to Hysteria2
proxy protocol hysteria2

# Switch to Naive
proxy protocol naive

# Switch back to Trojan
proxy protocol trojan
```

Switching is local and authorization-preserving: it only works for protocol configs already installed under `${XDG_CONFIG_HOME:-~/.config}/sing-box/<protocol>/config.json`.
If `proxy protocol <protocol>` says the protocol is not installed, reinstall from the hosted portal after your account has access to that protocol.
`proxy on` and `proxy service on` use the saved selected protocol when multiple installed protocols are present.

### Routing Strategies

One saved mixed-mode route applies to every installed protocol, so changing protocol
does not change the route. Run `proxy route` without an argument to show the saved
selection, or choose one explicitly:

```bash
proxy route china
proxy route gfw
proxy route ai
proxy route global
proxy check route
```

| Route    | Application traffic                                                             | DNS                                                                           |
| -------- | ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `china`  | China destinations direct; other public traffic proxied                         | China queries use Aliyun directly; other queries use Google through the proxy |
| `gfw`    | GFWList and AI destinations proxied; other traffic direct                       | GFWList and AI queries use proxied Google; other queries use direct Aliyun    |
| `ai`     | AI services proxied, with their sign-in and captcha hosts; other traffic direct | AI queries use Google through the proxy; other queries use systemd-resolved   |
| `global` | Public traffic proxied except configured direct exceptions                      | Queries use proxied Google except direct exceptions                           |

The exact deployment host and configured `deployment.direct_domain_suffixes`
remain direct in every profile. A suffix covers its apex and subdomains; the host
alone does not grant an exception to its parent domain. These exceptions precede
AI, AdGuard, Clash overrides, and geographic rules, while DNS interception and
transport restrictions still apply. Exception DNS queries and direct outbound
resolution use the system resolver in `ai` and direct Aliyun in other profiles.
Mobile exceptions precede FakeIP rules.

On Linux the `ai` route reaches the system resolver through systemd-resolved's
stub address, which is what `/etc/resolv.conf` points every other program at.
sing-box's own `local` transport reads the resolver configuration and queries
the link's upstream servers itself, binding the query socket to that link; a
kernel that requires `CAP_NET_RAW` for that bind, Ubuntu 18.04's among them,
fails every lookup in the unprivileged user service with `operation not
permitted`, which leaves the route unable to resolve any direct destination
while proxied ones keep working. Machines where systemd-resolved is not running
cannot use `ai`: the installer says so, and `proxy route ai` refuses.

`china` remains the installation default. It proxies unknown destinations;
`gfw` sends them directly. GFWList and AI matches take precedence over China
matches. China domain decisions precede the combined IPv4/IPv6 China IP set;
unknown hostnames are not resolved just to test their IPs against that set.
Older installations without `config-gfw.json` must be reinstalled before switching
to `gfw`; the command refuses before changing configs or services.

In every desktop route, a reviewed catalog shipped inside the config covers OpenAI,
Anthropic, and the sign-in, captcha, and OAuth hosts they check for a matching
egress address; a bundled AI geosite snapshot adds the remaining vendors and
refreshes itself between releases. AI destinations are matched before the AdGuard
filter in all four routes, so a filtered feature-flag host an AI service depends on
still reaches the vendor.

Private addresses remain direct in every route. DNS hijacking, AdGuard filtering,
encrypted-DNS/QUIC/STUN rejection, and Clash Direct/Global overrides also remain
enabled. A switch validates all installed protocol variants before changing anything,
then fetches one URL through the selected variant to prove the route resolves and
reaches the network, because a config can pass validation, start, and listen while
every lookup it makes fails. The probe runs before any service is stopped, in its
own working directory so it never waits on the cache file a running service holds.
If the candidate route fails the probe and the current route passes it, nothing is
changed. If both fail, the machine is treated as offline and the switch proceeds
with a warning. A switch then restarts only services that were already active, and
the restart is accepted only after the mixed proxy is listening locally. If
validation, copying, persistence, or readiness fails, the previous configs,
selection, and service state are restored.

Route changes are refused unless Linux TUN mode is safely off. While TUN is active,
`proxy route` and `proxy check route` still show the saved mixed-mode selection and
note that the active TUN route is fixed/global.

### Service Management

These commands only manage the detected user-scoped `sing-box` client service.
They do not touch shell environment variables, Git proxy config, or GNOME desktop proxy settings.

```bash
# Enable and start the detected user service
proxy service on

# Disable and stop the detected user service
proxy service off
```

### TUN Mode

`proxy on` points the shell, Git, and the desktop at a local mixed inbound. Programs
that ignore those settings, including ones that resolve DNS themselves, bypass it.
TUN mode captures traffic at the network layer instead, so applications do not have
to opt in.

```bash
proxy tun on
proxy tun on --persist
proxy tun off
proxy check tun
```

The trade-off is privilege. The normal client is user-scoped and never runs as root;
the tunnel is machine-wide and needs `CAP_NET_ADMIN`. `proxy tun on` therefore prompts
for sudo, and the sing-box process it starts runs as a dedicated locked `sing-box-tun`
system account with that one capability, from root-owned files outside your home
directory:

| Path                                               | Purpose                              |
| -------------------------------------------------- | ------------------------------------ |
| `/usr/local/libexec/sing-box-manager-tun/sing-box` | privileged copy of the client binary |
| `/etc/sing-box-manager/tun/config.json`            | active TUN config                    |
| `/etc/sing-box-manager/tun/manifest`               | ownership and provenance metadata    |
| `/etc/systemd/system/sing-box-manager-tun.service` | the singleton system unit            |
| `/var/lib/sing-box-manager-tun/`                   | sing-box runtime state               |

Things worth knowing:

- **One tunnel, one owner.** The TUN changes routing for every local user, so there
  is a single service and a single owning UID. Another user gets an error rather than
  taking it over; that user runs `proxy tun off` first.
- **It does not survive a reboot unless you ask.** `--persist` enables the unit at
  boot, and only after the tunnel has been verified. Re-syncing or switching protocol
  keeps whatever persistence you already had.
- **Mixed mode is blocked while it is up.** `proxy on`, `proxy service on`, and every
  component `on` command refuse until the tunnel is safely off, `--force` included.
  Pointing applications at the mixed inbound under TUN would send them to an inbound
  that is not running.
- **`proxy tun off` is a toggle, not an uninstall.** It stops the tunnel and leaves the
  provisioned files in place. It does not restore mixed mode; run `proxy on` for that.
- **Other shells keep their old settings.** One shell cannot rewrite environment
  variables another already inherited, and repository-local Git settings elsewhere on
  disk cannot be enumerated. Open a new shell, or run the matching `proxy ... off`
  command in the affected repository.
- **Native Linux only, for now.** WSL2, containers, macOS, Windows, and remote (SSH)
  sessions are refused rather than partially supported.

`proxy check tun` reports capability, unit state, ownership, and whether the managed
files still match their recorded hashes. It never prompts for a password just to print
a field: root-only provenance appears only when sudo is already unlocked.

Two rows in that report are easy to misread:

- **`Start at boot: no`** after a successful `proxy tun on` is expected. It answers
  "does this survive a reboot", not "is the tunnel up" — that is `Status`. Use
  `proxy tun on --persist` if you want it back after a restart.
- **`Profile auto_redirect: off`** is a property of the shipped Linux profile, not
  something that failed to switch on. `auto_redirect` is sing-box's nftables-based
  transparent redirect; leaving it off is what keeps nftables from being a hard
  requirement. `auto_route`, which is what actually captures your traffic, is on.

While the tunnel is up, `Active protocol` (from the manifest) is what is running.
`Selected protocol` is only what the next activation would use, so the two differ
whenever a switch is pending.

Reinstalling from the portal refuses while the tunnel is up. Stop it first:

```bash
proxy tun off
```

An inactive TUN installation survives a reinstall, and the next `proxy tun on` re-stages
the new binary and config by content hash.

### Shell-Local Proxy Management

These commands only affect the current shell session.
`proxy env ...` is an alias of `proxy shell ...`.

```bash
# Enable proxy for the current shell only
proxy shell on

# Enable proxy for the current shell even if no active sing-box service is detected
proxy shell on --force

# Disable proxy for the current shell
proxy shell off

# Alias: same as proxy shell on/off
proxy env on
proxy env off
```

### Git Proxy Management

```bash
# Enable Git global proxy
proxy git on

# Disable Git global proxy
proxy git off

# Enable Git local proxy for the current repository
proxy git on --local

# Disable Git local proxy for the current repository
proxy git off --local

# Force enable even if no active sing-box service is detected
proxy git on --force
proxy git on --local --force
```

`proxy git ... --local` must be run inside a Git repository.

### Desktop Proxy Management

These commands manage GNOME desktop proxy settings through `dconf`.
On non-GNOME desktops, they warn and do nothing.

```bash
# Enable GNOME desktop proxy settings
proxy desktop on

# Enable desktop proxy settings even if no active sing-box service is detected
proxy desktop on --force

# Disable GNOME desktop proxy settings
proxy desktop off
```

### Docker Proxy Management

```bash
# Enable Docker daemon and build proxy
proxy docker on

# Enable Docker daemon and build proxy even if no active sing-box service is detected
proxy docker on --force

# Disable Docker daemon and build proxy
proxy docker off
```

`proxy docker on` now configures both:

- the system Docker daemon through `/etc/systemd/system/docker.service.d/`, so daemon operations such as `docker pull` can reach the network
- the Docker client config at `${DOCKER_CONFIG:-$HOME/.docker}/config.json`, so new `docker build` and `docker run` commands inherit proxy environment variables and predefined build args automatically

`jq` is required to update the Docker client JSON config. If it is missing, `proxy docker ...` prints install hints, including `sudo apt install jq` on Ubuntu. Rootless Docker daemon management is still out of scope for this first-pass client flow.

### Diagnostics

```bash
# Run the common checks: system, service, shell, public IP, China-friendly public IP, private IP
proxy check

# Show runtime, desktop, support level, and resolved proxy target
proxy check system

# Show detected protocol and sing-box service status
proxy check service

# Show the saved mixed-mode routing strategy
proxy check route

# Show proxy-related environment variables for the current shell
proxy check shell

# Alias: same as proxy check shell
proxy check env

# Show Git proxy settings (global, and local when inside a Git repository)
proxy check git

# Show GNOME desktop proxy settings
proxy check desktop

# Show the VPS bandwidth allowance and this user's usage
proxy check quota

# Compare the installed client package with the portal's current build
proxy check update

# Check public IP through ipinfo.io
proxy check ip

# Check public IP through cip.cc
proxy check ip cn

# Show private/LAN IP
proxy check ip private
```

### Client upgrades

The shell helper checks for a newer client package at most once per day. With the
portal's `suggested` policy, it prints one hint for each newly offered build. With
`required`, it prints a prominent warning in every new shell until you upgrade; the
warning is advisory and does not disable any command. Run `proxy check update` for
the details behind a notice.

Upgrade from the configured portal with:

```bash
proxy upgrade
```

The upgrade refuses to run while TUN mode is active. On a portal whose release carries
`sbc`, it moves the machine to `sbc` with the same port, route and protocol, then
removes this client; a machine with TUN mode set up keeps this client. Docker settings
keep working on the same port, and the GNOME proxy comes back through
`sbc desktop on`.
`proxy check update` bypasses the daily cache when you want to check immediately.

The `VPS` section of `proxy check quota` needs no credentials. The `User` section authenticates with the saved machine token at `${XDG_CONFIG_HOME:-~/.config}/sing-box/portal-token` when available, or with `SBM_TOKEN`. If no token is available, set both `SBM_USERNAME` and `SBM_PASSWORD` for that shell session.

The `User` section prints two different things, and the difference is deliberate:

```
User
  Cycle         2026-09-03 ~ 2026-10-02
  Upload        12.40 GiB
  Download      98.10 GiB
  Total         110.50 GiB
  Plan usage    221.00 GiB / 1000.00 GiB (22.1%, x2 relay)
  Last updated  2026-09-09 14:30
```

`Upload`, `Download`, and `Total` are what the server actually measured between you and
it, so they should match what your own machine reports. `Plan usage` is how much of the
VPS bandwidth plan that traffic consumes, which is roughly double. Every byte crosses the
VPS twice, once between you and the VPS and once between the VPS and the site you are
reaching, and the host bills both. The `x2 relay` note names the factor being applied.
Compare `Plan usage` against the `VPS` section's quota, not `Total`.

`jq` is also required for the JSON-backed diagnostics: `proxy check`, `proxy check ip`, and `proxy check quota`. Those commands print the same install hints when `jq` is missing.

### Advanced Usage

```bash
# Verbose output
VERBOSE=true proxy check

# Public IP check with a custom timeout in seconds
proxy check ip 5

# China-friendly public IP check with a custom timeout in seconds
proxy check ip cn 5
```

## Environment Support

The helper script adapts to these environments:

- Native Linux with GNOME: full support
- Native Linux without GNOME: shell, Git, and Docker proxy management still work; desktop proxy commands warn and no-op
- WSL2: uses `127.0.0.1` as the proxy target. A proxy running on Windows is
  reachable only with mirrored networking (`networkingMode=mirrored` in
  `.wslconfig`), because it listens on loopback only
- Docker: uses `host.docker.internal` as the proxy target. The host's proxy listens
  on loopback only, so this works where Docker forwards that name to the host's
  loopback, such as Docker Desktop, and not through Linux Docker Engine's bridge
- Chinese locale: automatically switches terminal messages to Chinese for `zh_CN`

## Troubleshooting

If the proxy does not work after `proxy on`:

1. Wait a few seconds for the client service to settle.
1. Run `proxy check` for the quick summary.
1. Run `proxy check system` to confirm the detected runtime, desktop environment, and proxy target.
1. Run `proxy check service` to inspect the detected protocol and service state.
1. Run `proxy check shell` to confirm your shell is exporting proxy variables.

`proxy on` starts the detected user-scoped `sing-box` client service before applying proxy settings on native Linux.
If no installed client service can be detected, or if you are using the component-level enable commands, use `-f` or `--force` with `proxy on`, `proxy shell on`, `proxy git on`, `proxy desktop on`, or `proxy docker on` to apply proxy settings anyway.

`--force` does not override the TUN guard. If those commands refuse with "machine-wide
TUN mode is not safely off", run `proxy check tun` to see why, then `proxy tun off`.
A failed `proxy tun off` is an error, not a warning: the tunnel is left in place and
`proxy check tun` reports the state to recover from.

## Uninstallation

Run `proxy uninstall` from any directory. The installed copy of the uninstaller
works after you delete the downloaded archive. It lists the removal targets and
asks for confirmation. Use `proxy uninstall --yes` to skip the prompt, or
`proxy uninstall --help` for all options.

The command also removes global Git and Docker proxy values that match this
client's endpoint, and clears matching variables from the current shell. It
preserves unrelated settings. On Linux, it removes the Docker service override
only if the whole file still matches the client configuration, then restarts
Docker. Cleanup of Docker JSON requires `jq`.

Configuration removal includes saved portal credentials, route and port choices,
and DNS caches. After success, the command removes `proxy` and its completion
from the current shell. Other open shells retain their environment variables;
close them or clear those variables separately. Repository-local Git settings
also need separate cleanup with `proxy git off --local` before uninstalling.

Older installations without this command can still run `./client-uninstall.sh`
from the extracted archive. That script removes files and services but does not
clear the calling shell, Git, or Docker settings.

Inspect the detected client protocol first:

```bash
proxy check service
```

If you installed proxy helpers into a non-default RC file, pass the same override during cleanup:

```bash
proxy uninstall --shell bash
```

By default, the uninstaller removes the user-scoped client service, the XDG install directories, and any legacy system-scoped client install it finds:

```bash
proxy uninstall
```

It also reverts the GNOME desktop proxy, so you are not left pointing at a client that no longer exists. Everything it is about to remove is listed before it asks for confirmation.

The desktop proxy is only reverted when it points at this client's mixed inbound. If it points anywhere else, the uninstaller leaves it alone and says so, since clearing a setting it did not create would be worse than leaving one behind. As with `proxy desktop off`, only the proxy mode is set back to `none`; the configured hosts stay, so re-enabling later does not mean re-entering them.

If you skip the shell cleanup, remove the lines between the following markers from `~/.bashrc` and `~/.zshrc` manually:

```bash
# Network proxy management configuration (sing-box)
# and the next empty line
```
