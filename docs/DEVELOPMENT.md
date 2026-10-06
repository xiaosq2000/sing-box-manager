# Development

Checked on 2026-10-06.

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
allowlists cover only reviewed synthetic fixture values, and a negative control
checks that they do not hide new credentials. Keep these safeguards
and the GitHub-hosted runner policy described in [SECURITY.md](../SECURITY.md).

## Checks

Choose checks for the files and behavior being changed. The commands below are
available tools, not a sequence required for every edit.

| Change              | Useful checks                                                                                                                         |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Markdown            | `pixi run prettier --check README.md AGENTS.md 'docs/**/*.md'`; check edited commands and local links                                 |
| Doc reviews         | `pixi run docstale check`                                                                                                             |
| Python              | `pixi run lint-python`, `pixi run typecheck`, and the affected tests                                                                  |
| Unix client scripts | `pixi run test tests/test_shell_compat.py tests/test_client_install.py tests/test_setup_proxy_command.py`, plus `pixi run lint-shell` |
| Linux TUN           | `pixi run test tests/test_setup_tun_command.py tests/test_shell_compat.py`                                                            |
| Windows installer   | `pixi run test tests/test_powershell_compat.py` and `scripts/dev/test-install-ps1.ps1`                                                |
| Release and deploy  | Select tests such as `tests/test_release_builder.py`, `tests/test_deploy.py`, `tests/test_provision.py`, and `tests/test_ssh.py`      |
| Rendered configs    | `pixi run fetch-sing-box` once, then `pixi run test tests/test_sing_box_114.py tests/test_desktop_config.py`                          |
| Go client (`sbc`)   | `pixi run format-go`, `pixi run lint-go` and `pixi run test-go`                                                                       |
| `install.sh`        | `pixi run test tests/test_install_sh.py`, plus `pixi run lint-shell`                                                                  |

Use `pixi run test tests/test_routes.py -k download` as the pattern for running a
specific file or case. `pixi run test` runs the full Python suite when shared
behavior or the scope of the change warrants it.

`pixi run style` runs formatters and lint. `pixi run lint` includes Ruff with
`--fix`, so both commands can rewrite files. `pixi run check` also runs typecheck
and the full Python and Go test suites. Inspect the resulting diff and keep
unrelated formatting out of a focused change. For a Markdown edit, formatting just
the edited paths with `pixi run prettier --write PATH...` is sufficient.

`sbc`, in `cmd/sbc` and `internal/`, uses only the Go standard library. Its
`internal/singbox` and `internal/api` tests run the fetched sing-box and skip without
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
environment variables and desktop proxy toggles. Browser-policy fixtures separate
registry keys and cover permission failures, declined approval, read-back
verification, ownership, interrupted cleanup, persistent opt-out and repair while
the proxy is already on. CLI tests verify that failed browser cleanup leaves the
client and proxy available for retry. Concurrency fixtures pause policy or profile
setup and check that opt-out and uninstall cleanup wait for completion.
Firefox fixtures use temporary profiles. They check that profile failures do not
skip healthy profiles and that Unix desktop commands return those failures.
The Windows runner also runs `go test ./internal/winsettings ./internal/desktop`.
Native registry tests use disposable keys, not browser or environment keys. They
check registry types and rollback, and run the fixed WebRTC payload in a disposable
namespace to check ownership and cleanup. Edge migration tests check the supported
policy name, cleanup of owned obsolete values, preserved manual settings and retry
after failed deletion. Subprocess tests use disposable mutex
names to check serialization and recovery after the owning process exits.
These tests do not exercise the UAC dialog.
Validate approval, cancellation and different-administrator hive targeting from an
unelevated Windows account: an elevated runner can hide the `Access is denied`
failure under `HKCU\Software\Policies`.

`internal/e2e` installs `sbc` through `install.sh` on Unix and the hosted
`/install.ps1` on Windows, using a stand-in portal and the real systemd, launchd
or Task Scheduler. It checks installation, route and protocol switches without a
restart, a failed refresh with rollback, and uninstall.

