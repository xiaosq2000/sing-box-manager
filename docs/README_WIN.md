# Sing-box VPN Client for Windows

Checked on 2026-10-06.

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

| Command                  | Description                                                                     |
| ------------------------ | ------------------------------------------------------------------------------- |
| `sbc on`                 | Set user environment proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`)  |
| `sbc off`                | Clear user environment proxy variables; leaves sing-box running                 |
| `sbc desktop on`         | Direct Windows system/browser proxy (WinINet) to the local proxy                |
| `sbc desktop off`        | Clear Windows system proxy settings; retain WebRTC protection                   |
| `sbc webrtc on`          | Enable persistent browser WebRTC protection; may request administrator approval |
| `sbc webrtc off`         | Remove owned WebRTC settings and stop automatic setup; may request approval     |
| `sbc env`                | Print PowerShell commands to apply proxy settings to the current session        |
| `sbc env off`            | Print PowerShell commands to clear proxy settings from the current session      |
| `sbc route <strategy>`   | Switch route strategy (`china`, `gfw`, `ai`, `global`) without restarting       |
| `sbc protocol <name>`    | Switch outbound protocol (`trojan`, `hysteria2`, `naive`) without restarting    |
| `sbc port <number>`      | Move local proxy listen port and update associated settings                     |
| `sbc status`             | Show proxy state, active protocol and route, and monthly traffic usage          |
| `sbc ip`                 | Query exit IP addresses and geo-location through the proxy                      |
| `sbc speed`              | Test latency; add `--download` to test download throughput                      |
| `sbc update`             | Fetch latest subscription configuration and check for updates                   |
| `sbc start` / `sbc stop` | Start or stop the background service                                            |
| `sbc restart`            | Restart sing-box background service                                             |
| `sbc link show` / `set`  | Display or replace the active subscription URL                                  |
| `sbc uninstall`          | Remove scheduled tasks, files, PATH entry, and owned proxy settings             |
| `sbc version`            | Display `sbc` version and commit information                                    |

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
- **WebRTC leak prevention**: when browser policies are missing, `sbc desktop on`
  requests administrator approval for a registry-only helper. It sets `WebRtcIPHandling` to
  `disable_non_proxied_udp` for Chrome and Brave. Edge uses
  `WebRtcLocalhostIpHandling` with the same value. It also sets Firefox WebRTC
  preferences. It targets `HKEY_USERS\<original-user-SID>\Software\Policies`, even if
  elevation uses a different administrator. The installer, client and service stay
  unelevated; Firefox profile edits also run unelevated. Browser registry ACLs are
  not changed.
- **Persistent protection**: `sbc off` and `sbc desktop off` leave browser protection
  enabled. Ordinary toggles need no further approval unless a policy is missing.
  This can restrict WebRTC calls while the proxy is off. `sbc webrtc off` removes
  owned settings and disables automatic setup; only `sbc webrtc on` enables it
  again. Removing protected settings can require administrator approval.
  Concurrent setup and cleanup commands wait for each other, including Firefox
  profile changes. Automatic setup cannot reverse a completed opt-out.
- **Safety**: `sbc` preserves foreign proxies, PAC scripts and pre-existing browser
  policies. A conflicting Chromium policy or failed setup makes `sbc desktop on`
  fail before enabling a new desktop proxy. Policy writes are read back. A protected
  ownership record lets cleanup retry after interrupted or partially failed setup.
  Cleanup deletes only owned values that still match what `sbc` wrote.

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

| Path                          | Contents                                         |
| ----------------------------- | ------------------------------------------------ |
| `cli\`                        | `sbc.exe` CLI executable (on user `PATH`)        |
| `bin\`                        | sing-box executable and runtime libraries        |
| `config.json`, `subscription` | Cached configuration and subscription credential |
| `webrtc-mode`                 | Explicit WebRTC opt-out                          |
| `sing-box.log`                | Runtime log                                      |

The WebRTC opt-out is stored in `%LOCALAPPDATA%\sbc\webrtc-mode`. Policy ownership is recorded
separately under `HKCU\Software\Policies\sbc\WebRTC`, protected against unelevated
writes. Cleanup removes its ownership records and the key when empty.

## Uninstallation

From an ordinary terminal, run:

```powershell
sbc uninstall
```

Approve the browser-policy cleanup prompt if shown. Uninstall first removes owned
WebRTC settings, including the managed Firefox `user.js` block. It then stops the
proxy, removes scheduled tasks, reverts owned proxy and environment settings,
removes the user `PATH` entry, and deletes `%LOCALAPPDATA%\sbc`.

If approval is declined or cleanup fails, uninstall stops and keeps the client and
its recovery state. Resolve the error and retry `sbc uninstall`; do not delete its
folder first. Pre-existing or externally changed browser policies are retained and
listed. Firefox profiles can retain saved preferences; follow the reset steps below.

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
`HKEY_USERS\<Chrome-user-SID>`; obtain that SID with `whoami /user` in the Chrome
user's normal terminal. Reload browser policies afterward. Keep policies required
by your administrator. Edge and Brave use the corresponding keys listed by `sbc`;
do not delete an entire browser policy key.

Firefox copies `user.js` preferences into its saved profile settings. After
`sbc webrtc off` or uninstall, restart Firefox and open `about:config` in each
profile that used `sbc`. Reset these preferences if they came from `sbc`:

- `media.peerconnection.ice.no_host`
- `media.peerconnection.ice.default_address_only`
- `media.peerconnection.ice.proxy_only`
- `media.peerconnection.ice.proxy_only_if_behind_proxy`

`sbc` does not rewrite a running browser's `prefs.js` or close the browser for you.
Check `about:policies` if a preference is still enforced by another policy.
