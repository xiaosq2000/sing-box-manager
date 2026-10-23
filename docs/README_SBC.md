# sbc client CLI

Checked on 2026-10-23.

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

Root sets directory modes explicitly, including with `umask 077`. Do not lower
a private umask to make activation work. Public transaction metadata stays
readable, while config and rule directories remain service-group-only and the
engine state directory remains private. See [TUN file protection](SECRET_HANDLING.md).

TUN answers DNS through its route rules and never uses the systemd-resolved stub.
Linux TUN uses IPv4-only DNS and rejects captured public IPv6 traffic in every
route mode, including configured direct exceptions. Dual-stack capture stays
enabled so IPv6 traffic does not escape by losing its TUN route. Captured
private IPv6 destinations retain LAN access. This policy supports IPv4-only
networks without changing system IPv6 settings. Mixed and mobile profiles are
unchanged.

Cached addresses, IPv6 literals and independent encrypted DNS do not
bypass the public IPv6 rejection. This is not a guarantee for unvalidated paths
such as interface-bound sockets. China-direct still exposes the local public
IPv4 address to direct destinations by design.

Apply a new portal TUN policy with manual `sbc refresh` at the machine's console.
Automatic jobs only report the update. A browser that retains old IPv6 answers
may need a restart or DNS-cache clearing before it uses IPv4.

Mixed mode and TUN never run together: `sbc on`, `sbc desktop on` and
`sbc docker on` refuse while TUN is active, and the mixed user unit has a
condition guard. `sbc install` and `sbc uninstall` also refuse while TUN is
active. Run `sbc tun off` first. The service is enabled at boot only
after activation checks pass. `sbc tun off` stops and disables the service and
leaves the mixed proxy off; run `sbc on` to restore it. There is no fail-open: if
the proxy fails, captured traffic has no exit, so run `sbc tun off` at the machine.

Switching from Mixed can leave proxy variables in existing applications.
`sbc tun on` warns before it stops Mixed. `sbc status` reports residual proxy
variable names inherited by that command, not a system-wide process check.
`sbc env` refuses exports during active or uncertain capture. Explicit
`sbc env off` remains available, but its cleanup includes foreign proxy values.

Reload the current shell integration with `eval "$(sbc init bash)"` in Bash or
`eval "$(sbc init zsh)"` in Zsh. The hook removes recognized sbc values without
exporting a new ownership or capture marker. It preserves unrelated proxy values.
Then restart affected applications from a clean environment. With persistent
sessions such as tmux or Herdr, check the environment used to create new panes.
Reconnecting alone does not change existing processes. Do not automatically stop
an entire session service. Pure TUN use needs no routine application restart.
See [environment migration](TUN_ENV_MIGRATION.md) for boundaries and acceptance.

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

### TUN maintenance

Run manual `sbc refresh` and `sbc upgrade` at the machine's own console.
They can interrupt connectivity briefly. Refresh keeps the installed binaries.
Upgrade uses signed client and engine downloads. Both prepare the complete
configuration, rule snapshots and binaries before root stops the old capture.
An invalid candidate leaves the active generation unchanged.

Maintenance preserves the active route and selected protocol, not the portal's
startup defaults. A new cache namespace prevents old cached selections from
changing candidate startup. Root verifies the current policy before the owner
publishes installed files and API state. Root then commits the generation.
No failure automatically starts an old TUN or the mixed proxy.

Enabled automatic jobs only check for changed configuration, signed rule
snapshots and signed builds while TUN is active. They do not apply updates or
restart services. `sbc status` shows an update hint. Run manual maintenance to
apply the update.

An interrupted update retains a private typed recovery record and file backups.
Run `sbc refresh` locally to recover it. Root retains the commit fact after
successful or failed shutdown. Recovery keeps committed user files, including
when capture is already off. Otherwise, it restores files only after confirmed shutdown, or if the previous
generation never stopped. Recovery after shutdown leaves capture off and returns
an error with instructions. Run `sbc tun on` explicitly when ready.
If shutdown is unconfirmed, recovery retains the backups and mixed-mode block.
Do not delete the recovery record or staging directory to bypass this check.

