# Mixed-to-TUN environment migration

Checked on 2026-10-22.

This specification defines client behavior and acceptance checks.
See [Linux TUN](README_SBC.md#linux-tun) for operation.

## Environment boundary

An application inherits environment variables when its parent starts it.
Removing `env.sh` does not change an existing process's environment.
A child shell can inherit proxy exports without the parent's shell-local bookkeeping.
A long-running application can pass these exports to later child processes.

After Mixed stops, those processes can still request its closed loopback port.
TUN does not convert a failed local proxy connection into a connection to the intended destination.
A shell hook cannot change other running processes.
Subscription requests and activation checks ignore proxy variables.
Their success does not establish that application environments are clean.

## Requirements

| Requirement                     | Boundary                                                                                                                                  |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Transparent TUN                 | TUN injects no proxy variables or exported variables that identify capture state.                                                         |
| No compatibility listener       | TUN does not preserve the Mixed port as a compatibility inlet. Mixed and TUN remain mutually exclusive.                                   |
| Migration can require a restart | Users can restart affected applications after cleaning the environment used to start them. sbc does not restart applications or sessions. |
| No routine restart              | After migration, ordinary TUN activation and shutdown do not require application restarts.                                                |
| No reintroduction               | `sbc env` and shell integration must not enable Mixed exports during active or uncertain capture.                                         |
| Preserve foreign settings       | Automatic cleanup preserves unrelated proxy settings. Such settings limit the transparency guarantee and need a warning.                  |
| Generic guidance                | CLI messages describe process environments. Documentation uses tmux and Herdr as persistent-session examples, not special integrations.   |
| Limited visibility              | Diagnostics inspect only the current command's inherited environment. They do not claim system-wide cleanup.                              |

Transparency means the absence of sbc proxy configuration in application environments after migration.
It does not hide interfaces, routes, DNS behavior, or exit addresses.
It does not guarantee that existing connections survive migration.
Using Mixed again can introduce the same migration requirement.

## Command behavior

| Command or operation    | Behavior                                                                                                                                                   |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sbc tun on`            | Warn before stopping Mixed or clearing its settings. Also warn about inherited sbc values when `env.sh` is absent.                                         |
| Activation result       | Keep existing checks and exit status. Warnings do not block activation or add confirmation. Success establishes capture, not cleanup of every application. |
| `sbc status` under TUN  | Warn about recognized sbc values and unrelated or unknown values. Do not change capture status because of these warnings.                                  |
| `sbc env`, `sbc env on` | Refuse active, pending, dirty, or inconsistent capture before config access. Return nonzero, with diagnostics on stderr and no exports on stdout.          |
| `sbc env off`           | Keep explicit cleanup available. It prints code but cannot change its caller without evaluation.                                                           |
| Shell integration       | Remove recognized residual values without an inherited marker. Block Mixed exports even if a stale `env.sh` exists.                                        |
| `sbc tun off`           | Keep Mixed disabled. Do not recreate proxy exports or require restarts for clean applications.                                                             |

Rejecting `sbc env` does not clear variables that the caller already holds.
`sbc env off` explicitly clears supported proxy variables, including foreign values.
Automatic cleanup uses narrower ownership checks than this explicit command.
Existing Mixed controls remain available outside TUN.
Shared shell code supports Bash 3.2 and Zsh.
The prompt hook starts no subprocesses.
It suspends `allexport` for private bookkeeping and restores the caller's option.
It removes export attributes from private variables and Bash helper functions.

## Ownership and capture checks

- Reuse the private Mixed config for exact expected name/value pairs.
- Keep shell-local snapshots of values applied from `env.sh`, including values from a later port change.
- Do not export these snapshots or add a persistent credential record.
- Remove a variable only when its current value matches a recognized sbc value.
- Preserve user changes and unrelated fallback proxies such as `all_proxy`.
- Do not infer ownership from a loopback address, common port, or generic `no_proxy` list alone.
- Remove a matching bypass list only with a matching sbc proxy URL or an applied shell-local snapshot.
- Cover uppercase and lowercase forms.
- Diagnose unknown historical values without guessing their origin.
- Never print proxy URLs, credentials, or complete environments in diagnostics.

The hook reads existing public capture markers and the root transaction journal through shell builtins.
Only a complete canonical `off` journal permits exports when TUN configuration exists.
The hook checks RFC3339Nano syntax, precision, calendar dates, time ranges, and time zone offsets.
The root helper uses its process time zone, which need not be UTC.
Missing, unreadable, or unknown metadata blocks exports conservatively.
The native initialization check also retains a service-state refusal until the journal changes.
If repair leaves identical metadata, evaluate `sbc init` again to repeat that native check.
No privileged lifecycle changes or new root markers are required.

Generated `sbc init` code contains expected proxy values from private configuration.
Treat this code as sensitive, even though its bookkeeping variables are not exported.

## User guidance

Main warning, before Mixed shutdown:

> Running programs can retain Mixed proxy settings after the switch to TUN.
> Clean the environment used to start affected programs.
> Then restart those programs.

Chinese translation:

> 切换到 TUN 后，已运行的程序可能仍保留 Mixed 代理环境。
> 请先清理启动环境，再重启受影响的程序。

CLI warnings appear on stderr, not in desktop notifications.
Another application can display them as command output.
Printing a warning does not prove that the user reads it.

Migration procedure:

1. Save work before migration interrupts Mixed.
2. Activate TUN at the machine's console.
3. Load current shell integration in the shell that starts affected applications.
4. Check persistent session services for stale values used to create new processes.
5. Restart affected applications from the clean environment.
6. Check that new children have no sbc proxy variables and can connect through TUN.

Use `eval "$(sbc init bash)"` in Bash or `eval "$(sbc init zsh)"` in Zsh.
An existing shell retains old hook code until it evaluates the new integration or restarts.
Cleanup in an unrelated child process does not repair its parent.
For tmux or Herdr, reconnecting does not change existing processes' environments.
Run `sbc status` from a newly created pane to check the environment used for new children.
If Python 3 is available, this child check prints names only:

```sh
python3 -c 'import os; print("\n".join(name for name in sorted(os.environ) if name.lower().endswith("_proxy")))'
```

No printed names means this child has no proxy variables, not that every process is clean.
Restart a persistent service only if no other method can clean its process-creation environment.
Do not prescribe blanket session shutdown, which can stop unrelated work.

## Acceptance checks

Use synthetic credentials, disposable processes, and the isolated QEMU guest.
Do not change a developer's network or application environment.

| Check                       | Required result                                                                                                                                                                     |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Original shell              | The next hook removes unchanged sbc values after Mixed settings are withdrawn.                                                                                                      |
| Inherited or manual exports | Known values can be cleaned without a hook marker after `env.sh` removal.                                                                                                           |
| Foreign or changed values   | Automatic cleanup preserves them. TUN diagnostics report names, not values.                                                                                                         |
| Persistent parent           | File removal does not change its environment. A later child still inherits stale values.                                                                                            |
| Clean replacement           | A new process from the clean source receives no sbc proxy variables.                                                                                                                |
| Export guard                | Unsafe capture refuses exports. Explicit cleanup remains available.                                                                                                                 |
| Stale settings file         | Initialization and later hooks do not apply Mixed exports during active or uncertain capture.                                                                                       |
| Metadata guard              | Canonical off, pending, dirty, active, malformed, unreadable, and legacy metadata have conservative coverage.                                                                       |
| Ordinary TUN cycle          | Clean applications need no restarts or new proxy variables. Shutdown does not restore Mixed.                                                                                        |
| Network acceptance          | An ordinary request succeeds through TUN without exports or a proxy-bypass option. A stale parent's later request fails.                                                            |
| Regression coverage         | Failed sourcing preserves foreign values and previous snapshots. `allexport` exports no private state. Mixed, Bash 3.2, Zsh, completion, and region settings retain their behavior. |

The core VM suite deliberately introduces Mixed variables before activation.
It retains a persistent parent as a negative control, then tests a clean replacement.
The guest also checks confirmed-off journals from a non-UTC root publisher.
No tmux or Herdr installation is needed for this inheritance check.

## Scope

The change concerns `internal/shell/`, CLI guards and diagnostics, translations, fixtures, and documentation.
It adds no server, protocol, DNS, routing, or privileged lifecycle redesign.
It adds no compatibility listener, process injection, global process scan, or restart automation.
It does not change tmux, Herdr, or installation defaults.
Release, deployment, and live network changes remain separate operations.
