# Node acceptance

Checked on 2026-10-24.

This page records isolated G0 evidence, not a release or deployment approval.
The [M5/M6 specification](MULTI_NODE_SPEC.md#11-acceptance-and-evidence) defines the gates and operator involvement.

| Gate                         | Status               | Evidence boundary                                                         |
| ---------------------------- | -------------------- | ------------------------------------------------------------------------- |
| G0: minimum working system   | Accepted with limits | One Ubuntu VM, one synthetic account, Trojan only                         |
| G1: single-node reliability  | Pending              | Crash/reboot, locking, startup identity and forward repair                |
| G2: accounting and migration | Pending              | Four protocols, collector parity, history and restore                     |
| G3: two nodes and clients    | Pending              | Routing/DNS, native clients and required iOS evidence                     |
| G4: pre-release review       | Pending              | Two-hour stability run, reports and explicit public-network authorization |

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

G0 is accepted with the four unresolved items below. None blocks recording this limited result.
All four items remain open. Broader reliability and accounting claims remain unsupported.

| Item                        | Established facts                                                                                                                                         | Unknowns                                                                                                     | Required follow-up                                                                                                                                        |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Counter sampling            | Two transient failures followed engine starts. The agent does not wait for counter-API readiness                                                          | A startup race is likely. Discarded RPC causes prevent event-level diagnosis                                 | G1: bounded readiness and safe error classes. Test delayed and persistently unavailable listeners                                                         |
| Shutdown and final sampling | A connection-API error occurred during intentional shutdown. Replacement completed, but the agent takes no final old-invocation sample                    | Internal close cause, complete cleanup and any unpolled loss remain unverified                               | G1: final collection before ordinary replacement and explicit failure handling. G2: transition coverage and accounting parity                             |
| Optional HTTPS diagnostic   | No supplemental HTTPS diagnostic outcome is retained. The actual G0 flow checks verified HTTPS                                                            | Supplemental probe status and stderr are unavailable                                                         | Do not count absent output as success. Any future supplemental probe needs appropriate privilege and a retained outcome                                   |
| Retained evidence           | Runtime assertions compare selected numeric database values and reported invocation changes. Individual responses and both query outputs are not retained | Independent database re-audit, full bucket-key/digest equality and accounting completeness remain unverified | G1 evidence: retain response status, body length/hash, both keyed database results and invocation observations privately. G2: independent accuracy checks |

Source boundaries: `node-agent/cmd/sbm-node-g0/main.go` controls startup, collection and replacement.
`node-agent/internal/counter/client.go` reduces RPC failures to fixed errors.
`tests/node_g0/run.py` performs the runtime assertions and retains raw journals privately.
The Ansible playbook installs the controller CA under root-only permissions.

Successful applied-status reporting follows a successful sample, local commit and traffic upload.
The fixture waits for that status before each payload transfer.
This supports recovery from transient startup failures, not a guarantee of complete collection around replacement.
The connection-API shutdown error and the separate counter-query failures have no established causal link.

The result summary is evidence of successful runtime assertions, not a later independent inspection of both databases.
Equal databases can share an upstream undercount. Application body size does not define the engine's counter boundary.
HTTP response headers could explain the observed excess download bytes, but their actual contribution is unverified.
Do not label the difference TLS/network overhead or use it to claim accounting accuracy.

The analysis did not resolve the runtime anomalies. Harness phase diagnostics do not resolve them either.
G1–G4, complete accounting accuracy, iOS acceptance and two-hour stability remain unverified.
The hosted KVM run passes the same limited G0 checks. It does not resolve the four runtime and evidence limits.
Android remains unverified and non-blocking. Failed-start forward repair belongs to G1 without simultaneous credential revocation.
Keep detailed private review artifacts, run paths, raw logs and identity observations outside public source.

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
