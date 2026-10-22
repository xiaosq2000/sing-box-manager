# Development

Checked on 2026-10-21.

Run commands from the repository root. `pixi install` provides Python 3.12, Go and
the development tools on Linux x86-64, the platform declared in `pixi.toml`. Client
packages also support the platforms listed in the installer guides. Pixi sets
`GOTOOLCHAIN=local`, so Go never downloads another toolchain, and `CGO_ENABLED=0`.

## Public-source checks

Read [CONTRIBUTING.md](../CONTRIBUTING.md) before adding configuration or fixtures.
Keep real inventory and operator state outside public source control, including
SOPS-encrypted inventory. The local `.sops.yaml` is ignored; start from
`.sops.example.yaml` and supply your own public recipient.

```sh
pixi run python scripts/dev/check-public-tree.py
pixi run test tests/test_public_repository.py
bash scripts/dev/check-secrets.sh --staged
# After committing, check all reachable refs as CI does:
pixi run python scripts/dev/check-public-tree.py --history
bash scripts/dev/check-secrets.sh
```

The scanner is pinned by version and SHA-256 and redacts findings. Its narrow
allowlists cover only reviewed non-secret values, including synthetic fixtures
and exact documentation fingerprints. A negative control checks that they do
not hide new credentials. Keep these safeguards
and the GitHub-hosted runner policy described in [SECURITY.md](../SECURITY.md).

## Checks

Choose checks for the files and behavior being changed. The commands below are
available tools, not a sequence required for every edit.

Both CI workflows accept PRs against `main`, `feat/m1b-tun-core` and
`feat/m1b-tun-maintenance`. These branches permit separate maintenance and
native TUN CLI PRs with automated checks. Push events run these workflows only
on `main`.

The TUN job runs `core` and `lifecycle` in parallel on separate disposable
runners. CI grants its runner user access to `/dev/kvm` when that device exists.
`scripts/dev/tun-vm/select-accel.py` checks KVM startup with the acceptance
machine's CPU, memory and vCPU settings. A successful QMP response selects KVM.
An unavailable KVM selects TCG before acceptance starts. The job summary records
the selected accelerator. Each suite runs in full. A failed suite does not retry
with another accelerator. CI does not require TCG coverage on a runner with KVM.

The `test` job caches Go modules, Go build results and the custom server
binary cache. The TUN jobs cache Go modules and Go build results. The `test` job
runs Go tests with `-count=1` to prevent reuse of cached test outcomes. Cache keys
include the Pixi lockfile and build inputs. Only successful pushes to `main` save
these caches. PR jobs can restore them but cannot save them. Cached custom server
binaries still require the build-recipe key and SHA-256 check. No VM disks,
generated credentials or acceptance artifacts enter these caches.

Check these workflow contracts and the accelerator probe with
`pixi run test tests/test_ci_workflows.py tests/test_public_repository.py`.

| Change              | Useful checks                                                                                                                                                                                 |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Markdown            | `pixi run prettier --check README.md AGENTS.md 'docs/**/*.md'`; check edited commands and local links                                                                                         |
| Doc reviews         | `pixi run docstale check`                                                                                                                                                                     |
| Python              | `pixi run lint-python`, `pixi run typecheck`, and the affected tests                                                                                                                          |
| Shared Bash scripts | `pixi run test tests/test_shell_compat.py tests/test_server_install.py`, plus `pixi run lint-shell`                                                                                           |
| Windows installer   | `pixi run test tests/test_powershell_compat.py` and `scripts/dev/test-install-ps1.ps1`                                                                                                        |
| Release and deploy  | Select tests such as `tests/test_release_builder.py`, `tests/test_deploy.py`, `tests/test_provision.py`, and `tests/test_ssh.py`                                                              |
| Rendered configs    | `pixi run fetch-sing-box` once, then `pixi run test tests/test_sing_box_114.py tests/test_desktop_config.py`                                                                                  |
| Django/PostgreSQL   | `pixi run -e control-plane-test test tests/test_control_plane*.py tests/test_routes.py tests/test_deploy.py tests/test_provision.py tests/test_remote_config.py tests/test_server_install.py` |
| Go client (`sbc`)   | `pixi run format-go`, `pixi run lint-go` and `pixi run test-go`                                                                                                                               |
| `install.sh`        | `pixi run test tests/test_install_sh.py`, plus `pixi run lint-shell`                                                                                                                          |

