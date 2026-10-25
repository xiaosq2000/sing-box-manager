# Node acceptance

Checked on 2026-10-25.

This page records isolated G0/G1 evidence, not a release or deployment approval.
The [M5/M6 specification](MULTI_NODE_SPEC.md#11-acceptance-and-evidence) defines the gates and operator involvement.

| Gate                         | Status               | Evidence boundary                                                                       |
| ---------------------------- | -------------------- | --------------------------------------------------------------------------------------- |
| G0: minimum working system   | Accepted with limits | One Ubuntu VM, one synthetic account, Trojan only                                       |
| G1: single-node reliability  | Accepted with limits | Local VM and hosted runner: final sampling, process crashes, reboot, outages and repair |
| G2: accounting and migration | Pending              | Four protocols, collector parity, history and restore                                   |
| G3: two nodes and clients    | Pending              | Routing/DNS, native clients and required iOS evidence                                   |
| G4: pre-release review       | Pending              | Two-hour stability run, reports and explicit public-network authorization               |

G0 through G3 do not require per-gate approval. Report blockers rather than treating missing evidence as success.
Android device acceptance does not block the first release. Mark it unverified until tested.
The failed-startup check covers forward repair without a simultaneous credential-revocation scenario.
Authentication and access-control requirements remain in scope.

## G0 environment and result

- Node: disposable Ubuntu Noble guest, real systemd, checked SSH host keys and private root files.
- Controller: isolated PostgreSQL 17, Gunicorn HTTPS and a forced-command SSH process on the development host.
- Controller services use private sockets or loopback listeners. They are not host system services.
- QEMU uses restricted user networking. Only the fixture seed and HTTPS controller have outbound relays.
- The HTTP target stays inside the guest. No public DNS or application destination is required.
- Dependency checks pass with Go 1.25.8, Django Ninja 1.7.1, Ansible Core 2.21.5, gRPC 1.79.1 and modernc SQLite 1.50.1.

The latest SQLite driver requires Go 1.26. G0 pins a Go-1.25-compatible driver instead of changing the client toolchain.
The independent `node-agent/go.mod` keeps agent dependencies outside the `sbc` import graph.
Generated counter stubs match the V2Ray wire fields used by sing-box 1.14.3.
The private fixture uses `/node-g0/v1/`, not the production `/node/v1/` contract.
No production route, account table or operator command is changed by G0.

The native check verifies:

1. Ansible installs the two static executables and systemd units over SSH.
2. The node creates its own API secret. SSH registration transfers only its digest and public IDs.
3. The agent verifies HTTPS and obtains its first config.
4. A real Trojan client downloads a known payload through the guest proxy.
5. A runtime assertion compares selected SQLite/PostgreSQL version, upload and download values.
6. Repeated deployment preserves identity and the latest reported engine invocation.
7. The next generation changes the reported invocation and transfers another exact fixture payload.

G0 does not prove full crash safety, production provisioning, public certificate issuance or complete accounting parity.
Its UTC-only bucket and single-entry roster are test constraints, not the final accounting or authorization design.
The G0 executable is not a production agent and must not enter production release artifacts.

## Accepted limits and required follow-up

The accepted G0/G1 scope remains one synthetic account, Trojan and UTC-only buckets.
The following limits do not block that isolated result. They block broader accounting or production claims.

| Item             | Established facts                                                                                                                 | Remaining boundary                                                            |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Counter sampling | A five-second RPC cap, identity checks and real guest delayed/unavailable API checks pass                                         | Four-protocol parity and stream attribution belong to G2                      |
| Final sampling   | Ordinary replacement commits a final sample before stop. Failure leaves the live engine and pointer unchanged                     | Abrupt termination and the sample-to-stop interval can leave unobserved bytes |
| Coverage         | Unexpected invocation changes retain a durable gap warning without inventing a lost-byte count                                    | Warnings are not a complete accuracy measurement                              |
| Evidence         | Private payload status/length/hash and both keyed database snapshots include source, credential, date, version, values and digest | Equal databases can share an upstream undercount                              |

A supplemental HTTPS diagnostic is not acceptance evidence. The actual config flow verifies HTTPS.
The connection-API shutdown error has no established causal link to counter-query failures. Its internal cause remains unverified.
Application body size does not define the engine's counter boundary.
HTTP response headers could explain excess download bytes, but their contribution is not measured. Do not label it TLS/network overhead.
G2–G4, complete accounting accuracy, iOS acceptance and two-hour stability remain unverified.
Android remains unverified and non-blocking. Keep raw logs, identities and run paths outside public source.

## G1 progress

G1 passes locally and in the hosted `node-g0` job within the isolated single-node scope. This does not approve production installation or complete M5/M6.
`counter.Read` waits for transport readiness with a five-second cap. Shorter deadlines and cancellation remain effective.
Terminal service failures return fixed errors without raw RPC text. A later agent cycle can collect again without restarting a healthy engine.

The agent uses two private file locks:

- `agent.lock` rejects a second writer before SQLite opens.
- `lifecycle.lock` serializes staging, final collection, publication, startup and sampling. Its wait is limited to ten seconds.

`config.json` owns the exact bytes. Staged bundle metadata retains their digest without re-encoding the config.
The startup wrapper validates those bytes and executes the immutable generation path.
Its atomic receipt binds generation, digest, systemd invocation and PID. Sampling also checks the real `/proc/PID/exe` inode.
The lock closes at exec. Apply releases it before requesting startup. Unknown live identity prevents automatic mutation.
These are point-in-time process observations, not permanent health or root-compromise attestation.

Ordinary replacement commits the old invocation's final sample before stopping it.
Both MainPID termination and an empty service cgroup are required before publication.
An accounting completion marker records confirmed termination, not a multi-phase apply journal.
Unexpected termination retains a durable coverage warning. Committed checkpoints, source identity and usage survive agent restart and OS reboot.
SQLite, WAL and shared-memory files remain private under an open caller umask.

The proxy is enabled at boot and uses only locally committed policy. The agent attempts local recovery before contacting the controller.
A failed start does not select an older policy. A newer validated generation can repair startup and reset the old systemd failure limit.
Ordinary recovery respects that limit. A failed start cannot produce successful applied-status evidence.
Applied status follows a successful sample, accounting commit and traffic upload, and includes the retained gap count.

Go fixtures cover final sample/commit failures, failed stops, identity guards, local-first recovery, coverage replay and private file modes.
Config fixtures preserve literal `&<>` and insignificant whitespace. Loopback RPC fixtures cover readiness, deadlines, cancellation and safe errors.
The counter, lifecycle and accounting suites pass ten repeated runs.

The local KVM `--g1-checks` run includes G0 and the startup subset, then verifies:

| Fault or transition                             | Required observation                                                                                 |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Wrong receipt, lock contention, duplicate agent | Exact rejection without replacing the working process or pointer                                     |
| Delayed/unavailable counter API                 | Recovery within the RPC bound, or rejection without committing a sample                              |
| Failed final sample                             | No stop or publication. After API recovery, the old final counters commit before replacement         |
| Invalid config                                  | The current working process and pointer remain unchanged                                             |
| MainPID exits but a cgroup child survives       | Publication is refused until the service is empty                                                    |
| Config passes check but fails at runtime        | The failed generation remains selected. A higher generation restores service without false success   |
| HTTPS, PostgreSQL or authentication outage      | Proxy traffic and local collection continue. Upload converges after recovery                         |
| Agent SIGKILL                                   | Automatic restart does not restart the proxy or duplicate committed usage                            |
| Engine SIGKILL                                  | Service recovers and accounting reports the uncertain old tail                                       |
| Agent SIGKILL before pointer publication        | Offline recovery uses the old durable pointer                                                        |
| Agent SIGKILL after publication                 | Recovery uses the new pointer without replaying old policy                                           |
| Guest OS reboot with controller offline         | Boot ID and engine invocation change. Generation, source and committed checkpoints remain consistent |

Private JSON retains the two keyed database results, process observations and boot IDs. Payload records retain response status, length and SHA-256.
Expected failures require exit code 1 and an exact fixed agent error. SSH failure cannot satisfy a negative control.
Runtime-start faults allow only named fixed startup/sampling errors and require systemd evidence that the engine actually failed.
Listener checks use a five-second polling budget. Each SSH probe has a fifteen-second timeout and can complete after that budget.
The payload assertion remains strict.

Crash-boundary tests inject agent process loss through guest-only systemd hooks. The reboot test performs a real guest OS reboot.
They do not establish abrupt VM/host power-loss durability, storage-device fault recovery or lossless accounting.
The single-entry roster, UTC calendar and absent connection-stream collector remain fixture limits. G2 supplies accounting and migration parity.
Stop/suspend policy, complete adoption and the production node contract remain target M5/M6 behavior, not G1 fixture features.

The workflow runs `--g1-checks`. [PR #50](https://github.com/xiaosq2000/sing-box-manager/pull/50) covers the earlier startup subset.
[PR #52](https://github.com/xiaosq2000/sing-box-manager/pull/52) merged the complete isolated G1 result after all five applicable hosted jobs passed at head `08c310c`.
No release, deployment or migration is part of this result.

## Reproduce

Use the locked environment and verified cached artifacts:

```sh
pixi install --locked -e node-g0
pixi run -e node-g0 test tests/test_node_g0.py tests/test_node_g0_harness.py
pixi run -e node-g0 go -C node-agent test -count=1 ./...
pixi run -e node-g0 go -C node-agent vet ./...
```

Prepare the engine with `pixi run -e node-g0 fetch-sing-box-server`.
That command verifies the custom build cache before it publishes the test binary.
Create an adjacent checksum for the fixture runner:

```sh
sha256sum .cache/sing-box-manager/server/sing-box | cut -d ' ' -f 1 > .cache/sing-box-manager/server/sing-box.sha256
```

Use the image preparation in [Development](DEVELOPMENT.md#checks).
The runner verifies the image's adjacent checksum and the engine's checksum before execution.
It never downloads missing artifacts automatically.

```sh
SBM_NODE_G0=1 pixi run -e node-g0 python scripts/dev/node-g0/run.py \
  --engine .cache/sing-box-manager/server/sing-box
```

Add `--startup-checks` for the startup subset, or `--g1-checks` for the complete isolated G1 checks.
The latter includes the startup subset and a real guest OS reboot. The workflow uses `--g1-checks`.
`result.json` records gate `G1` only after every required G1 scenario passes.
The guest requires cgroup v2 and iptables. Counter faults affect only its loopback listener. No host firewall or service changes occur.

The runner requires QEMU, OpenSSH server/client, OpenBSD netcat and OpenSSL on the host.
The basic QMP probe checks KVM startup with the fixture CPU, memory and vCPU settings, otherwise it selects TCG.
It does not initialize the actual VM's disk or network devices. Those must pass the real acceptance run.
A failed acceptance run does not retry with another accelerator.
Root execution is refused. Ansible can modify only a loopback-forwarded guest with the disposable marker.
No operator SSH keys, SSH config, `SBM_CONFIG` or proxy environment enters the fixture.

Each run retains its VM, private keys, databases and logs under `~/.cache/sbm-node-g0/runs/`.
`result.json` records the commit, dirty-tree flag, source fingerprint, checks and limitations.
A dirty-tree run binds evidence to its source fingerprint, not only to its base commit.
Do not upload the whole directory or commit its contents.
Failed runs remain for diagnosis. Removal requires explicit cleanup authorization.

The dedicated GitHub-hosted workflow runs the same contracts, cgo-free module checks and native fixture.
It does not upload or cache private run directories.
The hosted G0 job grants `/dev/kvm` ownership to the runner UID, matching TUN CI.
This changes device access only on the disposable hosted runner. It does not change developer hosts or guest permissions.
Named-user ACL grants can disappear during KVM VM lifecycle events on the tested runner.
The [minimal reproduction](https://github.com/xiaosq2000/sing-box-manager/actions/runs/39971638135) records the runner UID entry disappearing while the ACL mask remains unchanged.
KVM and udev events, uaccess tags and candidate rules support the uaccess path, but do not identify the ACL writer.
Two unreadable rule files limit that observation.
The [hosted KVM G0 check](https://github.com/xiaosq2000/sing-box-manager/actions/runs/40062201993) passes for the simplified fixture at `f08cb99`.
The actual VM completes the full G0 flow using the basic QMP probe and runner ownership grant.
All five applicable PR checks pass at that commit. This verifies the fixture, not the identity of the ACL writer.

A local result does not establish remote acceptance or acceptance of a changed source fingerprint.

The runner prints fixed phase names and the selected accelerator.
On failure, it prints fixed error categories, numeric exit codes, guest readiness attempts and tracked process exit codes.
Raw QEMU and serial logs remain private. Guest readiness stops immediately if QEMU exits.
The last guest readiness outcome distinguishes SSH transport, host-key, authentication and remote readiness failures.
These categories identify observed symptoms, not necessarily root causes.
Raw command output, paths from commands and exception details stay in private run logs.
Credential-negative fixtures check that these values cannot enter public diagnostics.
Diagnostics do not change timeouts, SSH host-key checks, TLS verification or failure outcomes.
