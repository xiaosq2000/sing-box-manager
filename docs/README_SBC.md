# sbc client CLI

Checked on 2026-10-20.

`sbc` is the Go client for Linux, macOS, and Windows. It manages sing-box,
fetches subscription configs, and controls shell and desktop proxy settings.

Install it with the [hosted installer](RELEASE_AND_PORTAL.md#one-liner-install).
For Windows requirements and service details, see [the Windows guide](README_WIN.md).
The legacy Unix client implementation is not maintained in this repository.

## Unix installation and migration

Linux and macOS users install `sbc` with:

```sh
curl -fsSL "https://vpn.example.com/install.sh" | sh
```

The installer asks for the subscription link without echoing it. Do not put the
link in shell history or command arguments. The first executable download trusts
HTTPS. `sbc` checks the signed manifest for later binary and rule downloads.

The installer accepts `--shell bash|zsh`, `--no-rc`, and `-p PROTOCOL`.
Open a new terminal after installation. Run `sbc status` to check the client.
See [command help](#command-help) for service and proxy controls.

| Platform | Private config directory             | Service              |
| -------- | ------------------------------------ | -------------------- |
| Linux    | `~/.config/sbc/`                     | systemd user service |
| macOS    | `~/Library/Application Support/sbc/` | LaunchAgent          |

An installed Bash client can migrate through this installer or `proxy upgrade`.
The hosted `/install/linux.sh` and `/install/macos.sh` aliases remain available.
The installer checks the new proxy before removing the old client. It preserves
port, route, protocol, and installer preferences. It uses the old client's
installed uninstaller and desktop library, not repository copies.
See [the migration procedure](RELEASE_AND_PORTAL.md#subscription-links) for
failure recovery and settings ownership.

## Linux TUN

`sbc tun on` runs a root-owned sing-box TUN service (`sbc-tun.service`, interface
`sbc-tun0`) on native Linux. It needs a running systemd and `/dev/net/tun`; it
refuses WSL2, containers, SSH sessions, and machines with other login accounts.
The command asks for sudo. Root owns `/etc/sbc/tun`, `/etc/sbc/tun-control`,
`/var/lib/sbc-tun`, the staged binary and the unit; the control directory keeps
transaction metadata without API credentials. The service runs as `sbc-tun` with
`CAP_NET_ADMIN` only. The subscription's `config_tun` supplies the TUN config,
and `sbc` adds the local API, cache and absolute rule snapshot paths.
Root patches remote rule sets into local, read-only snapshots. The service does
not download rules. `sbc tun on` stages the installed snapshots again. One user owns the
TUN on a machine; another user's `sbc tun on` is refused.

TUN answers DNS through its route rules and never uses the systemd-resolved stub.
Mixed mode and TUN never run together: `sbc on`, `sbc desktop on` and
`sbc docker on` refuse while TUN is active, and the mixed user unit has a
condition guard. `sbc install`, `sbc uninstall` and `sbc upgrade` also refuse
while TUN is active; run `sbc tun off` first. The service is enabled at boot only
after activation checks pass. `sbc tun off` stops and disables the service and
leaves the mixed proxy off; run `sbc on` to restore it. There is no fail-open: if
the proxy fails, captured traffic has no exit, so run `sbc tun off` at the machine.

Activation requires a captured HTTPS request under the current route mode.
The check never changes that mode. `direct` does not require an unused proxy;
the other modes also require the selected proxy to reach the latency-test URL.
The current-route request uses `www.baidu.com`, or `www.gstatic.com` in `global`.
It uses system DNS, validates TLS, ignores proxy variables, and does not follow
redirects. No activation probe contacts an AI provider. These probes check
representative paths, not every destination or rule.

Root prepares a complete candidate before changing live files. Superseded
config and binary directories remain beside the live directories with `.prev-`
suffixes. They can contain credentials and consume disk space. There is no
automatic garbage collection. No failed operation automatically starts an older
TUN or the mixed proxy.

The user saves the local API state before root commits and enables the service
at boot. Until commit, an independent root timer can stop the pending generation
after three minutes. A pending generation cannot start after reboot without its
recovery timer. A failed user-state write or commit requests a confirmed stop.

Stop checks the service state, main process, interface, routes and nftables table.
If it cannot confirm cleanup, sbc retains a persistent mixed-mode block and marks
the TUN as dirty. `sbc status` reports `tun: dirty` and exits 1. Run `sbc tun off`
at the machine to retry recovery. Pending and dirty state also block install,
uninstall and upgrade. Do not delete markers to bypass this protection.

Android and iOS apps manage their own tunnels and are not affected.

The hosted installer refuses when a legacy TUN unit, transaction marker, or
unsafe status remains. This check also applies when user-scoped client files
are missing. It changes no files and performs no download in that case.
Use the installed legacy client's cleanup tools before installing `sbc`.
The installer does not remove privileged TUN state automatically. For the native
TUN service, run `sbc tun off` before uninstalling; `sbc uninstall` refuses while it
is active.

## Command help

```sh
sbc help
sbc install --help
sbc help speed
```

Help works without an installation. It does not read a subscription link,
contact a server, or perform the command's action. Messages follow `SBC_LANG`,
a saved choice, and the locale, in English or Simplified Chinese. Command and
flag names stay in English.

The CLI rejects unknown flags and extra arguments before performing an action.
Use `refresh` for configuration and `upgrade` for binaries. The public `update`
command is removed. Other commands retain their syntax and meaning.

| Command                                   | Purpose                                                         |
| ----------------------------------------- | --------------------------------------------------------------- |
| `sbc install`                             | Read a subscription link from stdin and install the client      |
| `sbc init bash`, `sbc init zsh`           | Print shell integration code                                    |
| `sbc link`, `sbc link set`                | Show the masked link, or replace it from stdin                  |
| `sbc on`, `sbc off`                       | Switch shell and opted-in desktop proxies                       |
| `sbc env`, `sbc env off`                  | Print proxy variables for scripts or the current shell          |
| `sbc desktop [on\|off]`                   | Show or switch desktop proxy settings                           |
| `sbc privacy`                             | Show browser and region privacy choices                         |
| `sbc privacy browser [on\|off]`           | Show or change Chrome WebRTC protection (Linux)                 |
| `sbc privacy region [on\|off]`            | Show or match the node's client time zone and language (Linux)  |
| `sbc tun on`, `sbc tun off`               | Enable or disable Linux TUN capture                             |
| `sbc lang [en\|zh\|auto]`                 | Show or set the language of sbc messages                        |
| `sbc docker [on\|off] [--yes]`            | Show or switch the Linux Docker daemon's proxy                  |
| `sbc route [name]`, `sbc protocol [name]` | Show or switch a choice without restarting sing-box             |
| `sbc route --list`                        | List installed route strategies and their meanings              |
| `sbc port [number]`                       | Show or change the local proxy port                             |
| `sbc status`, `sbc ip`, `sbc speed`       | Show status, exit location, or latency                          |
| `sbc start`, `sbc stop`, `sbc restart`    | Control the service                                             |
| `sbc run`                                 | Run sing-box in the foreground                                  |
| `sbc refresh`, `sbc upgrade`              | Refresh configuration, or install signed binary updates         |
| `sbc uninstall [--yes]`                   | Clean browser settings, stop the service, and remove the client |
| `sbc completion bash\|zsh\|powershell`    | Print a completion script                                       |
| `sbc version`, `sbc --version`            | Print the build version                                         |

`on` and `off` differ from `start` and `stop`. `off` leaves sing-box running.
Desktop settings follow proxy toggles only after `sbc desktop on`.
Docker changes require confirmation or `--yes` because they restart Docker.

Subscription links are credentials. Pass them through the installer prompt or
stdin, not command arguments. `sbc link set` also reads from stdin.

## CLI language

`sbc lang` shows the effective language and what selected it. `sbc lang en` and
`sbc lang zh` save English or Simplified Chinese. `sbc lang auto` removes the
saved choice and follows `SBC_LANG` and the locale again. The command works
without an installation or a subscription.

The saved choice is in the private config directory: `~/.config/sbc/language`
on Linux, `~/Library/Application Support/sbc/language` on macOS, and
`%LOCALAPPDATA%\sbc\language` on Windows. The order is:

1. `SBC_LANG`, where a value starting with `zh` selects Chinese and any other
   value selects English.
2. The saved choice.
3. The first of `LC_ALL`, `LC_MESSAGES` and `LANG` that is set. Only `zh_CN` and
   `zh_SG` select Chinese.
4. English.

`SBC_LANG` still overrides the saved choice. `sbc lang` shows both when they
differ. A corrupt saved choice falls back to `SBC_LANG` and the locale; run
`sbc lang auto` to remove it.

This setting changes only sbc messages. It does not change the system locale,
the language of other programs, the hosted installer, or the portal. Use
`sbc privacy region` to match the client's system time zone and language to the
node.

## Chrome WebRTC protection

`sbc privacy browser on|off` manages the mandatory
`WebRtcIPHandling=disable_non_proxied_udp` policy for Google Chrome on Linux.
It is off until enabled and works without a subscription or a running proxy.
Only Chrome is managed; Chromium, Edge, Brave, Firefox, macOS and Windows are
not. `sbc privacy` summarizes this choice. See
[WebRTC protection](WEBRTC.md) for ownership, cleanup, limits and manual
cleanup after the scope change.

## Region matching

`sbc privacy region on` makes this Linux machine present the time zone and
language the portal assigns to its node. It is off until enabled. The operator
sets the target in the node's `deployment.client_region`: `timezone` is an IANA
name and `language` is BCP 47 with a region, such as `zh-CN`. The subscription
carries it per node. A portal without that block reports the target as
unavailable; `sbc` does not guess.

By default, region matching changes only the current account. `--system` changes
the whole machine. System scope needs `sudo`, asks for confirmation, and records
the previous settings so `off` or uninstall restores them.

```sh
sbc privacy region              # Show the saved choice and availability
sbc privacy region on           # This account: time zone and language
sbc privacy region on --timezone
sbc privacy region off --language
sbc privacy region on --system  # All users: confirm and sudo
sbc privacy region on --system --yes
sbc privacy region off --system # Restore the previous system settings
```

### User scope

The command writes `~/.config/sbc/region.sh` for the `sbc init` prompt hook,
`~/.config/environment.d/90-sbc-region.conf` for the next login, and updates the
systemd user environment for newly started user services. An open shell picks
up `region.sh` at its next prompt. A desktop session needs a new login; open
browsers and applications need a restart. `off` removes the files and clears the
variables sbc set; it keeps the last profile so `on` does not need a download.

### System scope

`--system` writes `/etc/localtime` and the system locale file. It uses
`timedatectl set-timezone` for the time zone and `update-locale` for the
language on Ubuntu 18.04 to 26.04. If a tool is missing, sbc writes the file
directly. The language applies at the next login; the time zone applies to new
processes immediately. Open applications and browsers keep their current values
until you restart them.

System scope affects every account and service. It can change log timestamps,
cron schedules, and the language of system tools. On a shared machine, it can
match only one node at a time.

The first `on --system` records the previous time zone and locale under
`/var/lib/sbc/region`. `off` restores them. If someone changes a system setting
after sbc, sbc leaves that setting in place and reports it. `sbc uninstall`
restores the settings that sbc still owns, and asks for `sudo` when it does. If the record is damaged, sbc refuses
to guess: restore the time zone and language manually, then remove the record.

You cannot change scope while matching is on. Run `off` first:

```sh
sbc privacy region off
sbc privacy region on --system
```

### Refresh and automation

In user scope, `sbc refresh` updates the saved profile and reapplies it when
the node's region changes. In system scope, it does not call `sudo` and does
not change the system settings. It reports that a new target is
available; run `sbc privacy region on` to apply it. A refresh that temporarily
lacks `client_region` keeps the last profile and system settings. A new
subscription without it marks the system settings for removal; run `sbc privacy
region off`.

`sbc on` uses the cache and does not fetch. Replacing the subscription with one
that has no region turns user-scope matching off. In system scope, it asks you
to run `off` instead.

The language is applied only when its locale is generated. If it is missing,
`on` still applies the time zone, reports the missing locale, and exits 1:

```sh
# Debian and Ubuntu
sudo locale-gen de_DE.UTF-8
# Arch: add `de_DE.UTF-8 UTF-8` to /etc/locale.gen, then
sudo locale-gen
# Fedora: install the language pack, for example
sudo dnf install glibc-langpack-de
```

Then run `sbc privacy region on --language`.

Limits:

- The target is the operator's client-facing choice, not the node's own
  `/etc/localtime` or locale. A node that runs in UTC can tell clients to use
  `Europe/Berlin`.
- A user-set `LC_ALL` overrides `LANG` and `LC_MESSAGES`. `sbc privacy region`
  warns when it sees one. A saved `sbc lang en|zh` controls sbc's own messages;
  run `sbc lang auto` to let region matching change them too.
- Snap and Flatpak browsers can ignore the session environment. Firefox can
  keep an Accept-Language or UI language chosen earlier.
- Turning the proxy off or using `sbc route direct` leaves the region in place,
  so the exit IP and the region disagree. `sbc status` warns.
- Region matching does not change the keyboard layout, input method, fonts, or
  DNS, and it is not a complete anti-fingerprinting measure.
- It is Linux-only. System scope needs `sudo` and a locale that is already
  generated. sbc uses `timedatectl` and `update-locale` when they exist and
  writes the files directly otherwise. It does not generate locales or install
  language packs.
- System scope does not manage `/etc/timezone`. tzdata derives it from
  `/etc/localtime` where the file still exists, and Ubuntu 26.04 removes it.
- System scope is machine-wide. On a shared machine, the last account that runs
  `on --system` owns the setting.
- `sbc uninstall` from any account restores the system settings that sbc still
  owns. A user-scope choice is removed with the account's files.
- A client older than the system scope ignores the saved scope. Run `sbc privacy
region off` before downgrading.

## Automatic maintenance

Configuration refresh and binary upgrades have separate policies. Both default
to **on**, with a **6h** interval. New installations use these defaults. Existing
installations adopt them when the new executable sets up its schedules.
Saved policies, including `off`, survive reinstall and subsequent upgrades.

```sh
sbc refresh                  # Refresh configuration now
sbc refresh auto             # Show on or off
sbc refresh auto off          # Disable periodic configuration refresh
sbc refresh interval         # Show the interval
sbc refresh interval 1h       # Set the interval

sbc upgrade                  # Upgrade sbc and sing-box now
sbc upgrade auto              # Show on or off
sbc upgrade auto off          # Disable automatic binary upgrades
sbc upgrade interval          # Show the interval
sbc upgrade interval 24h      # Set the interval
sbc status                   # Show both policies and recent action results
```

Intervals use duration syntax, such as `30m`, `6h`, or `24h`. They must represent
whole seconds from `1m` through `720h`. Queries change no settings or services.
Changing a policy only changes background scheduling. It does not immediately
refresh configuration, install binaries, or restart the proxy.

Manual actions ignore the automatic switches. An upgrade can adapt configuration
even when automatic refresh is off. Upgrades use the operator's signed release,
not an upstream "latest" release.

Failures to register background tasks or retire legacy tasks produce a warning
during manual `refresh`, `upgrade`, `start`, `restart`, and `run`. These commands
still attempt their requested action. Invalid maintenance settings, failed settings
writes, and failed upgrade recovery remain errors. Policy-setting commands still
fail if scheduling fails, but retain the saved policy. After repairing the scheduler,
repeat the same policy-setting command to retry.

`sbc` verifies downloads and validates candidate configuration before replacing
live files. The staged client must report the signed build version. Restart or
readiness failures trigger restoration of the previous binaries and configuration. Upgrading only `sbc` does
not restart the Unix proxy when its configuration stays unchanged.

An operation lock serializes maintenance, configuration writes, and commands that
start the proxy. A conflicting manual command reports that the client is busy.
A background task waits for up to 10 minutes. Changing a policy does not cancel
an upgrade already in progress.

An engine lock prevents duplicate managed and foreground instances. Unix keeps
this lock across `exec`; Windows retains it in its existing wrapper. Stop a
foreground instance before starting the managed service.

A separate permission lock allows an updater's restart only after its files are
complete. The updater retains the operation lock until readiness finishes.
Every runtime writer passes the permission barrier before checking engine startup.
A startup timeout blocks writes. Failed startup must stop before rollback can
replace files. If shutdown fails, recovery files remain for repair.

The private `maintenance.json` file stores policies and recent action results.
Invalid settings stop maintenance rather than enable defaults. `sbc status` shows
an incomplete attempt as "running or interrupted". An interrupted upgrade keeps
its recovery record and backups. The next maintenance action or proxy start
restores the previous files before proceeding. A failed recovery retains those
files for repair. Policy queries and changes do not perform this recovery.

| Platform | Background jobs                                 | Runtime requirement                                          |
| -------- | ----------------------------------------------- | ------------------------------------------------------------ |
| Linux    | `sbc-config-refresh.timer`, `sbc-upgrade.timer` | systemd user manager, with linger for operation after logout |
| macOS    | `io.sbc.config.refresh`, `io.sbc.upgrade`       | Logged-in user session                                       |
| Windows  | `sbc-config-refresh`, `sbc-upgrade`             | Task Scheduler, with interactive fallback if S4U is refused  |

Intervals are approximate. Linux and Windows spread scheduled starts to reduce
simultaneous downloads. The proxy remains stopped if it was stopped before a
maintenance action. Three zero-byte lock files sit next to the config directory:
`.operation.lock`, `.engine.lock`, and `.startup.lock`, each prefixed with the
config directory's name. They remain after uninstall to prevent lock-file races.
They contain no credentials. File existence never grants startup permission.

### Migration from the fixed refresh task

The new proxy wrapper replaces the old schedules when it starts. A stopped
installation can instead migrate when its old task next invokes the new
executable. Run `sbc refresh` after the first upgrade to install the schedules
without waiting for that task. This does not start a stopped proxy.

A hidden, one-use `update` entry exists only to migrate old task definitions. It
does not act as a configuration-refresh alias. Remove this entry and the old-task
retirement code at the planned device-enrollment clean break, when old client
contracts end. Do not remove them while legacy tasks can still be installed.

The updater records the installed build rather than reusing its running process's
old version. A successful upgrade clears its notification on the first invocation.
Shell initialization and status also discard stale hints written by an older
updater process.

## Route strategies

```sh
sbc route --list
sbc route china-direct
sbc route direct
sbc route --help
```

| Strategy       | Behavior                                                                               | Use                                                   |
| -------------- | -------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| `direct`       | Direct all allowed traffic, including AI. Use system DNS                               | No remote proxy for application traffic               |
| `ai-only`      | Proxy community-listed AI destinations. Direct the rest                                | Only AI needs the proxy                               |
| `gfw-proxy`    | Proxy GFWList and AI destinations. Direct the rest                                     | Reduce proxy traffic, with possible gaps in the lists |
| `china-direct` | Direct recognized China destinations. Proxy the rest. AI and GFW matches take priority | Default, suitable for general use from mainland China |
| `global`       | Proxy allowed traffic except LAN and configured direct exceptions                      | Diagnose a route classification problem               |

`ai`, `gfw`, and `china` remain aliases for `ai-only`, `gfw-proxy`, and
`china-direct`. The CLI displays canonical names, including with an older installed
config. Rendered configs retain alias modes so saved selections survive refreshes
and restarts. Completion suggests canonical names only.

All strategies retain private-network access, configured direct exceptions,
AdGuard filtering, DNS interception, and restrictions on port 853, UDP port 443,
and STUN.
Listed AI hosts take priority over AdGuard, including when they go direct.
`direct` uses system DNS for all destinations, including direct exceptions.
Browser WebRTC settings remain unchanged when a route switches.
A proxy failure does not cause an automatic direct retry.

`route direct` keeps sing-box and its local proxy running. `sbc off` instead
removes our shell and opted-in desktop proxy settings. Only traffic that enters
sing-box follows the selected strategy.

Rule-set maintenance and explicit proxy diagnostics, including `sbc speed`, can
use the remote proxy even on `direct`. A route switch is not a promise to stop
all remote-proxy traffic or to close existing connections.

## Exit IP and AI checks

```sh
sbc ip
sbc ip --ai
# This permits provider requests to expose your direct network IP:
sbc ip --ai --allow-direct-ai
```

Plain `sbc ip` checks ipinfo.io and cip.cc through the local proxy. Each check
follows the active destination rules. It makes no AI provider request.

`--ai` adds one unauthenticated request each to Gemini, ChatGPT, and Claude.
These requests use the existing proxy-only diagnostic inlet, not the active
route's destination rules or direct exceptions. They test the remote proxy, not
whether community rules classify every provider host correctly. The CLI labels
this distinction. `direct` skips them unless `--allow-direct-ai` is supplied.
Missing or unrecognized proxy-only inlets also cause skips. Run `sbc refresh` to
add the inlet to an older managed config. Skips do not count as failures.

`--allow-direct-ai` requires `--ai` and uses the ordinary local proxy inlet.
Requests then follow the active route, including direct exceptions. This flag
permits direct access, but does not force it on a proxy strategy. It does not
authorize a direct retry after a proxy failure.

| Provider | What the result measures                                                            |
| -------- | ----------------------------------------------------------------------------------- |
| Gemini   | A region code in its public start page. The undocumented format can change          |
| ChatGPT  | IP and country from its edge trace, when available. This is not account eligibility |
| Claude   | Public-page reachability, denial, challenge, or redirect. Region remains unknown    |

Checks use no browser cookies, API keys, or account credentials. They do not
follow redirects, retry failures, solve challenges, or change the route.
A successful page request does not prove that a signed-in account can use a
service. These checks cannot guarantee protection from provider risk controls.
A direct request exposes the local network's public IP to the provider.

### Download speed

```sh
sbc speed --all
sbc speed --download
sbc speed --download 50
sbc speed --download=50 --all
```

Without `--download`, the command tests page latency. A bare `--download`
downloads 20 MB. An explicit size must be from 1 to 99 MB. `--all` tests every
available protocol without switching the protocol in use.
Downloads count against the server's transfer allowance.

## Shell completion

Completion supports Bash 3.2 or newer, Zsh, Windows PowerShell 5.1, and PowerShell 7.
Fish is outside this scope.

Completion suggests commands, command-specific long flags such as `--ai`, and
fixed values such as `on` and `off`. Choice-valued flags support both
`--flag value` and `--flag=value`. Boolean flags do not suggest values.
Zsh shows brief route descriptions. PowerShell shows descriptions in tooltips or
a Bash-like list, depending on its completion mode. Standard Bash completion inserts
names only. The same route descriptions appear in `sbc route --help` and
`sbc route --list`.

Completion never performs an IP or AI check.
It reads route and protocol names from the installed config, including values for
`install --route` and `install --protocol`. Completion needs no server request
or running service. Completion does not change the config or print credentials.

Missing, unreadable, invalid, non-regular, or larger-than-8-MiB config files
produce no dynamic choices. Completion suggests only names with letters,
numbers, dots, underscores, or hyphens. Names cannot start with a hyphen.
These restrictions affect suggestions, not the command's accepted choice names.

### Bash and Zsh

The existing `sbc init` line loads completion in interactive shells. After a
client upgrade, open a new shell or evaluate that line again. Completion needs
no additional rc block or `bash-completion` package.

Zsh uses an existing completion system. If no system is active, the emitted
init code runs `compinit` with its normal security checks.
Noninteractive init code does not register completion.

If you do not use `sbc init`, put `sbc` on `PATH` and load completion yourself.
For Bash:

```bash
source <(sbc completion bash)
```

For Zsh, initialize completion first if your dotfiles do not already do so:

```zsh
autoload -Uz compinit
compinit
source <(sbc completion zsh)
```

Try `sbc pro<Tab>`, `sbc route <Tab>`, or `sbc install --shell <Tab>`.

### PowerShell

Load completion in the current session:

```powershell
sbc completion powershell | Out-String | Invoke-Expression
```

For persistent completion, add that line to the `$PROFILE` file for each
PowerShell host and version you use. Windows PowerShell 5.1 and PowerShell 7
can use different profile files. The installer does not edit these profiles or
change execution policy. Completion supports both `sbc` and `sbc.exe`.

PowerShell completion follows the PSReadLine Tab mode. `TabCompleteNext` cycles
through names. `Complete` shows a Bash-like list with descriptions when several
choices match. `MenuComplete` shows a menu with a description for the selected
choice. To select the menu mode:

```powershell
Set-PSReadLineKeyHandler -Key Tab -Function MenuComplete
```

This does not apply proxy variables to an existing PowerShell session. For that,
use `sbc env | Invoke-Expression`, as described in the
[Windows guide](README_WIN.md#current-session-environment).