Windows runs the full lifecycle under Windows PowerShell 5.1 and PowerShell 7.
The harness supplies the hidden `Read-Host` answer; the downloaded installer,
executable download and child processes run against the local portal. The
installer runs with `Restricted` execution policy, and the test checks that it
leaves the policy unchanged. `internal/powershell` clears `PSModulePath` when
starting Windows PowerShell to avoid inheriting incompatible PowerShell 7 modules.
Windows also checks user PATH, persistent proxy variables, current-session
PowerShell output, desktop switching and port changes. It checks that browser
protection survives proxy toggles, explicit WebRTC opt-out persists until re-enabled,
and uninstall removes owned policies and ownership records. The Chrome and Edge
regression checks remove each browser policy while the desktop proxy remains on.
They then run `sbc on` to repair the missing policy. Each headless browser probe
uses a fresh profile with loopback HTTP and STUN fixtures. The unprotected control
must gather a fixture server-reflexive candidate. The protected run must complete
ICE gathering without direct STUN. The runner needs Chrome and Edge installed. Each probe starts a new process, including after an
Edge policy change that requires a restart. This IPv4 UDP check runs only in the
gated Windows lifecycle.
The fixture responder alone can be checked without changing host settings:

```sh
pixi run go test -tags e2e ./internal/e2e -run '^TestWebRTCSTUNFixture$' -count=1
```

Windows restores the runner's registry settings even after a failure.
The stand-in's protocols reach a local sing-box that sends traffic out directly. The
test changes the current user's services, so it builds only with `-tags e2e`, runs
only with `SBC_E2E=1`, and refuses to run where `sbc` is installed. The `sbc`
workflow runs it on Linux, macOS and Windows runners:

```sh
SBC_E2E=1 go test -tags e2e -count=1 -v ./internal/e2e
```

`tests/test_install_sh.py` runs `install.sh` with fake `curl` and `sbc`. Its
migration test installs the bash client into a scratch home and runs that client's
own `proxy upgrade` in bash and zsh.

`scripts/dev/test-install-ps1.ps1` tests the Windows bootstrapper with an isolated
`LOCALAPPDATA`, fake downloads and a fake `sbc`. It covers fresh installs,
saved-token migration, retained settings, failure before legacy removal, cleanup
failure and port handoff. It also checks URI validation and the real child-process
stdin and output handling. The `sbc` workflow runs it under Windows PowerShell 5.1
and PowerShell 7 without changing real tasks or registry settings.

`sbc` passes every message through `internal/i18n`, whose catalog holds the Chinese,
keyed by the English. Its test reads the source and fails when a message has no
translation, when a translation formats different values, or when the catalog keeps
a message the code no longer has. Go tests see English whatever the machine's
locale, unless a test asks for Chinese. `install.sh` keeps its translations in its
`t` function, and `tests/test_install_sh.py` checks them the same way.

The shell compatibility tests check the bash 3.2 and Windows PowerShell 5.1
constraints. They do not prove native service or networking behavior. The native
smoke script for the legacy bash client is `scripts/dev/macos-smoke.sh`.
TUN kernel integration still requires a disposable Linux VM, as described in
[TUN_MODE.md](TUN_MODE.md#143-compatibility-and-integration-tests).

Tests use temporary fixtures and mocked external operations for release and deploy
checks. A real `sbm release` decrypts inventory and writes sensitive outputs; a real
deploy contacts the VPS. Neither is needed to validate an ordinary docs or code
edit. Even `deploy --dry-run` can connect over SSH and needs local release artifacts.

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

Route tests also need local socket-pair communication for TestClient's event loop.
A sandbox that blocks socket sends can make even a minimal FastAPI test stall.
Run fixture route checks in an environment that permits local socket communication
before treating that stall as an application failure.

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
DNS, HTTP, and proxy servers. They check every rendered client profile on every
platform it is rendered for, since the desktop configs differ by platform in the
system resolver the `ai` route names, plus desktop startup with failed rule
updates, and DNS persistence and expiry. Routing fixtures cover all four desktop
strategies, direct exceptions with the proxy unavailable, and mobile FakeIP bypass.
TUN DNS and routing fixtures use a loopback mixed inlet; they do not exercise
tunnel interface capture. Supply the custom Linux server binary built with
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
failure, startup rollback and retention of recovery files when rollback fails; it
does not install or stop anything on the host. The runtime tests need permission to bind loopback
sockets. They do not establish native Windows Task Scheduler, macOS launchd, or
Linux TUN kernel compatibility; use the native smoke checks and disposable VM
described above for those behaviors.

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

The hooks in `.claude/settings.json` and `.codex/hooks.json` use
`.pixi/envs/default/bin/docstale hook` for both SessionStart and Stop. Run
`pixi install` before relying on them. SessionStart saves each document's
fingerprint, and Stop reports documents whose sources changed during the session and
that have not been stamped since. A document that was already stale when the session
started does not trigger a reminder, but `check` still reports it. The reminder
suggests the [`docstale-reviewer`](../.claude/agents/docstale-reviewer.md) subagent,
which judges one document with a small model and leaves edits and stamps to the main
agent.

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
