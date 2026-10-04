# Sing-box VPN Client for Windows

Checked on 2026-10-04.

`sbc` is the Sing-box client CLI for Windows. It runs unelevated, manages background
services via Windows Task Scheduler, and configures user and desktop proxy settings.

## Requirements

- Windows 10 or Windows 11 (x64, or ARM64 running x64 emulation)
- Windows PowerShell 5.1 (in-box on Windows 10/11) or PowerShell 7+
- Unelevated user account (no administrator rights needed or allowed)

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
4. Registers scheduled tasks `sbc-proxy` and `sbc-refresh`.
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

| Command                  | Description                                                                    |
| ------------------------ | ------------------------------------------------------------------------------ |
| `sbc on`                 | Set user environment proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`) |
| `sbc off`                | Clear user environment proxy variables; leaves sing-box running                |
| `sbc desktop on`         | Direct Windows system/browser proxy (WinINet) to the local proxy               |
| `sbc desktop off`        | Clear Windows system proxy settings                                            |
| `sbc env`                | Print PowerShell commands to apply proxy settings to the current session       |
| `sbc env off`            | Print PowerShell commands to clear proxy settings from the current session     |
| `sbc route <strategy>`   | Switch route strategy (`china`, `gfw`, `ai`, `global`) without restarting      |
| `sbc protocol <name>`    | Switch outbound protocol (`trojan`, `hysteria2`, `naive`) without restarting   |
| `sbc port <number>`      | Move local proxy listen port and update associated settings                    |
| `sbc status`             | Show proxy state, active protocol and route, and monthly traffic usage         |
| `sbc ip`                 | Query exit IP addresses and geo-location through the proxy                     |
| `sbc speed`              | Test latency; add `--download` to test download throughput                     |
| `sbc update`             | Fetch latest subscription configuration and check for updates                  |
| `sbc start` / `sbc stop` | Start or stop the background service                                           |
| `sbc restart`            | Restart sing-box background service                                            |
| `sbc link show` / `set`  | Display or replace the active subscription URL                                 |
| `sbc uninstall`          | Remove scheduled tasks, files, PATH entry, and owned proxy settings            |
| `sbc version`            | Display `sbc` version and commit information                                   |

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

## Architecture and runtime

### Scheduled tasks

`sbc` manages two Windows scheduled tasks registered via `schtasks.exe`:

| Task          | Schedule             | Purpose                                                         |
| ------------- | -------------------- | --------------------------------------------------------------- |
| `sbc-proxy`   | At user logon / boot | Runs `sbc run`, keeping sing-box active in a Windows Job Object |
| `sbc-refresh` | Every 6 hours        | Refreshes configuration and checks signed manifest updates      |

Tasks default to S4U (Service-for-User) logon, running without a console window. If
system policy forbids S4U for unelevated users, `sbc` falls back to an interactive
task. Task output logs to `%LOCALAPPDATA%\sbc\data\sing-box.log`.

### System and desktop proxy

The Windows desktop proxy is opt-in via `sbc desktop on`. Once enabled, it follows
`sbc on` and `sbc off`.

- **WinINet registry**: `sbc` updates `ProxyEnable`, `ProxyServer`, and `ProxyOverride` under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings` and notifies running
  browsers using `InternetSetOption` (options 39 and 95).
- **Environment variables**: `sbc` maintains `HTTP_PROXY`, `HTTPS_PROXY`, `FTP_PROXY`,
  `SOCKS_PROXY`, `ALL_PROXY`, and `NO_PROXY` in `HKCU\Environment` and broadcasts a
  `WM_SETTINGCHANGE` message to the system.
- **WebRTC leak prevention**: `sbc desktop on` sets `WebRtcIPHandling` to
  `disable_non_proxied_udp` under `HKCU\Software\Policies` for Chrome, Edge, and Brave,
  and WebRTC leak prevention preferences for Firefox, preventing WebRTC STUN UDP
  requests from bypassing the proxy. `sbc desktop off` removes these managed policies.
- **Safety**: `sbc` modifies only settings pointing to its own port or managed defaults.
  Foreign proxies, PAC scripts, or existing browser policies belonging to other tools
  are preserved.

### File locations

All client state and binaries reside in `%LOCALAPPDATA%\sbc`:

| Directory | Contents                                                       |
| --------- | -------------------------------------------------------------- |
| `cli\`    | `sbc.exe` CLI executable (on user `PATH`)                      |
| `config\` | Cached sing-box configuration and secrets (`0600` permissions) |
| `data\`   | Runtime data, sing-box executable, rule snapshots, and logs    |

## Uninstallation

To completely remove `sbc`:

```powershell
sbc uninstall
```

This stops the proxy, removes scheduled tasks, reverts owned registry and environment
settings, removes `%LOCALAPPDATA%\sbc\cli` from user `PATH`, and deletes `%LOCALAPPDATA%\sbc`.
