# Sing-box VPN Client for macOS

> [!NOTE]
> This guide covers the bash client, whose command is `proxy`. Releases no longer
> pack it, so a new Mac installs `sbc` with the
> [hosted one-liner](#hosted-portal-install). The guide stays for Macs that still run
> the bash client.

## Requirements

- macOS 10.15 Catalina or newer
- Apple Silicon (`darwin-arm64`) or Intel (`darwin-amd64`)
- No Homebrew needed to install

The installer runs on the stock `/bin/bash`, so nothing has to be installed first.

## Installation

> [!TIP]
> The installer will:
>
> 1. Detect your login shell and wire `proxy` into `~/.zshrc` or `~/.bash_profile`
> 1. Remove any previous client install first
> 1. Clear the Gatekeeper quarantine flag from the `sing-box` binary
> 1. Install the selected `sing-box` client as a user `LaunchAgent`
> 1. Print the exact shell RC changes applied during install

```bash
./client-install.sh
```

If you downloaded the archive through a browser, macOS marks the extracted files
as quarantined. The installer clears this for the binary automatically. If Finder
still refuses to open the folder, clear it for the whole directory first:

```bash
xattr -dr com.apple.quarantine ./sing-box
```

### Hosted Portal Install

The portal's one-liner installs `sbc`, the client that replaces this one:

```bash
curl -fsSL "https://vpn.example.com/install.sh" | sh
```

On a Mac with this client installed, the one-liner and `proxy upgrade` both move it
to `sbc` and remove this client; see
[Subscription links](RELEASE_AND_PORTAL.md#subscription-links).

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

### Supported Options

- `-h, --help` - Display help messages
- `-V, --verbose` - Enable debug logging
- `-p, --protocol PROTOCOL` - Specify protocol (`trojan`, `hysteria2`, or `naive`)
- `--shell SHELL` - Target a specific RC file (`bash` or `zsh`)
- `--portal-base-url URL` - Persist the portal base URL for `proxy check quota`
- `--no-rc` - Skip shell RC file configuration

## Install Layout

Configuration, data, and state use the same XDG-style paths as the Linux client.
Only the service definition and logs use Apple-specific locations, because
`launchd` will not look anywhere else.

- binary: `~/.local/bin/sing-box`
- config: `${XDG_CONFIG_HOME:-~/.config}/sing-box/<protocol>/config.json`
- route variants: `${XDG_CONFIG_HOME:-~/.config}/sing-box/<protocol>/config-{china,gfw,ai,global}.json`
- helper scripts: `${XDG_DATA_HOME:-~/.local/share}/sing-box/`
- state: `${XDG_STATE_HOME:-~/.local/state}/sing-box`
- LaunchAgent: `~/Library/LaunchAgents/io.sing-box.<protocol>.plist`
- logs: `~/Library/Logs/sing-box/<protocol>.log`
- selected protocol: `${XDG_CONFIG_HOME:-~/.config}/sing-box/selected-protocol`
- selected route: `${XDG_CONFIG_HOME:-~/.config}/sing-box/selected-route`
- selected port: `${XDG_CONFIG_HOME:-~/.config}/sing-box/selected-port`
- installed client build: `${XDG_DATA_HOME:-~/.local/share}/sing-box/client-version`
- installer preferences: `${XDG_DATA_HOME:-~/.local/share}/sing-box/install-preferences`

Installation and reinstallation select `china`. `config.json` is the active copy;
the route variants stay beside it for local switching.

The installer checks the packaged `rules/files.txt` inventory and its files before
removing an existing installation, then copies them into the state directory's
`rules/` subdirectory. These files allow the desktop proxy to start when background
rule downloads fail.

Desktop DNS caches use `cache-<protocol>-<route>.db` in the same state directory,
which is accessible only to the installing user. LaunchAgents use a private file
creation mask. Saved answers survive service restarts until their normal DNS TTL
expires, and each protocol and route has its own cache. Reinstall and uninstall
clear these caches. Route validation uses the same `-D` state directory as the
LaunchAgent, including when custom XDG paths are configured.

If your client package includes more than one allowed protocol, the installer
writes a LaunchAgent for each one and starts only the selected protocol.

## Service Lifetime

A `LaunchAgent` starts when you log in and stops when you log out. This differs
from the Linux client, where `loginctl enable-linger` can keep the service alive
after logout. There is no user-level equivalent on macOS — running before login
or after logout requires a root `LaunchDaemon`, which this client does not
install.

For a Mac that stays logged in, the practical behavior is the same: the client
comes back automatically after a reboot once you log in.

## Usage

After installation, all client-side shell commands go through `proxy`. Open a new
terminal, or re-source your shell RC file, if the command is not found right
after install.

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

`proxy on` applies the composite profile: the `sing-box` LaunchAgent, the current
shell's proxy environment variables, Git global proxy config, and the macOS
system proxy.

```bash
# Enable global proxy
proxy on

# Enable even if no active sing-box service is detected
proxy on --force

# Pin the mixed inbound to a specific port instead of auto-detecting one
proxy on --port 1085

# Disable global proxy
proxy off
```

The chosen port is recorded in `selected-port`, alongside `selected-route` and
`selected-protocol`. Reinstalling rewrites the route configs from the downloaded
package, which would otherwise reset the inbound to 1080; the marker is what
carries your port across an upgrade, and the next `proxy on` writes it back into
the configs and restarts the client if the port moved.

The mixed inbound listens on `127.0.0.1` only, so other devices on the network
can't use it as a proxy.

Use `proxy port` to display that saved selection and `proxy port <PORT>` to
change it. Ports must be decimal integers from `1024` through `65535`, and both
TCP and UDP must be free. The port already served by this client is accepted as
a no-op; a port occupied by another process is rejected before anything changes.

An active LaunchAgent is restarted immediately on the new port. When no client
is running, the selection and every installed protocol/route config are updated
without starting a LaunchAgent. Currently enabled, client-owned shell, Git,
system-proxy, and Docker client settings move with the port; disabled and
third-party-owned settings remain untouched. Any write, restart, readiness, or
integration failure restores the previous port and active-service state.
`proxy on --port <PORT>` uses the same checks and transaction.

> [!IMPORTANT]
> The system proxy step runs `networksetup`, which requires administrator
> rights, so `proxy on`, `proxy desktop on`, and `proxy port <PORT>` when the
> system proxy is currently enabled may prompt for your password. If
> you would rather not grant that, use `proxy service on` plus `proxy shell on`
> — neither needs privileges, and together they cover most command-line work.

### System Proxy Management

These commands manage the macOS system proxy through `networksetup`, applied to
the network service that currently holds the default route — usually `Wi-Fi` or
a USB Ethernet adapter.

```bash
proxy desktop on
proxy desktop on --force
proxy desktop off
```

`proxy check desktop` prints the resolved network service name alongside the
current HTTP, HTTPS, and SOCKS settings. Check it first if the system proxy seems
to apply to the wrong interface: with a VPN or a virtualization adapter active,
the default route may not be on the network service you expected. The same state
is visible in **System Settings → Network → \<service\> → Details → Proxies**.

### Service Management

These commands only manage the `sing-box` LaunchAgent. They do not touch shell
environment variables, Git config, or system proxy settings, and need no
password.

```bash
proxy service on
proxy service off
```

### Protocol Switching

If your client package includes more than one allowed protocol, use `proxy
protocol` to switch which one runs.

```bash
proxy protocol hysteria2
proxy protocol naive
proxy protocol trojan
```

Switching is local and authorization-preserving: it only works for protocol
configs already installed under
`${XDG_CONFIG_HOME:-~/.config}/sing-box/<protocol>/config.json`. If it reports
that a protocol is not installed, reinstall from the portal after your account
has access to it.

### Routing Strategies

The saved route applies to every installed protocol, so `proxy protocol` preserves it.
Use the bare command to report the current selection or choose a strategy:

```bash
proxy route
proxy route china
proxy route gfw
proxy route ai
proxy route global
proxy check route
```

| Route    | Application traffic                                                             | DNS                                                                            |
| -------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `china`  | China destinations direct; other public traffic proxied                         | China queries use Aliyun directly; other queries use Google through the proxy  |
| `gfw`    | GFWList and AI destinations proxied; other traffic direct                       | GFWList and AI queries use proxied Google; other queries use direct Aliyun     |
| `ai`     | AI services proxied, with their sign-in and captcha hosts; other traffic direct | AI queries use Google through the proxy; other queries use the system resolver |
| `global` | Public traffic proxied except configured direct exceptions                      | Queries use proxied Google except direct exceptions                            |

The exact deployment host and configured `deployment.direct_domain_suffixes`
remain direct in every profile. A suffix covers its apex and subdomains; the host
alone does not grant an exception to its parent domain. These exceptions precede
AI, AdGuard, Clash overrides, and geographic rules, while DNS interception and
transport restrictions still apply. Exception DNS queries and direct outbound
resolution use the system resolver in `ai` and direct Aliyun in other profiles.
Mobile exceptions precede FakeIP rules.

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

All four keep private destinations direct, hijack DNS, filter with AdGuard, reject
DoT/QUIC/STUN, and honor Clash Direct/Global overrides. Switching validates every
installed protocol variant, fetches one URL through the selected variant to prove the
route resolves and reaches the network, updates all active configs together, and
restarts only the LaunchAgents that were running. The probe runs before anything is
stopped; a candidate that fails it while the current route passes is refused, and if
both fail the machine is treated as offline and the switch proceeds with a warning. A
restart is accepted only after the mixed proxy is listening locally. Any failure
restores the previous route and active service set.

### Shell-Local and Git Proxy Management

Identical to Linux. `proxy env ...` is an alias of `proxy shell ...`.

```bash
proxy shell on
proxy shell off

proxy git on
proxy git off
proxy git on --local
proxy git off --local
```

`proxy git ... --local` must be run inside a Git repository.

### Docker Proxy Management

```bash
proxy docker on
proxy docker off
```

On macOS this configures only the Docker **client** config at
`${DOCKER_CONFIG:-$HOME/.docker}/config.json`, so new `docker build` and
`docker run` commands inherit proxy environment variables and predefined build
args.

The Docker **daemon** runs inside Docker Desktop's own Linux VM, and there is no
systemd drop-in to write. Configure it in **Docker Desktop → Settings →
Resources → Proxies** if `docker pull` needs to go through the proxy. `proxy
docker on` prints this reminder on macOS.

`jq` is required to update the Docker client JSON config.

### Diagnostics

```bash
proxy check              # system, service, shell, public IP, CN IP, private IP
proxy check system       # runtime, desktop, support level, resolved proxy target
proxy check service      # detected protocol and LaunchAgent status
proxy check route        # saved mixed-mode routing strategy
proxy check shell        # proxy-related environment variables
proxy check git          # Git proxy settings
proxy check desktop      # resolved network service and its proxy settings
proxy check quota        # VPS bandwidth allowance and this user's usage
proxy check update       # compare this client package with the portal
proxy check ip           # public IP through ipinfo.io
proxy check ip cn        # public IP through cip.cc
proxy check ip private   # private/LAN IP
```

### Client upgrades

The shell helper checks for a newer client package at most once per day. The portal's
`suggested` policy prints one hint for each newly offered build. `required` prints a
prominent warning in every new shell until you upgrade, but does not block commands.
Run `proxy check update` for the details behind a notice.

```bash
proxy upgrade
```

On a portal whose release carries `sbc`, the upgrade moves the Mac to `sbc` with the
same port, route and protocol, then removes this client. Docker settings keep working
on the same port, and the macOS network proxy comes back through `sbc desktop on`.
`proxy check update` checks immediately without the daily cache.

The `VPS` section of `proxy check quota` needs no credentials. The `User` section
authenticates with the saved machine token at
`${XDG_CONFIG_HOME:-~/.config}/sing-box/portal-token`, or with `SBM_TOKEN`. If no
token is available, set both `SBM_USERNAME` and `SBM_PASSWORD` for that shell
session.

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

`Upload`, `Download`, and `Total` are what the server actually measured between you
and it, so they should match what your own machine reports. `Plan usage` is how much
of the VPS bandwidth plan that traffic consumes, which is roughly double. Every byte
crosses the VPS twice, once between you and the VPS and once between the VPS and the
site you are reaching, and the host bills both. The `x2 relay` note names the factor
being applied. Compare `Plan usage` against the `VPS` section's quota, not `Total`.

`jq` is required for `proxy check`, `proxy check ip`, and `proxy check quota`.
Those commands print `brew install jq` as a hint when it is missing.

## Inspecting the Service

The client is an ordinary launchd user agent, so `launchctl` works on it
directly:

```bash
# Is it running?
launchctl print "gui/$(id -u)/io.sing-box.trojan"

# Follow the log
tail -f ~/Library/Logs/sing-box/trojan.log

# Stop until next login
launchctl bootout "gui/$(id -u)/io.sing-box.trojan"

# Start again
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/io.sing-box.trojan.plist

# Restart
launchctl kickstart -k "gui/$(id -u)/io.sing-box.trojan"
```

## Troubleshooting

### "sing-box" cannot be opened because the developer cannot be verified

Gatekeeper quarantined the binary. The installer clears this automatically; if
you are running the binary by hand:

```bash
xattr -d com.apple.quarantine ~/.local/bin/sing-box
```

### The service will not stay running

```bash
launchctl print "gui/$(id -u)/io.sing-box.<protocol>"
```

Look for `state = running` and a non-zero `last exit code`. The log file usually
names the cause — a malformed config, or the configured port already bound by
something else.

### System proxy applied to the wrong network service

Run `proxy check desktop` to see which service was resolved. If a VPN or
virtualization adapter holds the default route, turn it off, then run
`proxy desktop off` followed by `proxy desktop on`.

### The installer says launchd was not found

`launchctl` is missing from `PATH`. This should never happen on a real macOS
install; check that `/bin` and `/usr/bin` are on your `PATH`.

## Uninstallation

Run `proxy uninstall` from any directory. The installed copy of the uninstaller
works after you delete the downloaded archive. It lists the removal targets and
asks for confirmation. Use `proxy uninstall --yes` to skip the prompt, or
`proxy uninstall --help` for all options.

The command also removes global Git and Docker proxy values that match this
client's endpoint, and clears matching variables from the current shell. It
preserves unrelated settings. Cleanup of Docker JSON requires `jq`.

Configuration removal includes saved portal credentials, route and port choices,
and DNS caches. After success, the command removes `proxy` and its completion
from the current shell. Other open shells retain their environment variables;
close them or clear those variables separately. Repository-local Git settings
also need separate cleanup with `proxy git off --local` before uninstalling.

Older installations without this command can still run `./client-uninstall.sh`
from the extracted archive. That script removes files and services but does not
clear the calling shell, Git, or Docker settings.

```bash
proxy uninstall
```

This unloads and removes the LaunchAgents, removes the install directories and
`~/Library/Logs/sing-box`, cleans up the shell RC entries, and reverts the macOS
system proxy.

Everything it is about to remove is listed before it asks for confirmation, so
you can see whether the system proxy is included.

The system proxy is only reverted when it points at this client's mixed inbound.
If it points anywhere else — a corporate proxy, or another
local tool — the uninstaller leaves it alone and says so, since clearing a
setting it did not create would be worse than leaving one behind. Reverting it
needs administrator rights, so the uninstaller may prompt for your password; if
you decline, it warns and finishes the rest of the cleanup.

If you installed proxy helpers into a non-default RC file, pass the same override
during cleanup:

```bash
proxy uninstall --shell bash
```

If you skip the shell cleanup, remove the lines between the following markers
from `~/.zshrc` or `~/.bash_profile` manually:

```bash
# Network proxy management configuration (sing-box)
# and the next empty line
```
