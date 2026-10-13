# sbc client CLI

Checked on 2026-10-13.

`sbc` is the Go client for Linux, macOS, and Windows. It manages sing-box,
fetches subscription configs, and controls shell and desktop proxy settings.

Install it with the [hosted installer](RELEASE_AND_PORTAL.md#one-liner-install).
For Windows requirements and service details, see [the Windows guide](README_WIN.md).
Linux TUN still uses the [legacy Bash client](TUN_MODE.md).

## Command help

```sh
sbc help
sbc install --help
sbc help speed
```

Help works without an installation. It does not read a subscription link,
contact a server, or perform the command's action. Messages follow `SBC_LANG`
and the locale, in English or Simplified Chinese. Command and flag names stay
in English.

The CLI rejects unknown flags and extra arguments before performing an action.
Existing valid commands retain their syntax and meaning.

| Command                                   | Purpose                                                         |
| ----------------------------------------- | --------------------------------------------------------------- |
| `sbc install`                             | Read a subscription link from stdin and install the client      |
| `sbc init bash`, `sbc init zsh`           | Print shell integration code                                    |
| `sbc link`, `sbc link set`                | Show the masked link, or replace it from stdin                  |
| `sbc on`, `sbc off`                       | Switch shell and opted-in desktop proxies                       |
| `sbc env`, `sbc env off`                  | Print proxy variables for scripts or the current shell          |
| `sbc desktop [on\|off]`                   | Show or switch desktop proxy settings                           |
| `sbc webrtc on`, `sbc webrtc off`         | Manage persistent [browser protection](WEBRTC.md)               |
| `sbc docker [on\|off] [--yes]`            | Show or switch the Linux Docker daemon's proxy                  |
| `sbc route [name]`, `sbc protocol [name]` | Show or switch a choice without restarting sing-box             |
| `sbc route --list`                        | List installed route strategies and their meanings              |
| `sbc port [number]`                       | Show or change the local proxy port                             |
| `sbc status`, `sbc ip`, `sbc speed`       | Show status, exit location, or latency                          |
| `sbc start`, `sbc stop`, `sbc restart`    | Control the service                                             |
| `sbc run`                                 | Run sing-box in the foreground                                  |
| `sbc update`, `sbc upgrade`               | Refresh the config, or install signed binary updates            |
| `sbc uninstall [--yes]`                   | Clean browser settings, stop the service, and remove the client |
| `sbc completion bash\|zsh\|powershell`    | Print a completion script                                       |
| `sbc version`, `sbc --version`            | Print the build version                                         |

`on` and `off` differ from `start` and `stop`. `off` leaves sing-box running.
Desktop settings follow proxy toggles only after `sbc desktop on`.
Docker changes require confirmation or `--yes` because they restart Docker.

Subscription links are credentials. Pass them through the installer prompt or
stdin, not command arguments. `sbc link set` also reads from stdin.

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
Missing or unrecognized proxy-only inlets also cause skips. Run `sbc update` to
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
