# Sing-box VPN Client for Windows

Checked on 2026-10-19.

`sbc` is the Sing-box client CLI for Windows. It runs unelevated, manages background
services via Windows Task Scheduler, and configures user and desktop proxy settings.

## Requirements

- Windows 10 or Windows 11 (x64, or ARM64 running x64 emulation)
- Windows PowerShell 5.1 (in-box on Windows 10/11) or PowerShell 7+
- Run `sbc` and its installer unelevated. Writing protected browser-policy registry
  values can require administrator approval, even under `HKCU`.

## Installation

Install `sbc` from an ordinary Windows PowerShell 5.1 or PowerShell 7 terminal:

```powershell
& ([scriptblock]::Create((irm 'https://<portal>/install.ps1')))
```

The script runs in memory without modifying Windows execution policy. Paste your
subscription link at the prompt. The installer:

1. Downloads `sbc.exe` and verifies the signed manifest.
2. Downloads `sing-box.exe` and default rule snapshots.
3. Tests proxy connectivity through a loopback inbound.
4. Registers `sbc-proxy`, `sbc-config-refresh`, and `sbc-upgrade` scheduled tasks.
5. Adds `%LOCALAPPDATA%\sbc\cli` to the user `PATH`.
6. Sets user environment proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`).

To select a protocol during installation, pass `-Protocol`:

```powershell
& ([scriptblock]::Create((irm 'https://<portal>/install.ps1'))) -Protocol hysteria2
```

### Legacy client migration

Running the installer on a machine with the legacy archive-based PowerShell client
migrates it automatically:

1. Exchanges the saved machine token for a subscription URL via `POST /api/sub`.
2. Preserves the configured port, route strategy, protocol, and authentication choice.
3. Verifies `sbc` proxy functionality on a spare port before removing the old client.
4. Runs the legacy uninstaller to remove old scheduled tasks and PowerShell modules.
5. Hands the original port and owned desktop proxy settings over to `sbc`.

If installation fails, the legacy client remains active and untouched.

## Commands

| Command                           | Description                                                                     |
| --------------------------------- | ------------------------------------------------------------------------------- |
| `sbc on`                          | Set user environment proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`)  |
| `sbc off`                         | Clear user environment proxy variables; leaves sing-box running                 |
| `sbc desktop on`                  | Direct Windows system/browser proxy (WinINet) to the local proxy                |
| `sbc desktop off`                 | Clear Windows system proxy settings; retain WebRTC protection                   |
| `sbc webrtc on`                   | Enable persistent browser WebRTC protection; may request administrator approval |
| `sbc webrtc off`                  | Remove owned policies, reset Firefox WebRTC preferences, stop automatic setup   |
| `sbc env`                         | Print PowerShell commands to apply proxy settings to the current session        |
| `sbc env off`                     | Print PowerShell commands to clear proxy settings from the current session      |
| `sbc route <strategy>`            | Switch route strategy without restarting; `--list` shows names and meanings     |
| `sbc protocol <name>`             | Switch outbound protocol (`trojan`, `hysteria2`, `naive`) without restarting    |
| `sbc port <number>`               | Move local proxy listen port and update associated settings                     |
| `sbc status`                      | Show proxy state, active protocol and route, and monthly traffic usage          |
| `sbc ip`                          | Query exit IP addresses and geo-location through the proxy                      |
| `sbc speed`                       | Test latency; add `--download` to test download throughput                      |
| `sbc refresh`                     | Fetch subscription configuration and check the signed release                   |
| `sbc upgrade`                     | Install signed binary upgrades and matching configuration                       |
| `sbc refresh auto [on\|off]`      | Show or change automatic configuration refresh                                  |
| `sbc upgrade auto [on\|off]`      | Show or change automatic binary upgrades                                        |
| `sbc refresh interval [duration]` | Show or change the configuration refresh interval                               |
| `sbc upgrade interval [duration]` | Show or change the binary upgrade interval                                      |
| `sbc start` / `sbc stop`          | Start or stop the background service                                            |
| `sbc restart`                     | Restart sing-box background service                                             |
| `sbc link` / `sbc link set`       | Display or replace the active subscription URL                                  |
| `sbc uninstall`                   | Remove scheduled tasks, files, PATH entry, and owned proxy settings             |
| `sbc version`                     | Display `sbc` version and commit information                                    |