`sbc route` and `sbc protocol` query or switch the committed active TUN through
the owner's saved API state. Switching does not restart capture or rewrite the
installed configuration. Mutations use the operation lock. Invalid choices,
pending or dirty capture, missing owner state, and API failures return errors.
The commands never fall back to the mixed API while TUN is active or uncertain.
`sbc status` uses the same owner-state checks. `route --list` and completion
remain offline lists from the installed configuration, not live TUN queries.

`sbc ip`, `sbc check`, and `sbc speed download` use the committed active TUN.
An older generation without diagnostic metadata needs a local `sbc refresh` first.
The commands never fall back to mixed mode. Missing capture evidence retains the
HTTP observation but returns 1 as unverified scope. See
[network diagnostics](#network-diagnostics) and the
[measurement contract](CLIENT_DIAGNOSTICS.md).

### Acceptance and limits

The M1b acceptance scope covers workstation activation, five route modes, real
Trojan/Hysteria2/Naive traffic and refresh. Isolated KVM and TCG checks also cover
the hosted installer, actual maintenance timer triggers, a signed engine upgrade
from 1.14.2 to 1.14.3, and committed/pending/dirty state across reboot.
These checks do not mean the workstation received a new release or rebooted.

Public IPv6 access is disabled in Linux TUN. DNS suppression and captured public
IPv6 rejection have isolated fixture checks, not full IPv6 connectivity acceptance.
Docker bridge networking and interface-bound sockets remain unvalidated
compatibility areas. Do not assume those paths behave like the verified
application traffic. Diagnostic capture evidence covers only the observed request,
not every application or socket.
See the [acceptance scope](REFACTOR_SPEC.md#m1b-acceptance-scope).

After `sbc tun off`, uninstall removes the user client and its maintenance
schedules. It retains the disabled root TUN unit, helper, journal and superseded
root generations. Those generations can contain credentials. Uninstall does not
perform privileged garbage collection.

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

| Command                                     | Purpose                                                           |
| ------------------------------------------- | ----------------------------------------------------------------- |
| `sbc install`                               | Read a subscription link from stdin and install the client        |
| `sbc init bash`, `sbc init zsh`             | Print shell integration code                                      |
| `sbc link`, `sbc link set`                  | Show the masked link, or replace it from stdin                    |
| `sbc on`, `sbc off`                         | Switch shell and opted-in desktop proxies                         |
| `sbc env`, `sbc env off`                    | Print proxy variables for scripts or the current shell            |
| `sbc desktop [on\|off]`                     | Show or switch desktop proxy settings                             |
| `sbc privacy`                               | Show browser and region privacy choices                           |
| `sbc privacy browser [on\|off]`             | Show or change all three Chrome privacy components (Linux)        |
| `sbc privacy browser geolocation [on\|off]` | Show or block website geolocation for all accounts (Linux Chrome) |
| `sbc privacy browser webrtc [on\|off]`      | Show or change Chrome WebRTC protection (Linux)                   |
| `sbc privacy browser language [on\|off]`    | Show or match Chrome language preferences to the node (Linux)     |
| `sbc privacy region [on\|off]`              | Show or match the node's client time zone and language (Linux)    |
| `sbc tun on`, `sbc tun off`                 | Enable or disable Linux TUN capture                               |
| `sbc lang [en\|zh\|auto]`                   | Show or set the language of sbc messages                          |
| `sbc docker [on\|off] [--yes]`              | Show or switch the Linux Docker daemon's proxy                    |
| `sbc route [name]`, `sbc protocol [name]`   | Show or switch a choice without restarting sing-box               |
| `sbc route --list`                          | List installed route strategies and their meanings                |
| `sbc port [number]`                         | Show or change the local proxy port                               |
| `sbc status`, `sbc ip`                      | Show status or observed exit IPs                                  |
| `sbc check [URL...]`                        | Check website first responses and HTTP access                     |
| `sbc check targets …`                       | Manage the private local website list                             |
| `sbc speed download [--size 20MB]`          | Measure bounded single-stream download throughput                 |
| `sbc start`, `sbc stop`, `sbc restart`      | Control the service                                               |
| `sbc run`                                   | Run sing-box in the foreground                                    |
| `sbc refresh`, `sbc upgrade`                | Refresh configuration, or install signed binary updates           |
| `sbc uninstall [--yes]`                     | Clean browser settings, stop the service, and remove the client   |
| `sbc completion bash\|zsh\|powershell`      | Print a completion script                                         |
| `sbc version`, `sbc --version`              | Print the build version                                           |

`on` and `off` differ from `start` and `stop`. `off` leaves sing-box running.
Desktop settings follow proxy toggles only after `sbc desktop on`.
Docker changes require confirmation or `--yes` because they restart Docker.

Subscription links are credentials. Pass them through the installer prompt or
stdin, not command arguments. `sbc link set` also reads from stdin.

## Terminal output

Colors use the terminal's ANSI palette, not a fixed RGB theme. Body text and
backgrounds inherit terminal defaults. Headings use bold, with cyan focus,
green success, yellow warnings and red errors. Status text and focus markers
remain meaningful without color. Controls need no Nerd Font: selection uses `>`
and progress uses an ASCII spinner. Tables retain common Unicode borders.

Human-readable lists use CJK-aware column widths. Status and privacy panels align
values across the complete panel. Narrow terminals show stacked table fields
without removing results. A non-empty `NO_COLOR` disables styling, including bold,
but keeps keyboard interaction. `CLICOLOR=0` also disables styling unless
`CLICOLOR_FORCE` overrides it. `NO_COLOR` takes precedence.
Pipes, files, and `TERM=dumb` use static output without terminal controls, even
with forced color. Machine-readable output, including `--json`, shell exports,
and completion scripts, does not initialize these views or query terminal colors.

Interactive `check`, `ip`, and `speed download` show an inline progress view.
Use arrow keys or Page Up/Down to scroll. The view adapts to window size changes.
The complete final report remains in terminal history. Ctrl-C returns 130 with
partial diagnostic results. POSIX SIGTERM returns 143.

Route and protocol selectors keep the current item visible in short windows.
Use Page Up/Down to read a selected item that is taller than the window.
Below four columns, interactive views show a `<>` resize hint (`<` in one column).
They retain their data and redraw when the window is wider.
Only Enter confirms a switch. Space does not submit. Esc or `q` cancels without
changing settings. Ctrl-C and SIGINT return 130. POSIX SIGTERM returns 143.
Selection errors return a failure instead of a successful query.
Non-interactive calls show the same choices in a static table, with a current-item
marker. If the API list omits the current choice, the query stays read-only and
shows the current value separately. Display labels omit terminal control sequences
and embedded line breaks. API requests keep the original option values.

## Snell protocol

New installs prefer [Snell v6](SNELL.md) when its infrastructure and account
credential are enabled, unless the operator or installer selects another protocol.
An omitted `default_protocol` uses Trojan when Snell infrastructure is absent.
An explicit operator default takes precedence. Saved protocol choices survive
refresh, upgrade and restart. A client without a saved choice follows the new
default, even if it is already installed. To switch an existing client,
refresh its subscription and run `sbc protocol snell`. Linux TUN owners must
refresh locally at the console.

New installs can use `-p snell` in the Unix installer or `--protocol snell` in
`sbc install`. Completion reads the installed selector. Mixed-mode diagnostics
recognize Snell. Diagnostics in both modes follow active rules, not a forced
protocol benchmark.

Snell uses a shared PSK and a separate userkey for each account. It does not
use the deployment TLS certificate. Adding it does not change route policy or
Linux TUN UDP restrictions. An account without the configured default uses its
first enabled protocol. This is not automatic switching after connection failure.

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

`sbc privacy browser webrtc on|off` manages the mandatory
`WebRtcIPHandling=disable_non_proxied_udp` policy for Google Chrome on Linux.
It works without a subscription or a running proxy. Desktop proxy setup also
ensures the policy unless you opted out. Only Chrome is managed; Chromium, Edge, Brave, Firefox, macOS and Windows are
not. `sbc privacy` summarizes this choice. See
[WebRTC protection](WEBRTC.md) for ownership, cleanup, limits and manual
cleanup after the scope change.

## Chrome geolocation blocking

`sbc privacy browser geolocation on` blocks website location requests with the
mandatory `DefaultGeolocationSetting=2` policy. It covers saved site grants and
new Chrome profiles. It needs no subscription or running proxy. Policy writes
need sudo and affect every account that uses Linux Chrome's system policy
directory. Restart Chrome and check `chrome://policy` after a change.

The default is off. Installation, upgrades, refreshes and proxy toggles do not
enable or repair it. `sbc privacy browser geolocation off` removes only sbc's
owned policy. It preserves saved website permissions and external policies.
Failed cleanup prevents uninstall from removing sbc.

The query reports the saved choice, local policy configuration and conflicts.
Local files do not prove Chrome's effective policy. IP-based location estimates
remain possible. See [geolocation blocking](WEBRTC.md#website-geolocation-blocking)
for ownership, conflict checks, cleanup and limits.

## Chrome language matching

`sbc privacy browser language on` replaces the language list in this account's
standard Chrome personal profiles with the node language and its base language,
such as `en-US,en`. It updates `intl.selected_languages` and
`intl.accept_languages` at the top level and in existing `account_values` fields.
It creates no account store or missing account language field. Exit Chrome
completely before applying or restoring languages. Do not start Chrome while
the command runs.

The node's subscription region supplies the target. Language matching does not
require a generated system locale or enable system region matching. It changes
neither the browser UI language nor translation and spell-check settings.
`sbc privacy browser language off` restores fields that still match sbc's values.
It preserves external changes. Failed restoration blocks uninstall and keeps
recovery records for retry.

`sbc privacy browser on|off` attempts WebRTC, language and geolocation changes
independently. It reports each result and exits nonzero if any component fails.
Successful components stay applied. Use explicit component subcommands for
scripts. `webrtc` and `geolocation` need no subscription. An existing WebRTC choice
does not enable language matching or geolocation blocking.

Refresh only updates the target and reports pending application. Proxy toggles,
refreshes and upgrades never apply browser language changes automatically.
Chrome settings sync can propagate or overwrite the list. sbc warns but does not
change sync settings. See [Chrome privacy](WEBRTC.md#chrome-language-matching)
for profile discovery, policy conflicts, recovery and effective-language checks.

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
directly. The time zone check accepts both absolute and relative `/etc/localtime`
links to the target zone. The language applies at the next login; the time zone
applies to new processes immediately. Open applications and browsers keep their current values
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

Active Linux TUN uses the [TUN maintenance contract](#tun-maintenance): automatic
jobs only check and report updates. Manual maintenance republishes the TUN
without starting the mixed proxy. The restart and rollback details below apply
to mixed mode.

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
an incomplete attempt as "running or interrupted". An interrupted mixed-mode
upgrade keeps its recovery record and backups. The next maintenance action or proxy start
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
and restarts. Completion suggests canonical names only. When standard input and
output are interactive terminals, bare `sbc route` and `sbc protocol` open an
interactive selector (`Enter` switches, `Esc` or `q` cancels without changing the
selection). Non-interactive invocations show a static choices table with the
current choice marked. See [terminal output](#terminal-output) for fallback rules.

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

Rule-set maintenance can use the remote proxy even on `direct`. Diagnostics follow
the active destination rules. A route switch does not close existing connections.

## Network diagnostics

```sh
sbc ip
sbc ip --dual-stack --json
sbc check
sbc check https://www.example.com/ --samples 3
sbc check --preset global
# Explicit AI targets can expose your direct public IP:
sbc check --preset ai
sbc speed download
sbc speed download --size 5MB --json
```

All three commands diagnose sbc traffic, not every application's environment.
Mixed uses the ordinary authenticated inlet even with shell proxies off. Linux
TUN uses native connections and ignores proxy variables. Both follow current
routes and direct exceptions. They do not change selections or restart services.

### Website responses

`check` uses the saved list, or 17 built-in non-AI targets when no list exists.
Explicit URLs and read-only `default`, `china`, `global`, or `ai` presets override
that selection for one invocation. Explicit URLs and `--preset` are mutually
exclusive. Default sampling is once per target. The range is 1–5. Multiple samples
show the median first-response time and response counts.

| Category / preset        | Websites                                                                                                                      |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| China / `china`          | Baidu, Bilibili, WeChat, Taobao, Zhihu, Douyin, University of Macau (`www.um.edu.mo`), University of Hong Kong (`www.hku.hk`) |
| International / `global` | GitHub, Cloudflare, Google, YouTube, X, TikTok, Reddit, Telegram, IEEE (`www.ieee.org`)                                       |
| AI / `ai`                | ChatGPT, Claude, Gemini. Explicit selection only                                                                              |

`default` contains China targets first, then international targets. These targets
request `/favicon.ico` on their respective hosts. AI targets request provider
homepages. A resource response does not establish App, messaging, or video access.
The category column appears in both progress and final output. JSON website
observations include `category`: `china`, `global`, `ai`, or `custom`.
Categories describe website groups, not actual direct or proxy paths.

HTTPS GET checks retain TLS verification, read at most 8 KiB, and follow no
redirects. They use no cookies or account credentials. A 403 means a response
arrived but access was denied. A 3xx is an unfinished redirect, not a completed
visit. These checks do not measure rendering, login eligibility, or packet loss.

Capture evidence comes from asynchronous local API observations. A short
connection can disappear before inspection. Missing evidence remains unverified,
never a guessed direct path. TUN scope verification fails in that case while
retaining HTTP timing and status. Post-connect inspection cannot prevent a source
IP from escaping with an uncaptured initial connection.

### Website lists

```sh
sbc check targets list
sbc check targets add https://www.example.com/health
sbc check targets remove TARGET_ID
sbc check targets export > targets.json
sbc check targets import targets.json
sbc check targets reset
```

The private `diagnostic-targets.json` uses versioned JSON and stable IDs. It holds
at most 24 targets. Import replaces the saved list only after full validation.
List operations send no request. A missing file uses defaults without writing.
An explicitly empty list stays empty. New built-in defaults do not replace a saved
list. Use `sbc check --preset default` without changing it, or run
`sbc check targets reset` to explicitly replace it with the 17 built-in targets.

`targets list` shows each target's category. Saved JSON targets accept an optional
`category` field: `china`, `global`, `ai`, or `custom`. Import and export preserve
this field. Lists without it remain valid. Exact built-in URLs inherit their
built-in category for display and reports without changing the saved file.
Other URLs show as Custom unless the saved target supplies a category.

Only HTTPS is accepted. Embedded credentials and obvious local literals are
rejected. Validation does not resolve mixed targets locally. Exact endpoint URLs
remain distinct on one host. Exports preserve complete URLs, including potentially
secret query values, and must remain private. Reports hide paths and query values.

### Exit IP

`ip` asks IPify and ident.me for IPv4. `--dual-stack` requests IPv6 too, subject
to generation-bound policy. Linux TUN's known public IPv6 restriction produces
explicit skipped, unverified rows. It does not become a verified dual-stack result.
Unknown failures never prove that IPv6 rejection works.

Different sources can observe different exits under split routing. No source
represents every website. Each distinct IP gets one ipwho.is lookup for location,
ASN, and organization estimates. A detail failure keeps the IP and is only a
warning. Those databases receive observed addresses. Region estimates do not
prove service or account eligibility.

### Download throughput and results

`speed download` uses Cloudflare's download source through the current route.
Default payload is 20 decimal MB, with whole-MB sizes from 1 through 99. It measures
one response stream, not maximum line capacity. It reports actual payload bytes,
transfer duration, and Mbit/s separately from first-response latency. Budgets do
not include all network overhead or guarantee a server bill.

Bare `speed`, help, and completion send no request. Retired `--all`, `--download`,
`--ai`, and `--allow-direct-ai` flags fail before requests. There is no protocol
ranking or provider-region scraping.

`--json` outputs a versioned result with separate transport, HTTP, capture, and
IP validation data. Unknown values stay null. HTTP 2xx for every requested sample
is required for a successful `check`. Any failed goal, changed runtime, or missing
TUN scope proof returns 1. Invalid invocation or configuration returns 2. SIGINT
returns 130 with partial results. See [the full contract](CLIENT_DIAGNOSTICS.md).

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
