# Sing-box VPN Client for Windows

Checked on 2026-10-20.

`sbc` is the Sing-box client CLI for Windows. It runs unelevated, manages background
services via Windows Task Scheduler, and configures user and desktop proxy settings.

## Requirements

- Windows 10 or Windows 11 (x64, or ARM64 running x64 emulation)
- Windows PowerShell 5.1 (in-box on Windows 10/11) or PowerShell 7+
- Run `sbc` and its installer unelevated. No browser-policy approval is needed.

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

| Command                           | Description                                                                    |
| --------------------------------- | ------------------------------------------------------------------------------ |
| `sbc on`                          | Set user environment proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`) |
| `sbc off`                         | Clear user environment proxy variables; leaves sing-box running                |
| `sbc desktop on`                  | Direct Windows system/browser proxy (WinINet) to the local proxy               |
| `sbc desktop off`                 | Clear Windows system proxy settings                                            |
| `sbc env`                         | Print PowerShell commands to apply proxy settings to the current session       |
| `sbc env off`                     | Print PowerShell commands to clear proxy settings from the current session     |
| `sbc lang [en\|zh\|auto]`         | Show or set the language of sbc messages                                       |
| `sbc route <strategy>`            | Switch route strategy without restarting; `--list` shows names and meanings    |
| `sbc protocol <name>`             | Switch outbound protocol (`trojan`, `hysteria2`, `naive`) without restarting   |
| `sbc port <number>`               | Move local proxy listen port and update associated settings                    |
| `sbc status`                      | Show proxy state, active protocol and route, and monthly traffic usage         |
| `sbc ip`                          | Query exit IP addresses and geo-location through the proxy                     |
| `sbc speed`                       | Test latency; add `--download` to test download throughput                     |
| `sbc refresh`                     | Fetch subscription configuration and check the signed release                  |
| `sbc upgrade`                     | Install signed binary upgrades and matching configuration                      |
| `sbc refresh auto [on\|off]`      | Show or change automatic configuration refresh                                 |
| `sbc upgrade auto [on\|off]`      | Show or change automatic binary upgrades                                       |
| `sbc refresh interval [duration]` | Show or change the configuration refresh interval                              |
| `sbc upgrade interval [duration]` | Show or change the binary upgrade interval                                     |
| `sbc start` / `sbc stop`          | Start or stop the background service                                           |
| `sbc restart`                     | Restart sing-box background service                                            |
| `sbc link` / `sbc link set`       | Display or replace the active subscription URL                                 |
| `sbc uninstall`                   | Remove scheduled tasks, files, PATH entry, and owned proxy settings            |
| `sbc version`                     | Display `sbc` version and commit information                                   |

Region matching (`sbc privacy region`) and Linux TUN (`sbc tun on`) are
Linux-only. On Windows these commands report that they are unavailable; the
mixed proxy and scheduled tasks provide the client service.

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
The CLI rejects unknown flags and extra arguments. `sbc lang en|zh|auto` selects
the message language; `SBC_LANG` overrides the saved choice.

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
- **Safety**: `sbc` preserves foreign proxies and PAC scripts. A conflicting
  proxy setting makes `sbc desktop on` fail before enabling a new desktop proxy.

Chrome WebRTC protection and browser language matching are not managed on
Windows. They are available only for Google Chrome on Linux. See
[Chrome privacy](WEBRTC.md).

### File locations

Client files and binaries reside in `%LOCALAPPDATA%\sbc`:

| Path                                   | Contents                                         |
| -------------------------------------- | ------------------------------------------------ |
| `cli\`                                 | `sbc.exe` CLI executable (on user `PATH`)        |
| `bin\`                                 | sing-box executable and runtime libraries        |
| `config.json`, `subscription`          | Cached configuration and subscription credential |
| `language`                             | Saved CLI language choice                        |
| `maintenance.json`                     | Automatic policies and recent action results     |
| `sbc.version`                          | Installed client build                           |
| `upgrade-recovery.json`, `.upgrade-*\` | Pending upgrade recovery record and backups      |
| `sing-box.log`                         | Runtime log                                      |

## Uninstallation

From an ordinary terminal, run:

```powershell
sbc uninstall
```

Uninstall stops the proxy, removes scheduled tasks, reverts owned proxy and
environment settings, removes the user `PATH` entry, and deletes
`%LOCALAPPDATA%\sbc`.

If cleanup fails, uninstall stops and keeps the client and its recovery state.
Resolve the error and retry `sbc uninstall`; do not delete its folder first.