Region matching (`sbc privacy region`) is Linux-only. On Windows the command
reports that it is unavailable.

### Current session environment

Persistent environment changes apply to newly launched applications and terminals.
To apply proxy variables immediately to an existing PowerShell session:

```powershell
sbc env | Invoke-Expression
```

To clear them from the current session:

```powershell
sbc env off | Invoke-Expression
```

### Help and completion

Use `sbc <command> --help` or `sbc help <command>` for command details.
Help does not require an installation and does not perform the command's action.
The CLI rejects unknown flags and extra arguments.

Load completion in the current PowerShell session:

```powershell
sbc completion powershell | Out-String | Invoke-Expression
```

For persistent completion, add that line to the `$PROFILE` file for each
PowerShell host and version you use. The installer does not edit profiles or
change execution policy. Completion works in Windows PowerShell 5.1 and
PowerShell 7, for both `sbc` and `sbc.exe`.
Route and protocol suggestions come from the local config, without a server
request or running proxy. Completion includes command-specific flags and route
descriptions as tooltips. See the [cross-platform CLI guide](README_SBC.md)
for `direct`, `ai-only`, `gfw-proxy`, `china-direct`, and `global`.

Plain `sbc ip` contacts no AI provider. `sbc ip --ai` adds unauthenticated Gemini,
ChatGPT, and Claude checks through a proxy-only diagnostic inlet. Direct AI
requests require `--ai --allow-direct-ai`. See
[IP and AI checks](README_SBC.md#exit-ip-and-ai-checks) for scope and risks.

## Architecture and runtime

### Scheduled tasks

`sbc` manages three Windows scheduled tasks registered via `schtasks.exe`:

| Task                 | Schedule                     | Purpose                                                   |
| -------------------- | ---------------------------- | --------------------------------------------------------- |
| `sbc-proxy`          | At user logon                | Runs `sbc run` and keeps sing-box in a Windows Job Object |
| `sbc-config-refresh` | Enabled by default, every 6h | Refreshes subscription configuration                      |
| `sbc-upgrade`        | Enabled by default, every 6h | Installs binaries from the operator's signed release      |

The two maintenance policies are independent. Saved switches and intervals survive
reinstall and subsequent upgrades. Policy changes do not immediately upgrade or
restart the proxy. Manual actions ignore the automatic switches. See
[automatic maintenance](README_SBC.md#automatic-maintenance) for intervals, recovery,
and migration from `sbc-refresh`.

Background task registration or retirement failures do not block manual refresh,
upgrade, start, or restart. These commands report a warning and still attempt their
action. Invalid maintenance settings, failed settings writes, and failed recovery
remain errors.

The wrapper holds an engine lock until sing-box exits. A controlled startup window
allows maintenance to restart it without admitting unrelated starts during file
replacement. Failed startup must stop before rollback. If Windows refuses process
cleanup setup, the wrapper does not start the engine.

Windows permits renaming a running executable but can prevent its removal.
Upgrades use separate retired filenames so rollback can restore a binary while
the updater still runs. A later proxy start removes retired executables and
libraries that Windows no longer holds open.

Tasks default to S4U (Service-for-User) logon, running without a console window. If
system policy forbids S4U for unelevated users, `sbc` falls back to an interactive
task. Proxy output logs to `%LOCALAPPDATA%\sbc\sing-box.log`. `sbc status` shows
recent maintenance results. Run a manual action to inspect its error.

### System and desktop proxy

The Windows desktop proxy is opt-in via `sbc desktop on`. Once enabled, it follows
`sbc on` and `sbc off`.

- **WinINet registry**: `sbc` updates `ProxyEnable`, `ProxyServer`, and `ProxyOverride` under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings` and notifies running
  browsers using `InternetSetOption` (options 39 and 95).
- **Environment variables**: `sbc` maintains `HTTP_PROXY`, `HTTPS_PROXY`, `FTP_PROXY`,
  `SOCKS_PROXY`, `ALL_PROXY`, and `NO_PROXY` in `HKCU\Environment` and broadcasts a
  `WM_SETTINGCHANGE` message to the system.
- **WebRTC leak prevention**: when browser policies are missing, `sbc desktop on`
  requests administrator approval for a registry-only helper. The helper sets
  `WebRtcIPHandling` to `disable_non_proxied_udp` for Chrome and Brave. For Edge, it
  sets `WebRtcLocalhostIpHandling` to the same value. It also sets Firefox WebRTC
  preferences. The helper targets `HKEY_USERS\<original-user-SID>\Software\Policies`,
  even if elevation uses a different administrator. The installer, client, service
  and Firefox profile edits stay unelevated. Browser registry ACLs are not changed.
- **Persistent protection**: `sbc off` and `sbc desktop off` leave browser protection
  enabled. Ordinary toggles need no further approval unless a policy is missing.
  This can restrict WebRTC calls while the proxy is off. `sbc webrtc off` removes
  owned policies, resets the four Firefox WebRTC profile preferences regardless of
  origin, and disables automatic setup; only `sbc webrtc on` enables it again. Removing protected settings can require administrator approval.
  Concurrent setup and cleanup commands wait for each other, including Firefox
  profile changes. Automatic setup cannot reverse a completed opt-out.
- **Safety**: `sbc` preserves foreign proxies, PAC scripts and pre-existing browser
  policies. A conflicting Chromium policy or failed setup makes `sbc desktop on`
  fail before enabling a new desktop proxy. Policy writes are read back. A protected
  ownership record lets cleanup retry after interrupted or partially failed setup.
  Registry cleanup deletes only owned values that still match what `sbc` wrote.
  Firefox profile cleanup intentionally resets the four supported preferences,
  including pre-existing user choices; unrelated preferences remain untouched.

The Chromium policy restricts non-proxied UDP; it does not disable the WebRTC API.
`sbc on` checks browser settings even when the opted-in desktop proxy is already
on, unless you opted out with `sbc webrtc off`.

### Check Chrome WebRTC protection

`sbc desktop` reports the WinINet proxy state, not Chrome's effective policy.
To enable or repair browser protection, run this in an ordinary, unelevated terminal
and approve the registry-only prompt:

```powershell
sbc webrtc on
```

Use `sbc desktop on` to enable the Windows proxy as well.

The elevated PowerShell window runs the helper and closes automatically. It needs
no typed input. The original terminal reports the result.

Open `chrome://policy`, click **Reload policies**, and find `WebRtcIPHandling`.
Its value should be `disable_non_proxied_udp` with status **OK**. Reload the WebRTC
leak-test page: it may still report that WebRTC is available, but it must not expose
your ISP address. `WebRtcIPHandlingUrl` is not required or set by `sbc`; an existing
per-URL or machine policy can override the user setting.

If the policy is missing, check the command's error and the saved value:

```powershell
reg query "HKCU\Software\Policies\Google\Chrome" /v WebRtcIPHandling
```

For an older client that reports success but leaves this value missing, set Chrome's
per-user policy directly as a temporary workaround:

```powershell
reg add "HKCU\Software\Policies\Google\Chrome" /v WebRtcIPHandling /t REG_SZ /d disable_non_proxied_udp
```

If this returns **Access is denied**, Windows protects the policy key. Run only
this registry command in an elevated terminal for the same Windows account, then
reload `chrome://policy` and the leak-test page. Keep `sbc` and its installer
unelevated; do not change registry permissions to bypass the restriction.

If elevation uses a different administrator account, `HKCU` refers to that
administrator, not the Chrome user. The administrator must instead target
`HKEY_USERS\<Chrome-user-SID>\Software\Policies\Google\Chrome`. Obtain the Chrome
user's SID with `whoami /user` in their normal terminal.

### Check Edge WebRTC protection

Run `sbc webrtc on`, then restart Edge through `edge://restart`.
Open `edge://policy` and check that `WebRtcLocalhostIpHandling` has value
`disable_non_proxied_udp` and status **OK**. Repeat the leak test.
Restart Edge after `sbc webrtc off` too.

Edge reports `WebRtcIPHandling` as an unknown policy. Setup and cleanup remove this
obsolete value only when the protected ownership record identifies it as an
unchanged value created by `sbc`. Manual or externally changed values remain.
The supported Edge policy has its own ownership record, so migration preserves
pre-existing settings.

[Microsoft documents](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/WebRtcLocalhostIpHandling)
a required browser restart and excludes profiles signed in with a personal
Microsoft account. Check the effective policy in the profile used for testing.

### File locations

Client files and binaries reside in `%LOCALAPPDATA%\sbc`:

| Path                                   | Contents                                         |
| -------------------------------------- | ------------------------------------------------ |
| `cli\`                                 | `sbc.exe` CLI executable (on user `PATH`)        |
| `bin\`                                 | sing-box executable and runtime libraries        |
| `config.json`, `subscription`          | Cached configuration and subscription credential |
| `webrtc-mode`                          | Explicit WebRTC opt-out                          |
| `maintenance.json`                     | Automatic policies and recent action results     |
| `sbc.version`                          | Installed client build                           |
| `upgrade-recovery.json`, `.upgrade-*\` | Pending upgrade recovery record and backups      |
| `sing-box.log`                         | Runtime log                                      |

`%LOCALAPPDATA%\sbc\webrtc-mode` records the WebRTC opt-out. Policy ownership records
are separate, under `HKCU\Software\Policies\sbc\WebRTC`. This key is protected against
unelevated writes. Cleanup removes the ownership records and deletes the key when empty.

## Uninstallation

From an ordinary terminal, run:

```powershell
sbc uninstall
```

Approve the browser-policy cleanup prompt if shown. Uninstall first removes owned
WebRTC settings, including the managed Firefox `user.js` block and its saved
`prefs.js` values. Close Firefox first; an active profile makes cleanup fail
without removing its ownership information. It then stops the
proxy, removes scheduled tasks, reverts owned proxy and environment settings,
removes the user `PATH` entry, and deletes `%LOCALAPPDATA%\sbc`.

If approval is declined or cleanup fails, uninstall stops and keeps the client and
its recovery state. Resolve the error and retry `sbc uninstall`; do not delete its
folder first. Pre-existing or externally changed browser policies are retained and
listed. Firefox's four supported profile preferences are reset to browser defaults,
including orphaned values from older installations. Old snapshots are deleted,
not restored.

See [cross-platform WebRTC behavior](WEBRTC.md) for the shared command contract.

### Manual WebRTC cleanup

To stop `sbc` from reapplying protection without uninstalling it:

```powershell
sbc webrtc off
```

Reload `chrome://policy` (or the corresponding Edge/Brave page). Pre-existing
policies are not adopted by `sbc`, including a matching Chrome policy added with
the workaround above. If you added that value yourself, check it with `reg query`
and remove only that value:

```powershell
reg delete "HKCU\Software\Policies\Google\Chrome" /v WebRtcIPHandling
```

If access is denied, run this command elevated for the same Windows user. If
elevation uses another account, replace `HKCU` with
`HKEY_USERS\<Chrome-user-SID>`. Obtain that SID with `whoami /user` in the Chrome
user's normal terminal. Reload browser policies afterward. Keep policies required
by your administrator. Edge and Brave use the corresponding keys listed by `sbc`.
Do not delete an entire browser policy key.

Close Firefox before `sbc webrtc off` or uninstall. Plain cleanup resets these four
preferences in both `prefs.js` and `user.js`, even without an ownership marker or
snapshot:

- `media.peerconnection.ice.no_host`
- `media.peerconnection.ice.default_address_only`
- `media.peerconnection.ice.proxy_only`
- `media.peerconnection.ice.proxy_only_if_behind_proxy`

`sbc` does not rewrite a running browser's `prefs.js` or close the browser for you.
These four preferences are reset regardless of value or origin, including
pre-existing user choices. Unrelated preferences and administrator-enforced
policies remain untouched. Cleanup deletes obsolete `.sbc-webrtc.json` snapshots
without restoring their values. A locked profile returns an error; close Firefox
and retry the command instead of editing preferences manually. Check
`about:policies` if a preference is still enforced by another policy.