For rendered subscription changes, also run the
[PostgreSQL checks](#control-plane-checks). They verify authenticated subscription
responses, including Linux `config_tun`, separately from renderer and engine tests.

Use `pixi run -e control-plane-test test tests/test_routes.py -k download` as the pattern for running a
specific file or case. `pixi run test` runs the full Python suite when shared
behavior or the scope of the change warrants it.

`pixi run style` runs formatters and lint. `pixi run lint` includes Ruff with
`--fix`, so both commands can rewrite files. `pixi run check` also runs typecheck
and the full Python and Go test suites. Inspect the resulting diff and keep
unrelated formatting out of a focused change. For a Markdown edit, formatting just
the edited paths with `pixi run prettier --write PATH...` is sufficient.
Django HTML templates are excluded from Prettier because its HTML parser changes
template tags. Route tests check that these pages render without unprocessed tags.

`sbc`, in `cmd/sbc` and `internal/`, uses Cobra and pflag for commands, help,
validation, and completion. Other client code uses the Go standard library.
`go.mod` and `go.sum` pin CLI dependencies. No Viper settings layer is used.
Its `internal/singbox` and `internal/api` tests run the fetched sing-box and skip without
it, and its install tests serve a signed release from a local test server. The
`internal/desktop` and `internal/docker` tests run fake `gsettings`, `networksetup`
and `sudo`, and the command tests replace both, so no test reads or changes the
machine's desktop or Docker settings. The `internal/probe` tests and the `sbc ip` and
`sbc speed` command tests use a stand-in proxy on loopback or a stand-in download, so
no test reaches the internet. The `internal/shell` tests source `sbc init`
in every bash and zsh they find, which on the macOS runner includes bash 3.2. CI
builds `sbc` for linux-amd64, linux-arm64, darwin-amd64, darwin-arm64 and
windows-amd64 on every run, and runs its tests on a macOS runner when Go code
changes. The unit tests stand sing-box in with shell scripts, so Windows gets a
`GOOS=windows go vet` there and the end-to-end test below; the `internal/service`
tests cover the scheduled tasks with a recorded `schtasks.exe` and PowerShell.
Windows settings tests use an in-memory registry on Linux and macOS, covering
environment variables and desktop proxy toggles. Browser protection is Linux
Chrome-only, so Windows does not test browser policies or run a WebRTC probe.

Chrome fixtures execute the embedded Linux helper in a temporary policy
directory, without sudo. They cover ownership, exact legacy-file cleanup,
conflicts, changed values, failed writes and retry. Legacy cleanup fixtures
change a file or replace it with a symlink before the helper runs, and check
that the elevated recheck refuses deletion.

Chrome geolocation fixtures execute their separate embedded Linux helper in a
temporary policy directory, without sudo. They cover the default-off choice,
explicit repair, matching external policies, conflicting defaults and URL lists,
read-only diagnostics, ownership, safe cleanup and retry. Helper fixtures change
policy contents, ownership records or symlinks before elevated-operation
simulation. CLI fixtures isolate policy directories, test all three composite
components, preserve the client after cleanup failure, and check Chinese messages.
No fixture changes host Chrome policy. These tests do not establish effective
geolocation blocking in a native browser. Verify `chrome://policy` and denied
website location requests on an authorized disposable Linux runner.

`internal/browserlanguage` fixtures use temporary Chrome roots and policies.
They cover full list replacement, both preference fields, multiple profiles,
absent original fields, external edits, policy conflicts, singleton checks,
symlink refusal, deferred targets and recovery after failed writes. Account
fixtures cover existing `account_values` fields, separate originals, version 1
migration, pending status and removed stores. No test creates missing account
fields during application or restores a removed account store. Recovery records
contain only language fields. CLI fixtures supply temporary language
managers, including for region refresh and uninstall. No unit test reads or
changes a developer's Chrome profiles.

`internal/e2e` installs `sbc` through `install.sh` on Unix and the hosted
`/install.ps1` on Windows, using a stand-in portal and the real systemd, launchd
or Task Scheduler. It checks installation, route and protocol switches without a
restart, policy defaults, old-task migration, interval changes without a proxy
restart, failed refresh and upgrade recovery, first-upgrade notification cleanup,
and uninstall.

Windows runs the full lifecycle under Windows PowerShell 5.1 and PowerShell 7.
The harness supplies the hidden `Read-Host` answer; the downloaded installer,
executable download and child processes run against the local portal. The
installer runs with `Restricted` execution policy, and the test checks that it
leaves the policy unchanged. `internal/powershell` clears `PSModulePath` when
starting Windows PowerShell to avoid inheriting incompatible PowerShell 7 modules.
Windows also checks user PATH, persistent proxy variables, current-session
PowerShell output, desktop switching and port changes. Uninstall removes the
owned proxy and environment settings. Native registry tests use disposable keys.

The Linux lifecycle runs Chrome with a fresh profile and loopback STUN fixtures.
The unprotected control must gather a fixture server-reflexive candidate. The
protected run must complete ICE gathering without direct STUN. It checks positive
controls, protection after proxy toggles, explicit opt-out, repair after policy
deletion, and cleanup after uninstall. A timeout before the browser requests the
probe page permits one restart with a fresh profile and loopback page. Only the
attempt's unique probe URL counts as a page request. The first process must stop
before the restart. Early exits, cleanup failures, ICE failures and privacy
assertion failures do not permit retries. Timeouts report whether the browser
requested the probe page and include captured browser output. Browser sandboxing
stays enabled.

`TestChromeLanguagePreferences` runs only on an explicitly enabled disposable
Linux runner. It launches Chrome with a temporary standard profile, checks the
original language, applies the node language, and checks restoration. A loopback
page reports `navigator.language`, `navigator.languages` and the actual
`Accept-Language` header. The probe sets no language command-line flags and keeps
the sandbox enabled. It disables sync only for the disposable probe, so it does
not establish isolation from real Chrome Sync. The disposable profile has no
signed-in account. It does not validate the effective account preference layer.
Account storage fixtures check writes and recovery. Native account-layer acceptance needs a separate test with
an authorized signed-in profile and unchanged sync settings.

These checks do not launch a browser or change host settings:

```sh
pixi run go test -tags e2e ./internal/e2e -run '^(TestBrowserLanguageProbeCommand|TestWebRTC(STUNFixture|BrowserCommand|BrowserOutput|BrowserStartup(Retry|Timeout)))$' -count=1
```

CLI help and argument-validation fixtures reject calls to installation lookup
or stdin. AI check fixtures cover explicit opt-in, direct-mode skips, direct
permission, proxy-only inlet validation, HTTP denials, challenges, redirects,
unknown regions, and absence of account credentials or automatic retries.
Region fixtures use a temporary `/etc` tree and a fake tool runner. They cover
system scope, absolute and relative time zone links, unchanged tool results,
tool and file fallback, ownership, external changes, partial components, pending
retries and the root state. No test changes the host's time
zone or locale. The common CLI fixture uses temporary system region paths and
rejects sudo calls without a fake runner. Mixed guards also use the injected
capture state, including for region warnings. Without an injected state, the
production guard still uses `tun.Active()`. An unreadable operator journal must
not change fixture results. CLI fixtures check the `--system`
confirmation, activation through a symbolic-link directory, the no-sudo refresh
path and the uninstall restore. The directory fixture covers macOS path aliases
on Linux too.

Route alias fixtures cover old and new installed configs. Provider checks use
loopback stand-ins only, never live provider requests. IP fixtures cover both source
rows, IPv4-only defaults, explicit dual-stack, address-family validation, duplicate
IP lookup suppression, mismatched database responses, unknown fields, bounded
responses, terminal-control removal, HTTP denials, redirects, rate limits, timeouts,
and no direct retry. CLI fixtures check nonfatal detail failures, partial IPv6
results, proxy authentication, unchanged route and protocol, Chinese output, and
combination with the existing AI checks. TUN refusal fixtures include `--dual-stack`
and its AI flag combinations. They refuse before config access or network requests.
No IP test needs native IPv6 connectivity.
Completion fixtures read only a scratch config and use no running proxy.
Native completion tests build a temporary `sbc` and use isolated homes and profiles.
Bash tests cover bare-shell fallback, split equals signs, cursor position, and
interactive-only activation. The macOS job also tests `/bin/bash` for Bash 3.2.
Zsh tests use its completion parser with a stand-in display function.
PowerShell tests use native `CommandCompletion` on each available PowerShell
version. They explicitly test `TabCompleteNext`, `Complete` and `MenuComplete`,
including multi-choice descriptions, tooltips and equals-form flag values.
They run in the Windows job under PowerShell 5.1 and 7 and skip locally
when neither executable is available:

```sh
pixi run go test ./internal/cli -run 'Test(CommandHelp|CLIValidation|Completion)' -count=1 -v
```

Windows restores the runner's registry settings even after a failure.
The stand-in's protocols reach a local sing-box that sends traffic out directly. The
test changes the current user's services, so it builds only with `-tags e2e`, runs
only with `SBC_E2E=1`, and refuses to run where `sbc` is installed. The `sbc`
workflow runs it on Linux, macOS and Windows runners:

```sh
SBC_E2E=1 go test -tags e2e -count=1 -v ./internal/e2e
```

`tests/test_install_sh.py` runs `install.sh` with fake `curl` and `sbc`. Migration
fixtures reproduce an installed client's files, uninstaller, and desktop ownership
interface. `tests/fixtures/legacy-unix/setup.sh` preserves the upgrade caller's
local state and post-install source step. Bash and Zsh checks ensure the one-use
replacement clears that state and prevents legacy restoration. TUN refusal tests
cover both installed legacy clients and missing user-scoped files. No test installs
the retired Bash client or changes the developer's services.

`scripts/dev/test-install-ps1.ps1` tests the Windows bootstrapper with an isolated
`LOCALAPPDATA`, fake downloads and a fake `sbc`. It covers fresh installs,
saved-token migration, retained settings, failure before legacy removal, cleanup
failure and port handoff. It also checks URI validation and the real child-process
stdin and output handling. The `sbc` workflow runs it under Windows PowerShell 5.1
and PowerShell 7 without changing real tasks or registry settings.

Maintenance fixtures use temporary policy files and recorded service commands.
They check independent switches, intervals, saved opt-outs, schedule failures,
startup migration, disabled workers, and manual actions. They verify that native
scheduling failures do not block manual refresh, upgrade, start, or restart.
Invalid settings and recovery records still block these actions. Subprocess tests
check operation locks and recovery after an updater exits during commit.
Startup fixtures check shared probes, old grants during descriptor cleanup,
asynchronous service starts, duplicate engines, occupied controller ports, and
stalled wrappers. Rollback fixtures refuse writes until a failed startup stops.
Unix subprocess tests retain the engine lease across real `exec` and check normal
exit and forced termination. Windows subprocess fixtures check job-based child
cleanup after wrapper termination. Native lifecycle tests check engine leases,
locked startup refusal, duplicate refusal, stop, and start on disposable runners.

Signed-release fixtures check candidate validation and restoration of binaries,
libraries, configuration, and version receipts. Client-only upgrades check staged
configuration and rules with the installed engine, without copying its directory
or retired binaries. Windows tests replace and restore an
executable while both prior versions remain mapped. No local fixture registers a
real maintenance task. Native scheduling checks run only in the gated lifecycle.

TUN maintenance fixtures use a separate typed recovery path. They check complete
candidate preparation, fresh startup selections, user-file publication before
root commit, failed writes and confirmed shutdown before restoration. They also
check missing user journals, lost commit replies, committed-generation recovery
and refusal to recover TUN implicitly through proxy startup. Commit facts survive
shutdown. Stopped committed generations keep their published files. A failed
stop or changed transaction identity retains recovery data. Automatic workers
check signed source fingerprints and update hints without changing runtime files
or invoking sudo. No TUN recovery test uses mixed restart or rollback hooks.

TUN runtime fixtures use separate owner and mixed API endpoints. They check
query-only calls, route aliases, protocol changes, invalid choices, operation
locks, missing or invalid owner state, and unavailable APIs. Pending, dirty or
inconsistent capture metadata blocks runtime access. No TUN failure falls back
to the answering mixed endpoint. IP and speed fixtures refuse TUN before config
access or network requests, including explicit AI and download flags.

`sbc` passes every message through `internal/i18n`, whose catalog holds the Chinese,
keyed by the English. Its test reads the source and fails when a message has no
translation, when a translation formats different values, or when the catalog keeps
a message the code no longer has. Go tests see English whatever the machine's
locale, unless a test asks for Chinese. `cmd/sbc` applies the saved CLI language
before it builds commands; `SBC_LANG` and the locale decide when no choice is
saved. `install.sh` keeps its translations in its `t` function, and
`tests/test_install_sh.py` checks them the same way.

The shell compatibility tests check Bash 3.2 for the shared server scripts and
Windows PowerShell 5.1 for the hosted Windows installer. The Unix installer uses
POSIX sh. These checks do not prove native service or networking behavior.
The `sbc` workflow exercises supported client services on disposable runners.
Linux TUN is implemented for native Linux; its unit tests cover the environment
gate, config patch, staged files, capability probe, transaction journal,
dirty-state recovery, rollback and mixed-mode guard. `scripts/dev/test-tun-vm.sh`
runs the native acceptance check in a disposable QEMU guest. It serves bounded
SOCKS, TLS, DNS and HTTP fixtures from the host and accepts only the guest's
run-specific success marker. The host's services and network stay unchanged.
`scripts/dev/fetch-tun-image.sh` downloads and verifies the default Ubuntu cloud
image. `tests/test_tun_vm.py` checks the fixture DNS/SOCKS allowlist and the
prepared config without starting a VM. `tests/test_tun_lifecycle.py` checks
host refusal, archive hashes, socket matching, reboot markers and checkpoint identity. Each `e2e-tun` matrix job installs QEMU,
fetches sing-box and the image, and runs one complete suite on its own disposable
Ubuntu runner. The script needs `qemu-system-x86_64`, `qemu-img`, `curl`,
`openssl`, `python3`, OpenBSD `nc` and the sing-box binary from `pixi run fetch-sing-box`. Set
`SBC_TUN_E2E_IMAGE` to a prepared Noble cloud image; the default is
`~/.cache/sbc-tun-e2e/noble-server-cloudimg-amd64.img`. It selects KVM when
available and otherwise uses TCG. Set `SBC_TUN_E2E_ACCEL=tcg` to check that fallback
on a KVM host.

```sh
bash scripts/dev/fetch-tun-image.sh
pixi run bash scripts/dev/test-tun-vm.sh
pixi run bash scripts/dev/test-tun-vm.sh lifecycle
```

The guest disk has a sparse virtual size of 12 GiB. This permits repeated
activation with retained generations without filling the base image's small
root filesystem. Host space is allocated as the guest writes data.
The image downloader clears proxy variables, checks SHA-256, and reuses the
verified cache. It downloads about 600 MB on first use. The guest requires `ip`,
`nft`, `dig`, `curl`, Python and CA-management tools. Acceptance installs no
packages and needs no public DNS or websites.

QEMU uses `restrict=on`. Only the asset and SOCKS fixture ports have guest
forwarding rules. The guest cannot use the host as a general internet gateway.
The config comes from the product renderer. Only external dependencies,
certificates, rule-set contents and cache identities change. A guest router
namespace makes the direct target reachable through a non-adjacent subnet.
The proxy-only target is unreachable directly. SOCKS forwards only allowlisted
fixture destinations. TLS uses an ephemeral test CA inside the guest only.
Reserved `.test` names reach the resolver, unlike locally rejected `.invalid`
names. Distinct DNS answers and nonce-bearing responses distinguish direct and
proxy paths in all five modes. The upstream DNS fixtures supply both A and AAAA.
Local real-engine checks verify successful empty AAAA answers and unchanged A
answers in every mode and alias. A negative control restores `prefer_ipv4` and
must receive the fixture's AAAA answer. The VM checks DNS suppression and rejects
forced public IPv6 destinations in every mode, including direct exceptions.
A routed documentation-prefix IPv6 TLS target works before capture, so failed
requests under TUN cannot pass merely because the target has no route. These
checks establish the captured-flow policy, not protection for interface-bound
sockets or Docker bridges. The suite drives route and protocol changes through
the native CLI, including a route alias and round-trip selector changes.
Generation, process ID and installed files stay unchanged across switching.
Distinct responses prove both selector paths. Invalid choices and mixed-only
diagnostics fail explicitly.

A capture-first activation probe does not establish hostname-based routing.
Native domain-route checks need an application TLS flow with SNI. Obtain operator
permission before sending TLS ahead of capture verification. Match the new
connection ID, source/destination tuple, TUN inbound, hostname and expected chain
before sending HTTP on that socket. Load the system TLS context before opening
TCP, so certificate loading does not consume the engine's sniff timeout.
Keep system TLS verification. Do not follow redirects, retry directly, or infer
routing from HTTP status or exit location.
These checks do not enable TUN `ip` or `speed` diagnostics.

Checks cover stop failure, recovery after a dead proxy,
`direct` with an unused dead proxy, and controller death after service startup.
Controller-death cases observe timer recovery before explicit test cleanup.

Maintenance acceptance checks signature rejection without changing the live
process, a signed client-only upgrade, rule-snapshot refresh, and check-only
workers. It preserves a CLI-selected route and alternate selector across root
publication. Distinct SOCKS responses prove the selected path. The alternate
selector uses the `naive` tag but not the Naive transport. After both maintenance
controllers die, the test observes shutdown before explicit user-file recovery.
Proxy startup cannot perform that recovery implicitly. A second fixture pauses
sudo after root commit. It kills the CLI, explicitly stops capture, and checks
that recovery keeps the newly published rule snapshots rather than restoring
superseded files.

A build overlay adds an ephemeral signing key to disposable test binaries only.
Production trust keys and signature checks stay unchanged. The private signing
key remains outside the guest archive. The core guest uses fixture files rather
than the hosted installer or native scheduler. Its client-only upgrade does not
establish an engine-version transition.

The lifecycle suite installs through the hosted Unix installer in a fresh guest.
It verifies pinned official 1.14.2 and 1.14.3 archives and serves two signed test
releases. Set `SBC_TUN_E2E_OLD_ARCHIVE` and `SBC_TUN_E2E_NEW_ARCHIVE` to reuse local
archives. Otherwise, it downloads them without proxy variables into the image
cache's `engines/` directory. Hash mismatch aborts acceptance.

The suite sets the guest's maintenance intervals to one minute through the
product CLI. Real user timers must trigger successful check-only workers and
produce an update hint. PID, generation and installed files must stay unchanged.
A guest manager PATH trap rejects and records any worker sudo attempt. Manual
upgrade then replaces both owner and root engine files and checks their hashes,
version receipts, current selections and captured HTTPS traffic.

The same disk boots four times. A committed generation starts automatically.
Pending and dirty records come from paused commit and failed-stop operations,
not forged journals. They refuse startup after reboot without transient recovery
and keep mixed mode blocked. Explicit `tun off` confirms resource cleanup.
The suite then starts mixed mode and uninstalls the user client and schedules.
Root generations, the helper and the disabled TUN unit remain.

Fixture topology starts before TUN. The controller must not wait for cloud-final:
that service waits for multi-user, which also waits for this controller.
Every boot needs a current run-specific marker, and the guest checks distinct
kernel boot IDs and unchanged files. Contradictory, premature or stale success
markers fail. Per-boot serial logs, packet traces and reviewed-field JSON evidence
stay under the run directory. The `e2e-tun` CI matrix runs both suites in parallel
with the accelerator selected by the startup probe.

A guest-only wrapper shortens the real systemd timer to 15 seconds and pauses
the start command after the service starts. No production skip flag is used.
Unit tests check expired commits, stale timers, user-state write failure,
complete candidates and native systemd unit syntax. Permission subprocesses
check `umask 022` and `077`, with fresh and existing private control directories.
They check the complete published generation, commit and shutdown. The tests do
not change the parent test process's umask.

The guest runs with user and root `umask 0077`. A guest-only sudoers entry sets
the elevated mask, and the script checks its effective value. It checks directory
modes and ownership after activation and maintenance. Public metadata remains
readable without sudo. Config, rule and state access stays restricted. These checks do not change
host sudo policy or establish operator-machine acceptance. Workstation console
acceptance separately covers private-umask activation, five modes, real
Trojan/Hysteria2/Naive traffic, refresh and noninteractive check-only workers.

Local fixture checks need no VM:

```sh
pixi run test tests/test_tun_vm.py tests/test_tun_lifecycle.py tests/test_desktop_config.py
pixi run go test ./internal/tun ./internal/cli ./internal/api ./internal/service
```

Each VM run retains its disk, generated assets, guest-only packet trace and
serial log under the image cache's `runs/` directory. These contain synthetic private keys and credentials.
Do not upload them wholesale. The script prints the run directory and failure
markers. TUN IP and speed diagnostics remain unsupported. Public IPv6 access is
disabled. The IPv6 fixture checks establish DNS suppression and rejection, not
full IPv6 connectivity. Docker bridge and `SO_BINDTODEVICE` compatibility remain
unvalidated. VM selector transports remain SOCKS fixtures. Physical workstation reboot, production timer
migration and a production client release are not established by these suites.

Core acceptance passes locally with sing-box 1.14.3 and the Noble fixture under
KVM and TCG. The [GitHub CI run](https://github.com/xiaosq2000/sing-box-manager/actions/runs/39415310872)
also passes under TCG. Maintenance acceptance passes locally under KVM and TCG
with sing-box 1.14.3 and the Noble fixture. Native route/protocol CLI switching
also passes locally under KVM and TCG. Lifecycle acceptance passes locally under
both accelerators, including a real 1.14.2 to 1.14.3 engine upgrade. Together with
console acceptance, these results meet the [M1b completion scope](REFACTOR_SPEC.md#m1b-acceptance-scope).

Tests use temporary fixtures and mocked external operations for release and deploy
checks. A real `sbm release` decrypts inventory and writes sensitive outputs; a real
deploy contacts the VPS. Neither is needed to validate an ordinary docs or code
edit. Even `deploy --dry-run` can connect over SSH and needs local release artifacts.

## Control-plane checks

The Django portal and control plane live in `sing_box_manager/control_plane/`. Production uses
OS-managed PostgreSQL and the default Pixi environment. The `control-plane-test`
environment adds PostgreSQL 17 tools and shares the runtime dependency resolution.
It does not install or start a system database service.

```sh
pixi run -e control-plane-test test tests/test_control_plane*.py tests/test_routes.py
```

Tests start a temporary cluster with no TCP listener, a private socket directory,
and synthetic operator records. Child environments exclude operator credentials.
They cover Django migrations, admin permissions, CSRF, host checks, static files,
repeat provisioning, restricted database privileges, backup/restore, idempotent
inventory import, config parity, subscription and download serving, and single-node
config sync. Reliability checks cover account-wide disablement, separate portal
access, migration of legacy switches, import refusal in authoritative mode, Admin
commit/rollback callbacks, and read-only reset counters. Sync fixtures cover locking
before database reads, rejected configs, restart recovery, and recovery failure.
Mocked restart and identity tests do not start PostgreSQL. Database tests also verify
traffic collection for accounts that exist only in PostgreSQL.
Deployment fixtures exercise both authority transitions, Nginx rollback, and JSON-equivalent no-op sync.
Django Admin login checks send a browser-style `Origin` header and check the `same-origin`
referrer policy. They reject null and cross-site origins, plus missing or incorrect
CSRF tokens.
The default test environment skips these integration checks without PostgreSQL
tools. CI sets `SBM_REQUIRE_POSTGRES=1`, so missing tools fail its dedicated job.
Deployment fixtures cover enablement on existing hosts, failure before success
metadata, dry runs, disablement without data removal, and `--skip-code`.
The dedicated CI job also checks native PostgreSQL peer authentication and the
hardened systemd service. `scripts/dev/test-control-plane-native.py` runs only on
an explicitly enabled disposable GitHub runner. It verifies repeat deployment,
read-only assets, peer-authenticated root sync, and trigger-driven account disable/re-enable.
It installs the pinned official sing-box binary, checks config preservation and no-op sync,
and checks whole-portal Nginx routing across authority transitions. It rejects public
Admin access, stops the retired Web service, and verifies 503 when Django is disabled.
Node installation must finish within 60 seconds in this fixture; this does not measure
production interruption.
Database records survive authority exit and disablement. Native node checks keep traffic
stats off; the separate runtime job tests the custom counter binary.
Its local unit test mocks all host commands. Do not run the native script on an operator machine or VPS.
See [the operator guide](CONTROL_PLANE.md).

## Traffic accounting checks

Traffic values and calendar rules live in `traffic_models.py`, SQLite transactions
and migrations in `traffic_store.py`, and boundary matching in
`traffic_reconciliation.py`. `connection_tracking.py` handles connection lifecycle
and pending batches. `connection_stats.py` handles subscriptions and flushing.
The existing CLI and portal provider remain in `traffic_stats.py`.

The collector tests use synthetic protobuf frames, a controlled clock, and temporary
SQLite databases. They cover failed writes, commit retries, process restart, replay,
zero-byte counts, live snapshot expiry, and completed-day compaction. The schema
migration fixture is the previous version's schema with synthetic usage.

```sh
pixi run test tests/test_connection_stats.py tests/test_connection_collector.py tests/test_traffic_stats.py tests/test_traffic_stats_billing_day.py tests/test_traffic_stats_domains.py tests/test_traffic_reconciliation.py tests/test_traffic_store_migrations.py
```

`tests/test_routes.py` uses Django's test client in isolated PostgreSQL processes.
It checks database-only accounts, signed-download allowlists, legacy Basic/Bearer
migration, token revocation, traffic-viewer permissions, personal-domain privacy,
cache headers, and refusal to use inventory when PostgreSQL fails.

## Mobile import checks

`tests/test_mobile_import.py` decodes locally generated PNG QR images with ZXing.
It checks the unified import URI, Unicode names and absence
of network access during generation. Node.js fixtures exercise clipboard success,
denied permission, and manual-copy fallback. Pillow and ZXing are development
dependencies. Segno generates the images at runtime.

The PostgreSQL route fixtures verify one import entry, decoded QR contents,
authenticated multi-protocol JSON, ETag responses, disabled protocols, host checks
and token revocation. They reject per-protocol mobile URLs. No test uses an operator subscription or changes a device.

```sh
pixi run -e control-plane-test test tests/test_mobile_config.py tests/test_mobile_import.py tests/test_routes.py
```

These checks do not replace [physical-device acceptance](README_MOBILE.md#acceptance-boundary).
Record app and embedded core versions separately. Mark unavailable values as unknown.
Unified-profile device acceptance is not complete on iOS or Android. Native app
controls, refresh, real-DNS domain routing and TUN capture need device checks.

## sing-box version checks

`pixi run fetch-sing-box` downloads the sing-box release that `sing_box_version` in
`config/inventory/example.yaml` names, for this machine. It verifies the archive
against the digest GitHub publishes and links its files into
`.cache/sing-box-manager/bin/`. Keep that version equal to the deployed one. CI
fetches it on every run.

Use the binary to generate its JSON Schema, since the website schema can change
after a release:

```sh
SBM_SCHEMA_DIR=$(mktemp -d)
.cache/sing-box-manager/bin/sing-box schema -o "$SBM_SCHEMA_DIR/sing-box.schema.json"
```

Associate that file with fixture configs in your editor. Schema validation helps
with fields and types, while `sing-box check` also verifies runtime options and
build features. Run checks on rendered fixture configs with the intended state
directory, using `sing-box check -D /path/to/fixture-state -c /path/to/fixture.json`.
The command loads local rule sets but does not load a remote rule's `initial_path`,
so initial files also need a startup check.

The tests in `tests/test_sing_box_114.py` use generated certificates and loopback
ports, while `tests/test_server_egress.py` verifies that the server route resolves
hostnames through local DNS, pins UDP associations, and rejects private and reserved
destinations.
DNS, HTTP, and proxy servers. They check every rendered client profile on every
platform it is rendered for, since the desktop configs differ by platform in the
system resolver that `ai-only` and `direct` use, plus desktop startup with failed rule
updates, and DNS persistence and expiry. The SOCKS stand-in validates protocol frames
before recording a target, so unrelated HTTP probes cannot create false proxy
traffic. Engine fixtures wait for the current process's startup message, not
only its first TCP listener. Earlier log entries cannot establish readiness.
Routing fixtures cover all five desktop
strategies and the three legacy alias modes, direct exceptions with the proxy
unavailable, and mobile real-DNS routing. `direct` tests verify system DNS and direct
AI connections with the remote proxy both available and unavailable.
Dedicated Anthropic fixtures test proxy routing and DNS ahead of AdGuard and
China-direct matches. Desktop fixtures also test failed updates and no direct
fallback when the proxy is unavailable. `tests/test_mobile_config.py` checks all five canonical modes for every enabled
protocol, both with and without a working proxy. It compares mobile routing and
DNS policy with desktop output. It checks distinct selector paths, real DNS
answers after mode changes, restart persistence, case handling and rejection of
the removed `Rule` mode. It also checks mobile storage and the five-name list.
Mobile TUN DNS and routing fixtures use a loopback mixed inlet. They do not
exercise tunnel interface capture or provide Linux TUN support. Supply the custom Linux server binary built with
`with_v2ray_api` to include all the single multi-inbound traffic-statistics config:

```sh
export SBM_TEST_SING_BOX_SERVER=/absolute/path/to/custom/sing-box
pixi run test tests/test_sing_box_114.py
```

`tests/test_desktop_config.py` covers the desktop config `sbc` will run, which
holds every route as a Clash mode and every protocol behind one selector. Its
structural tests compare each mode with the single-route config for that route.
Its runtime tests switch modes and the selector through the Clash API and apply
the same routing checks to each mode.

The tests run the fetched binary, or the one `SBM_TEST_SING_BOX` names, and skip
when neither exists. The server-with-stats test skips without `SBM_TEST_SING_BOX_SERVER`. CI builds the
pinned custom server binary too and sets `SBM_REQUIRE_SING_BOX=1`, so missing real
binaries fail rather than skip in CI. Locally, prepare and select that build with:

```sh
pixi run fetch-sing-box-server
export SBM_TEST_SING_BOX_SERVER="$PWD/.cache/sing-box-manager/server/sing-box"
pixi run test tests/test_sing_box_114.py tests/test_m3_profiles.py
```

`tests/test_m3_profiles.py` compares every remaining profile with the example-only
golden outputs in `tests/fixtures/profiles/`. Production release rendering and
the golden checks use the same client-profile builder. The pinned-binary server
checks cover enabled, empty and entirely disabled Naive rosters, both with and
without traffic stats. The profile tests also check shared API accounting,
stream replay, protocol separation, and redaction of failed checker output.
`tests/test_m3_runtime.py` runs one real node process and loopback clients for all
three protocols, verifying authentication and both collectors' per-protocol usage.
Include it in the command above to run that integration check.
`tests/test_server_install.py` runs a scratch copy of the server installer with a
fake systemd and a scratch filesystem. It bypasses only the root-user guard in
that copy and tests legacy-unit migration, reinstall, protocol selection, validation
failure, startup rollback, and retention of recovery files when rollback fails.
It also executes the deployment command with an obsolete release installer, checks
that database-managed users survive repeated installs, and exercises the shared sync
lock. It does not install or stop anything on the host. The runtime tests need permission to bind loopback
sockets. They do not establish native Windows Task Scheduler, macOS launchd, or
Linux TUN kernel compatibility. Native `sbc` lifecycle checks run on disposable
CI runners. Linux TUN end-to-end checks run in a disposable QEMU guest with
`scripts/dev/test-tun-vm.sh`.

## Generated protobuf stubs

`sing_box_manager/proto/sing_box_api.proto` is a trimmed copy of the sing-box API
service definition, pinned to the `sing_box_version` the config builders target. It only
reproduces the connection-tracking messages, and its `package` and `service` names must
stay verbatim because they decide the gRPC method path. Regenerate after editing it:

```sh
pixi run gen-proto
```

The generated `*_pb2*` modules are committed so deploying and CI need no protoc, and are
excluded from Ruff and `ty` because protoc rewrites them wholesale in its own style.
`tests/test_proto_stubs.py` regenerates and diffs them, so a stale checked-in stub fails
the suite rather than surfacing at runtime. Revisit the proto on every sing-box version
bump.

## Documentation reviews

[`docstale.toml`](../docstale.toml) maps each document, or a glob of documents, to
the source patterns that may require a review. Keep the mappings aligned with the
context table in [`AGENTS.md`](../AGENTS.md), and add mappings when introducing a new
documented area. [`docstale.lock`](../docstale.lock) records a fingerprint of each
document's sources at its last review.

```sh
pixi run docstale check
pixi run docstale stamp <document>
```

`check` returns `0` when no review is needed, `2` when a document's sources changed
since its stamp, and `1` for a configuration or Git error. It also requires every
configured document to exist and warns about source patterns that match no file.
Read each listed document, update facts an operator or contributor would act on, and
then stamp it. A review may conclude that no edit is needed, but editing a document
does not record a review. The check does not verify the prose.

The Claude Code and Codex hooks in `.claude/settings.json` and `.codex/hooks.json`
run `.pixi/envs/default/bin/docstale hook` for both SessionStart and Stop. Pi loads
the docstale package from `.pi/settings.json` after project trust, and
`.pi/extensions/docstale-executable.ts` points its adapter at the same command. Run
`pixi install` before relying on any of them. SessionStart saves each document's
fingerprint, and Stop reports documents whose sources changed during the session and
that have not been stamped since. A document that was already stale when the session
started does not trigger a reminder, but `check` still reports it. The Claude Code and
Codex reminder suggests the
[`docstale-reviewer`](../.claude/agents/docstale-reviewer.md) subagent, which judges
one document with a small model and leaves edits and stamps to the main agent; the pi
reminder omits that instruction.

The `.pi/settings.json` package is pinned to the docstale commit in `pixi.lock`. Move
both pins together when upgrading docstale.

The baseline survives session resume and compaction. If it is missing, the first hook
call saves the current state without checking for changes. Hooks report each document
once per source state and let a `stop_hook_active` continuation finish. Reading a
document does not record a review. A quiet hook does not prove that a document was
reviewed or that an operation passed.

Use `pixi run docstale disable` or `pixi run docstale enable` to toggle automatic hooks
for this checkout. Manual checks and stamps still run while disabled. Baselines,
acknowledgements, and the switch live under Git metadata, outside the working tree.

## Agent instructions and skills

Keep project constraints and document routing in `AGENTS.md`, and keep operator
procedures in the relevant guide. The repository currently contains no project
`SKILL.md` files. Skills installed in an agent's personal configuration or plugins
are separate from this checkout.

Add a project skill only for a repeated task that needs guidance beyond the existing
docs. Give it a short name and a description that identifies the specific trigger.
Keep essential constraints in `SKILL.md` and link to existing docs or supporting
references for details. Avoid duplicate skills, blanket document-reading rules,
model-specific rituals, and approval steps for work already authorized by the user.

When reviewing instructions, check a small docs edit and a behavior change against
them. The docs edit should reach a checked diff without requiring a release or full
test suite. The behavior change should reach relevant verification and doc updates.
A request to prepare a deploy should produce a reviewable target and command; an
already authorized deploy should continue using that authorization.

For background on keeping instructions focused, see the
[explainx guide](https://explainx.ai/blog/gpt-6-astra-skills-agents-md-prompting-guide-2026).
OpenAI's [prompting guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra#prompting-best-practices)
also recommends auditing conflicting instructions, respecting user intent, and
matching verification to the change.
