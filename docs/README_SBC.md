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

Completion suggests commands, flags, and fixed values such as `on` and `off`.
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

This does not apply proxy variables to an existing PowerShell session. For that,
use `sbc env | Invoke-Expression`, as described in the
[Windows guide](README_WIN.md#current-session-environment).
