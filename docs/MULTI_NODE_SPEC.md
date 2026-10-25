# Node agent and multi-node specification

Checked on 2026-10-25.

Status: target contract for M5/M6, not production authorization.
The [acceptance record](NODE_ACCEPTANCE.md) separates isolated G0/G1 evidence from pending product behavior.
All commands and interfaces in this document are targets unless identified as existing.
The [roadmap](REFACTOR_SPEC.md#delivery-order) puts M5 and M6 before device management.

Current implementation note (2026-10-25): migration `0006_node_source_registry`
adds dormant `Node`, `TrafficSource`, and `Credential` identity metadata only.
Migration `0007_traffic_snapshot` adds an unexposed `TrafficSnapshot` table and
`ingest_page` service that validates codec pages, commits snapshots in
PostgreSQL, and returns receipts after commit. These migrations implement only that local subset; the target
contract below remains the target. There is no HTTP route, bearer resolver,
node/source registration or binding, exporter/ack scheduler, local ledger,
policy generation, calendar inference, retention or history import.

## 1. Design decisions

Optimize lifetime complexity, maintenance and performance, not the size of the first diff.
Use mature components where they remove custom machinery. A new dependency must have a specific responsibility.
Remove replaced code after acceptance. Do not maintain two architectures indefinitely.
Preserve useful data and security requirements, not every implementation detail.

| Area                | Target                                                                                  |
| ------------------- | --------------------------------------------------------------------------------------- |
| Control plane       | Django, Django Ninja and PostgreSQL on the current VPS                                  |
| Central storage     | PostgreSQL for policy, subscriptions, traffic history and ingestion state               |
| Deployment          | Local `sbm` invokes pinned Ansible roles over OpenSSH                                   |
| Operator management | Fixed management commands over SSH, using host authorization                            |
| Nodes               | Debian/Ubuntu with systemd, initially linux-amd64                                       |
| Agent               | One cgo-free Go executable, `sbm-node`, including traffic collection and upload         |
| Local storage       | SQLite through `modernc.org/sqlite` for accounting checkpoints and snapshots            |
| Node bootstrap      | Install a permanent node credential through SSH. No public enrollment API               |
| Node transport      | Outbound HTTPS with a node-specific bearer credential                                   |
| Config rendering    | Synchronous, transactional rendering into one current immutable bundle per node         |
| Config application  | Validate first. Publish forward only. No automatic replay of an older roster            |
| User access         | Account subscriptions with node-scoped protocol credentials                             |
| Client contract     | Version 2 envelope with an explicit upgrade boundary, independent of devices            |
| Ordinary routing    | One node/protocol selector with an automatic pool                                       |
| Proxied AI routing  | One explicit node with verified stable egress. No cross-node fallback                   |
| Accounting          | Counter-authoritative totals, separate stream attribution, versioned absolute snapshots |
| Privileges          | Retain root-based server operations. No agent/applier split in M5/M6                    |
| Protocols           | Trojan, Hysteria2, Naive and optional Snell v6. No VLESS-Reality in M6                  |

No Kubernetes, Redis, Celery, message broker, private CA service or new control-plane host is required.
No public user registration, login password, quota enforcement, payments or automatic VPS/DNS provisioning is included.
The agent cannot install software or execute commands supplied by the control plane.

### Architecture and operating cost

```text
operator workstation
  sbm -> Ansible/OpenSSH -> control-plane management + node provisioning
  signing key and SSH keys remain here

clients -> HTTPS subscriptions -> Nginx -> Django/Ninja -> PostgreSQL
   |                                        ^
   | ordinary pool / fixed AI node          | HTTPS config/status/traffic
   v                                        |
each node: sing-box <---- local control ---- sbm-node
               |                            |
               +---- loopback gRPC ----------+---- local SQLite
```

The central service groups remain Nginx, Gunicorn/Django and PostgreSQL.
Each node has two persistent application processes: `sbm-node` and `sing-box`.
Systemd, SSH, certificate renewal and OS services remain host dependencies.
There is one central database and one local accounting database per node.
Private identity files and immutable config directories are also durable state.

Nodes need no project Python environment, Pixi, Django, PostgreSQL or client archive store.
Ansible modules and Certbot can require distribution Python. This is not a claim that nodes contain no Python installation.
The operator gains an Ansible dependency. The agent gains gRPC/protobuf and pure-Go SQLite dependencies.
These dependencies replace custom deployment plumbing and storage machinery, not proxy semantics.

Keep the agent in a separate Go module under `node-agent/`.
Its dependencies must not enter the `sbc` runtime import graph.
The existing client dependency policy and cgo-free builds remain unchanged.

### Accepted and rejected alternatives

| Alternative                                     | Decision and reason                                                                                         |
| ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| One PostgreSQL central store                    | Accept. One transaction can authorize and ingest traffic. Backups no longer span two central databases      |
| SSH identity installation                       | Accept. Existing host trust removes enrollment expiry, consumed receipts and public recovery                |
| Ansible roles                                   | Accept. Replace custom remote provisioning, quoting and file/service orchestration                          |
| Django Ninja                                    | Accept. Reuse Pydantic schemas for validation and OpenAPI, without another service                          |
| Python agent                                    | Reject. Reusing collectors reduces porting work but retains a project interpreter environment on every node |
| Go agent plus Python collectors/exporter        | Reject. It preserves two application runtimes and cross-process coordination                                |
| No local database / JSON outbox                 | Reject. Offline durable accounting still needs transactions, checkpoints and crash recovery                 |
| Stream-only accounting / official server binary | Defer. Bounded replay and lost events do not meet counter parity                                            |
| Remove domain and destination reporting         | Reject. This is a feature loss, not a necessary architectural simplification                                |
| Automatic config rollback                       | Reject for the new agent. Forward recovery avoids restoring retired credentials                             |
| Render worker and durable render queue          | Reject initially. Transactional rendering removes another process and publication race                      |
| Permanent v1 projection                         | Reject. Use an explicit client upgrade boundary, not device-dependent compatibility                         |
| Flatten ordinary selection                      | Accept. All node/protocol pairs remain selectable without one persistent selector per protocol              |

Performance and package-size claims require measurements. No numerical savings are established by this design.

## 2. Ownership and identity

PostgreSQL owns accounts, node settings, grants, recoverable protocol secrets, subscriptions, bundles, revocations and central traffic.
The operator owns SSH access and release signing keys.
Each node owns its TLS private keys, API credential, current bundle and local accounting state.
A local inventory is bootstrap input, not a second writable copy of established node policy.

Separate the public control-plane origin from node endpoints.
Changing a node must not change the portal origin, allowed hosts or subscription links.
Keep Admin private to loopback and SSH tunnels.
Never distribute database access, portal signing keys, operator passwords or release signing keys to a node.

### Logical records

Names describe responsibilities, not a required number of Django tables.

| Record              | Required properties                                                                                                               |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Account             | Immutable ID, display name, master enable switch, subscription enable switch, explicit AI node                                    |
| Subscription        | Account, unique indexed token digest, enabled state. No management permission                                                     |
| Node                | Installation-lifetime UUID, unique immutable slug, display name, endpoints, protocol settings, lifecycle and publication evidence |
| Node authentication | One active token digest, node identity and audit timestamps                                                                       |
| NodeGroup           | Explicit node memberships. Accounts receive the union of their groups                                                             |
| Credential          | Immutable ID, account, node, protocol, recoverable secret and enabled/retired state                                               |
| Policy head         | Monotonic generation and serialized policy-mutation lock                                                                          |
| Node bundle         | Current exact bytes, generation, payload digest, runtime digest and credential roster                                             |
| Observation         | Latest acknowledged generation and roster, process identity, health and monotonic status sequence                                 |
| Revocation target   | Retired credential or node stop, removal generation, pending/confirmed/isolated evidence                                          |
| Traffic source      | Node, immutable source UUID, active/retired state and coverage information                                                        |
| Traffic snapshot    | Source, credential, calendar bucket, version, digest and absolute values                                                          |
| Audit record        | Actor, operation, affected IDs, expected generation and result, without secret payloads                                           |

A node UUID identifies one installation lifetime. Replacement creates a new node, not a parallel instance model.
A retired node remains in revocation and accounting history.
A copied disk cannot become a second active installation under the same identity.
Reuse of a slug or credential ID is forbidden.

Credentials are node-scoped from M5. A compromise exposes that node's credentials, not the same secrets on every node.
Keep a unique active credential per account/node/protocol. Rotation creates a new credential ID and retires the old one.
A disabled credential can be re-enabled unchanged only if it was not retired.
Account, protocol and grant disablement each create removal targets for all affected credentials.
Re-enable waits for those targets to resolve. It cannot silently cancel pending removal.
The agent consumes opaque roster IDs, not account permissions or a device enum.
Future device ownership must not require another node transport protocol.

Import original-node secrets and wire usernames unchanged. New nodes receive independent random secrets and stable opaque wire labels.
Map each label to its immutable credential and account IDs in the bundle.
Never attribute delayed traffic by a mutable username lookup.
Keep tombstones for removed accounts, credentials, nodes and imported historical names.

Snell PSKs and Hysteria2 obfuscation secrets remain node-scoped infrastructure secrets.
Store them under the existing protected PostgreSQL/backup boundary, not in normal Admin output.
TLS private keys remain local. Self-signed clients receive only the verified public certificate.
Root-owned TLS path references replace inline server keys in node bundles.

### Subscription boundary

Keep existing account subscription URLs through migration.
Compute their current tokens once and store indexed digests without printing them.
New and reset subscriptions use opaque random tokens with at least 256 bits of entropy.
Show a newly issued link once. A lost link requires explicit reissue.
A request can reproduce its own link on its authenticated page, but Admin cannot recover a stored token.

At the client-contract cutover, remove HMAC token derivation, password exchange and machine-token compatibility.
Remove the legacy password/admin-parity fields and rename portal access to subscription access without changing its value.
Retain account enablement, operator passwords, permissions, account subscriptions and imported token digests.
Link revocation still does not revoke downloaded proxy credentials.
Device enrollment and global user-credential invalidation remain a separate future decision.

## 3. Policy transactions and publication

Every policy writer uses one service layer, including Admin and SSH management.
Direct model edits must not bypass generation, rendering or revocation rules.

1. Lock the policy-head row inside a PostgreSQL transaction.
2. Check the caller's expected generation for conflicting edits.
3. Apply the account, grant, credential or node change.
4. Render affected node bundles from the same protected policy state.
5. Store exact bytes and increment the generation.
6. Record revocation targets and the redacted audit result in that transaction.
7. Commit before returning success.

Rendering performs no network request or host operation.
All policy writers acquire the same lock, so affected reads cannot race another policy mutation.
A render failure rolls back the mutation and returns a fixed error code.
The stop-node operation uses a minimal bundle and must not depend on a valid proxy config or TLS files.
This gives operators an emergency path when ordinary rendering fails.

Store only the current desired bundle, not a general revision-history service.
Audit records and revocation barriers retain the facts needed after supersession.
Nodes retain their accepted bundle locally. GET serves stored bytes, never an ad hoc render.
ETags identify generation and digest. Intermediate changes naturally coalesce between polls.
Do not delay policy commits with a batching timer.

Acceptance initially measures 25 nodes and 500 accounts with all four protocols.
A full affected-fleet mutation must complete within two seconds on the declared test host.
This is a capacity gate, not a measured fact or an unlimited scale promise.
If it fails, report the measurements and revise the design before adding a queue.

### Four distinct facts

| Fact            | Meaning                                                                        |
| --------------- | ------------------------------------------------------------------------------ |
| Desired         | The database instruction                                                       |
| Applied         | Authenticated evidence of durable local publication and matching service state |
| Published       | Endpoint and credential pair eligible for subscription rendering               |
| Observed health | Recent evidence, not permanent reachability                                    |

Publish a new endpoint only after host preparation, apply acknowledgement and real protocol checks.
Publish new credentials only after the node applies their generation.
Eligibility requires a serving roster that contains the exact credential ID, not just a sufficiently high generation.
Always filter subscriptions through current permissions, even while an older endpoint remains published.
Never return retired secrets from a cached published bundle.
A stale heartbeat alone does not withdraw a working published node.

Keep established endpoints immutable during ordinary deploy.
Use a replacement node for incompatible domain, port or TLS changes.
Publish the replacement, refresh clients, explicitly move AI assignments, then drain the old node.
No general dual-listener endpoint migration is included.

`draining` withdraws ordinary advertisements but keeps existing service and explicit AI pins.
`stopped` withdraws all advertisements and requests shutdown.
Neither a missing subscription entry nor an invalid agent token proves proxy revocation.

## 4. Operator deployment and authentication

Every `sbm` command requires `--config PATH` or `SBM_CONFIG`.
The operator config selects the control-plane SSH alias, origin and built release.
New-node input supplies initial settings. Repeat deploy reads PostgreSQL and never overwrites it from stale local input.

| Proposed command after `sbm --config PATH`                     | Effect                                          |
| -------------------------------------------------------------- | ----------------------------------------------- |
| `node list`, `node show NODE`                                  | Redacted desired/applied/published/health state |
| `node deploy --create SPEC --hostname SSH_ALIAS --release DIR` | Register and provision one node                 |
| `node deploy NODE --hostname SSH_ALIAS --release DIR`          | Reconcile one existing host                     |
| `node set NODE --input SPEC --expect-generation N`             | Explicit policy update                          |
| `node drain NODE`, `node disable NODE`, `node enable NODE`     | Lifecycle change with honest pending state      |
| `node rotate-token NODE --hostname SSH_ALIAS`                  | SSH-based API credential replacement            |
| `node revoke-status OPERATION`                                 | Per-node confirmation or pending exposure       |
| `node isolate NODE --reason TEXT --evidence FILE`              | Record externally verified isolation            |
| `node set-ai ACCOUNT --node NODE`                              | Explicit AI reassignment                        |

`--create` and positional `NODE` are mutually exclusive.
A read-only `--plan` can contact hosts, but creates no records, credentials or files there.
Do not equate Ansible check mode with proof of no side effects. Use reviewed read-only discovery tasks.
No command implicitly purchases a VPS, edits DNS or changes a provider firewall.

### Ansible boundary

The wrapper invokes pinned `ansible-core` and explicitly pinned collections from the operator's locked environment.
Use SSH connection plugins and modules for packages, users, files and systemd.
Keep a small custom apply command for the proxy-specific state transition in section 6.
Do not turn a playbook into a shell-script transport.

Preserve checked host keys, non-PTY transfers, bounded SSH control paths and the actual management port.
Use full managed templates where the file belongs to this project.
For shared firewall files, manage a marked block and reject conflicting ownership.
Preserve Hysteria2 IPv4/IPv6 NAT redirects, 16 MiB UDP buffers and existing reserved-destination protection.
Do not replace an entire operator-owned firewall configuration.

Use Certbot standalone on node-only hosts when port 80 is available.
Retain a webroot method for a colocated control-plane host.
Require DNS readiness before issuance. Keep RSA/ECDSA requirements and coordinate renewal through the node lock.
No node-only Nginx installation is required.

Secret-bearing tasks use `no_log`, disabled diffs and private temporary files.
Do not put secrets in command arguments, environment dumps, fact caches or callback output.
Do not forward operator proxy credentials to remote package commands.
Package downloads follow the operator's direct-download policy.
Pin dependencies and test role idempotency, failure recovery and log redaction in disposable hosts.

### Management without generic RPC receipts

Invoke fixed control-host management commands over SSH, with bounded typed input on stdin.
Reuse host SSH/sudo authorization and PostgreSQL peer access as `sbm`.
Capture output privately and emit only redacted summaries.
Do not add an operator HTTPS API, management token service or a generic RPC dispatcher.

Operations set desired state with stable resource IDs and expected generations.
After a lost reply, query actual state. Do not blindly repeat a non-idempotent operation.
For credential rotation, create its immutable ID and secret before the mutation in protected operation state.
An identical ID/digest is a no-op. A conflicting reuse fails.
Discard secret operation state only after confirmed completion. Do not maintain a permanent request-receipt database.

### SSH bootstrap and token rotation

1. The node generates its UUID and 256-bit API secret in a private identity file.
2. The operator reads only the ID and secret digest over SSH.
3. A control-host command registers that ID/digest and initial desired settings atomically.
4. The operator installs the verified role artifact and fixed HTTPS origin.
5. The agent authenticates and obtains its first bundle.
6. Deployment checks service and external protocol access before publication.

The node's identity file is the retry anchor after a lost response.
An identical registration succeeds. A conflicting identity or existing-node identity loss fails.
Replacement requires a new UUID and explicit old-node shutdown or external isolation.
Ordinary deploy never changes credentials, account links or accounting state.

Rotation stages a new node-local secret before replacing its registered digest over SSH.
After registration, activate that staged file and verify authentication.
A lost reply is reconciled by comparing digests. Keep the staged file until completion.
A temporary API authentication gap must not stop proxy service.
No overlapping API keys, public enrollment, expiry receipts or token-recovery endpoint is required.

## 5. Node HTTPS contract

Use Django Ninja schemas, explicit authentication and bounded request parsing.
Keep OpenAPI generation for fixtures and development. Disable public interactive API documentation.
Schemas do not replace authorization, byte limits, duplicate-key checks or transaction rules.
Generate pinned gRPC/protobuf stubs for the local engine APIs, not dynamic descriptors.

| Endpoint                | Contract                                                                             |
| ----------------------- | ------------------------------------------------------------------------------------ |
| `GET /node/v1/config`   | Stored bundle or `304`, with engine/agent compatibility negotiation                  |
| `POST /node/v1/status`  | Applied generation, runtime digest, process identity, persistent sequence and health |
| `POST /node/v1/traffic` | Bounded pages of absolute versioned snapshots. Reply after PostgreSQL commit         |
| `POST /node/v1/live`    | Current bounded connection snapshot, never historical usage                          |

Resolve node identity from the bearer credential, not a request field.
Reject cross-node IDs, unknown traffic sources and credentials that never belonged to that node.
Retired credentials remain valid accounting references but grant no new proxy access.
Keep stopped-node reporting credentials until pending traffic and revocation evidence are reconciled.

Verify HTTPS hostnames and reject redirects or unexpected origins.
Disable environment-proxy inheritance. Colocated nodes use the same verified HTTPS contract.
No CORS, remote shell, arbitrary file path, URL fetch or executable update action exists.

| Bound                           | Default                                                  |
| ------------------------------- | -------------------------------------------------------- |
| Config poll                     | 15 seconds, up to 20% jitter                             |
| Status                          | 30 seconds and after apply outcomes                      |
| Stale status                    | 90 seconds without valid evidence                        |
| Counter sample / traffic upload | 30 seconds / 60 seconds, plus final maintenance sampling |
| HTTP timeout                    | 10-second connect, 30-second total                       |
| Failure retry                   | Full-jitter exponential backoff, 2 seconds to 5 minutes  |
| Bundle / traffic page           | 8 MiB / 1 MiB                                            |
| Status / live snapshot          | 64 KiB / 256 KiB                                         |

Use UUIDs, RFC 3339 UTC timestamps, lowercase SHA-256 digests and bounded nonnegative integer counters.
Version every schema. Reject unsupported required versions before changing local state.
Errors contain fixed codes and retry guidance, never raw config or checker output.
`401` slows retries without stopping proxies. `409` requires reconciliation.
`413`, `429` and `503` signal bounds, rate limits and unavailable dependencies.
A dependency failure must never become an empty successful roster.

The bundle contains node ID, generation, desired service state, engine contract, config, accounting calendar and immutable roster mapping.
Store exact canonical JSON bytes. Its payload digest excludes the digest field itself.
The runtime digest covers config, roster and service state, but excludes generation-only metadata.
A higher generation with identical runtime content needs no restart when the running process matches that content.
Commit and report the newer generation without inventing process-replacement evidence.
Ignore delayed status sequences and reject conflicting digests.
Accept new apply evidence only for the current desired bundle or the last acknowledged bundle.
A late report for another superseded bundle requests reconciliation, without publishing access or confirming removal.

## 6. Forward-only application and recovery

The agent is the only writer of its managed config.
Root owns executable code, identity, configuration and state directories.
Use `0700` private directories and `0600` private files under any caller umask.
Keep binaries executable without broadening access to secrets.

| Path                                 | Purpose                                                |
| ------------------------------------ | ------------------------------------------------------ |
| `/etc/sbm-node/identity.json`        | Node ID, HTTPS origin and API credential               |
| `/var/lib/sbm-node/generations/`     | Immutable config, roster and bundle directories        |
| `/var/lib/sbm-node/current`          | Atomically replaced pointer to one complete generation |
| `/var/lib/sbm-node/traffic.sqlite3`  | Accounting state, not policy authority                 |
| `/var/lib/sbm-node/suspended`        | Persistent local emergency guard                       |
| `/usr/local/etc/sing-box/.sync.lock` | Shared OS lock for apply and maintenance               |

The service starts through a small fixed `sbm-node exec` path.
It acquires the shared startup lock, checks the pointer and suspension guard, and records the loaded digest with systemd invocation identity.
It then executes sing-box with the immutable generation path, not a mutable config path.
The lock must not remain held for the lifetime of the proxy.
The unit's pre-start validation must not contact the control plane.
Engine restarts therefore use local committed state during a control-plane outage.

### Active apply

1. Verify bundle identity, generation, hashes, installed engine features and permitted TLS paths.
2. Stage and fsync a complete immutable generation on the same filesystem as the pointer.
3. Run `sing-box check` against real local TLS material. Keep raw failures private.
4. Acquire the shared lock and recheck ownership, current generation and suspension.
5. Complete final accounting sampling and suspend collection across process replacement.
6. Stop the old engine and confirm its cgroup contains no remaining service process.
7. Atomically publish the new pointer and fsync its containing directory.
8. Release the lock before requesting systemd startup, whose wrapper acquires that lock itself.
9. Verify the new invocation, loaded digest, expected listeners and local APIs for five seconds.
10. Persist the monotonic status receipt and report the result.

One agent controller serializes apply requests. Maintenance must stop or pause that controller before taking ownership.
Recheck the current pointer and maintenance ownership before reporting success.
Never report success for another controller's generation.

Steps 1–5 leave the working service untouched on failure.
A failed stop prevents pointer replacement and a second start.
Ordinary maintenance aborts on failed final collection. Urgent revocation can proceed with an explicit accounting-gap record.

Pointer publication is durable policy selection, not proof that startup succeeded.
After publication, recovery uses only that generation or a newer one.
A runtime-invalid candidate can leave the node unavailable until repaired.
The operator repairs settings with a newer generation or restores a compatible binary against current policy.
Do not restore an older credential roster automatically or through a generic rollback command.

| Failure boundary                            | Recovery                                                           |
| ------------------------------------------- | ------------------------------------------------------------------ |
| Crash before pointer publication            | The old pointer remains authoritative. Revocation is still pending |
| Crash after durable publication             | Start or reconcile only the new pointer                            |
| Startup/readiness failure                   | Report failed. Retry the current generation within systemd limits  |
| Reply lost after application                | Resend evidence. Do not reapply or reduce generation               |
| Unknown pointer, digest or process identity | Stop automatic mutation and require repair                         |

No multi-phase apply journal, rollback floor or boot recovery service is needed for this policy.
Crash tests must prove that claim. Atomic rename alone does not establish service correctness.
On initial adoption, verify the live config and process before recording matching ownership without a restart.
Unknown adoption state requires a controlled restart, not a fabricated no-op receipt.

### Stop and emergency suspension

A stop bundle bypasses config/TLS validation after its identity and generation checks.
Confirm termination, publish a stopped generation and report stopped state.
The startup wrapper refuses a stopped pointer, even after reboot.
A later enable requires a higher generation and normal validation.

`sbm-node suspend` persists a local guard and stops the service under the same coordination.
The agent continues reporting but cannot restart the proxy while suspended.
`sbm-node resume` requires current control-plane policy and validation before clearing the guard.
A plain `systemctl stop` is not a persistent policy change.

### Revocation evidence and restore

A credential retirement records its node and removal generation.
Confirmation requires durable roster exclusion and proof that an older process no longer serves it.
A stopped node can confirm removal without being healthy or published.
A generation number alone is insufficient, including on a runtime-hash no-op.

Keep unreachable nodes pending indefinitely. Target healthy confirmation within 60 seconds of the policy commit.
Measure each planned interruption against a 60-second gate.
An isolation record requires operator verification and an external evidence reference.
A compromised agent can lie. Its report is an operational acknowledgement, not attestation.

Reject generation regression and equal-generation digest changes.
After a PostgreSQL restore, quarantine management and publication until policy and revocation barriers are reconciled with nodes and recovery records.
Do not merely increase a generation counter to legitimize restored old authorization.
If lost revocations cannot be reconstructed, isolate affected nodes and perform an explicitly authorized credential reset.
Local identity/config backups cannot silently replace a newer active installation.

## 7. Traffic accounting and synchronization

Keep the counter-enabled server build. The V2Ray counter poller remains authoritative for usage totals.
The connection stream supplies domain/destination attribution and live observations, not a second billing total.
Both collectors run inside `sbm-node`, with one transactional local database writer.
Loopback listeners and authentication restrictions remain unchanged.

Port behavior from `traffic_stats.py`, `connection_tracking.py`, `traffic_store.py` and `traffic_reconciliation.py` using recorded fixtures.
Preserve UPDATE deltas, CLOSED totals, opening replay frames, zero-byte checkpoints and day-boundary reconciliation.
Sharing a timer or a language does not remove those requirements.
Retain existing domain/IP views and privacy permissions.

### Local transactions

A local transaction commits observed counter checkpoints and their derived absolute buckets together.
Stream transactions likewise commit attribution and replay checkpoints together.
Use source ID, systemd invocation identity and connection ID to prevent collisions across nodes and engine restarts.
Never infer an engine restart only from a counter decrease or wall-clock time.

Each database has a random source UUID, registered through operator SSH before export.
A reboot retains it. A new installation or lost database gets an explicitly registered new source.
A restored old database is not a valid continuation of an active source.
Block export until recovery reconciles it or starts a fresh baseline under a new source.
Do not replay imported totals under a new identity.

### Absolute snapshots, not delta transport

Key each snapshot by source, immutable credential, accounting date and snapshot kind.
Use separate kinds for authoritative totals and derived attribution.
Each key has a monotonically increasing version and a digest of its complete payload.
Snapshot generation occurs in the same local transaction as its source-state change.
The current rows themselves are pending export state. No per-event transport outbox is required.

In one PostgreSQL transaction, authenticate ownership, lock the snapshot key and apply these rules:

| Incoming version            | Effect                                           |
| --------------------------- | ------------------------------------------------ |
| Greater than stored         | Replace the complete snapshot atomically         |
| Equal with identical digest | Return success without another write             |
| Equal with different digest | Reject conflict and require reconciliation       |
| Older                       | Return the current version without changing data |

Return accepted key/version/digest receipts only after commit.
The node marks a version acknowledged only if its current local version and digest still match that receipt.
A newer local update remains pending. Batch order and duplicate requests cannot add bytes twice.
No contiguous transport sequence or unbounded receipt table is required.

Authoritative totals cannot decrease within a source bucket.
Derived attribution can shrink or change names after reconciliation or top-N compaction.
Replace that entire versioned bucket, including removals. Never merge its values with component-wise maxima.
Bound each complete bucket before collection starts so it fits the request limit.
Use top-N plus reserved aggregate keys for bounded export. Retain the established completed-day compaction semantics.
Default to 50 named domains and 50 named destinations per completed credential/day bucket.
Limit current-day detailed entries to 10,000 per credential across both dimensions.
Continue existing tracked entries, but put excess new identities into aggregate keys and report degraded detail.
If current-day detail reaches that cap, do not claim exact top-N results.

Periodically resend retained snapshots to repair lost acknowledgements.
Reconnect sweeps all dirty keys. A slower anti-entropy sweep checks clean retained keys too.
Keep at least 35 days of acknowledged local buckets for repair and reconciliation.
Older buckets can expire only after their final version is acknowledged and no pending collector state can change them.
Keep unresolved boundary/checkpoint dependencies even beyond that window.
Central history follows the established retention rules and does not expire with the local copy.
Do not silently discard unacknowledged usage.

Default accounting storage budget is 256 MiB, excluding separately budgeted config generations.
At 90% capacity, stop adding new domain detail and report degraded attribution.
Reserve remaining capacity for totals and checkpoint updates.
At the hard bound or filesystem failure, stop accounting commits and report a coverage gap. Keep proxy service running.
Expose oldest pending data, last successful sample/upload and storage pressure.
This is bounded best-effort accounting, not an indefinitely lossless financial ledger.

### Live state and history

Live connections are bounded replacement snapshots, not durable usage events.
Key them by source, engine invocation and connection ID.
Accept only the current engine/status identity with a newer live sequence.
Expire views after 90 seconds. Do not replay queued live snapshots after reconnect.
No separate cache service is required.

Central queries sum authoritative snapshots across nodes and sources.
Keep original account identities, timezone, cycle boundaries and historical provenance.
Freeze the account accounting calendar at migration. Provider reset calendars remain independent per node.
Preserve monthly-only history where daily reconstruction is impossible.
Do not sum an imported monthly baseline and its already represented daily rows twice.

Import existing daily/monthly totals, domain/IP snapshots and retained reconciliation results into PostgreSQL.
Stop old writers, flush and record a cutoff before import.
Seed PostgreSQL and local SQLite under the same registered original-node source ID, with matching snapshot versions and digests.
Carry final engine counter checkpoints, stream replay checkpoints and unresolved reconciliation state into that local database.
Compare every retained bucket before enabling its writer. Subsequent exports replace those snapshots rather than add a second historical baseline.
If the engine stays running, the first new sample starts from the transferred checkpoint, not zero or a fresh discard baseline.
An idempotent import uses a source-file digest and explicit original-node mapping.
Keep the private SQLite archive and a documented offline reader after parity passes.
Do not erase old data as part of rollout.

Unpolled bytes can be lost when the engine dies. Stream replay can omit older closed connections.
Preserve explicit coverage warnings and attribution uncertainty around midnight and outages.
Do not turn missing data into zero usage or exact domain totals.
A future official-binary switch needs a separate proof of accounting parity, not a happy-path comparison.

## 8. Client routing and contract

Keep one profile and the five existing route modes.
Use stable node/credential-derived leaf tags, not display names.
The ordinary selector offers every authorized published node/protocol pair.
Its automatic pool tests one operator-selected preferred transport per node.
Other transports remain available manually. Automatic selection does not promise transport or throughput optimization.

```text
ordinary rules -> proxy [auto, n.NODE.PROTOCOL, ...]
                   auto -> urltest [preferred leaf for each eligible node]
AI rules ------> ai-proxy [leaves of exactly one pinned node]
AI DNS --------> resolver with detour ai-proxy
ordinary DNS --> resolver with detour proxy
```

Use a non-AI probe, initially `https://www.gstatic.com/generate_204`, every five minutes with 100 ms tolerance.
Bound startup and concurrent probes. No direct retry can turn a failed proxy probe into success.
Automatic changes affect new connections without closing healthy sessions.
Manual ordinary switches must not interrupt AI streams.
Prove actual sing-box selector interruption behavior with the pinned core.

The AI pin must be authorized and explicitly assigned.
Initially assign the original node. Grant removal must explicitly reassign it or accept blocked AI access.
Never choose a faster or healthier AI node automatically.
Missing or unusable pins reject proxied AI traffic and DNS, with no ordinary or direct fallback.
An alternative protocol on the same pinned node is permitted.

A node identity does not guarantee a stable public IP.
Mark AI eligibility only after verifying a static egress address and family.
Initially constrain AI-eligible node egress to its verified IPv4 address.
This also constrains ordinary traffic through that node. Providers can still reassign addresses.
Do not claim location or provider eligibility from an IP check.

Preserve direct exceptions, security rules and community AI precedence.
Explicit `direct` mode continues to send permitted AI traffic and DNS directly.
This is a user-selected privacy trade-off, not failure recovery.
Use separate AI and ordinary DNS detours and bootstrap endpoint resolution without recursive proxy dependence.
Do not broaden a direct exception to a provider's parent domain.

Linux TUN retains IPv4-only DNS, public IPv6 rejection, UDP limits, exclusive capture and API ownership checks.
Automatic TUN maintenance remains check-only. New endpoints require console refresh.
An existing ordinary pool can fail over without refresh, but cannot discover an unknown replacement endpoint.
Mobile retains platform DNS and capture behavior with no HTTP controller listener.

### Version 2 desktop interface

| Command                      | Target behavior                                                        |
| ---------------------------- | ---------------------------------------------------------------------- |
| `sbc exit`                   | Show requested ordinary selection and observed leaf                    |
| `sbc exit auto`              | Select the automatic ordinary pool                                     |
| `sbc exit NODE --protocol P` | Select one authorized pair without restart                             |
| `sbc exit --list`            | Offline list of installed choices                                      |
| `sbc ai protocol [P]`        | Show/select protocol on the fixed AI node                              |
| `sbc status`                 | Separate ordinary selection, AI pin, observed paths and stale metadata |
| `sbc route`                  | Existing modes and aliases                                             |

This replaces protocol-only selection in the new contract. No permanent alias hides the node choice.
Preserve operation locks, Cobra/pflag, translated errors, terminal sanitization and offline help/completion.
Verify API readback and report uncertain results as failure.
A TUN API failure never falls back to mixed mode.

The v2 envelope contains mixed/TUN configs, node UUIDs/slugs/display names, leaf mappings, selector names, AI pin and region reference.
Reject unsupported metadata or oversized responses without replacing the working installation.
Preserve valid selections on refresh. A removed ordinary leaf returns to auto with a visible notice.
An unavailable AI transport can select another transport on the same pin only.
Region/language matching uses the explicit AI node and warns that ordinary traffic can exit elsewhere.
Node selection never changes OS/browser settings implicitly.
Diagnostics report the tested connection's exit, not one supposed IP for all routes.

### Compatibility boundary

Release a transition `sbc` that can read v1 and v2 before server cutover.
Its signed binary upgrade must work while the old server still serves v1.
Define and test mapping from the old protocol choice to the original-node leaf.
After authorized cutover, the server serves only v2 desktop envelopes and gives old clients an explicit upgrade-required response.
Retain signed upgrade/download access for valid subscription tokens.
Do not serve a hidden single-node compatibility projection or capability-dependent routing graph.
Remove the transition v1 reader after the announced upgrade window.

The contract cutover does not rotate existing subscription tokens or original-node proxy credentials.
Old cached profiles can continue until their credentials are separately revoked.
Do not confuse client-version refusal with user-access revocation.
Installer password/machine-token exchange ends at this boundary. Users then supply their existing subscription link.
Keep service-conflict, legacy TUN and ownership checks in installers.

Official mobile apps continue to import one remote profile at the same URL.
The new graph requires recorded supported-app acceptance before rollout.
Record OS, app and embedded core versions separately. No nested-selector support is assumed from JSON validation alone.

## 9. Release and maintenance

Build role-specific artifacts locally with the existing release signing system.
The proposed `sbm --config PATH release --role node` builds the node artifacts.
The node role contains `sbm-node`, the counter-enabled sing-box build, service definitions and public trust material.
It contains no account roster, project Python environment or operator secrets.
The control role contains the application and client downloads. A colocated host installs both roles independently.
`sbm release` continues to use the working-tree configuration as supplied, without git resets.

Verify signatures and hashes before installing node binaries.
Keep signing keys only on the operator machine.
Protect the installed verification key through SSH provisioning, not a key supplied by the candidate archive.
No agent self-upgrade or arbitrary executable URL is included.

Maintenance pauses the agent controller and coordinates through the shared lock.
Validate candidate binaries against the current desired config before stopping service.
Replace immutable executable paths, then start and verify the current policy.
A binary rollback is permitted only if it supports that same current policy and local database schema.
Never roll back the accounting database or credential roster with the executable.
Stop with explicit repair instructions when downgrade compatibility is unknown.
Deploy one node at a time by default.

Certificate renewal uses the same coordination and final accounting sample.
Retain automatic renewals but not an uncoordinated `reload-or-restart` hook.
Back up PostgreSQL, node identities, current bundles, TLS material and retained releases.
Local accounting backups are recovery evidence, not automatically resumable sources.
Test restoration in quarantine before publication or policy writes.

## 10. Cut list and implementation sequence

| Area                                               | Retain                                                                        | Replace or remove after acceptance                                                      |
| -------------------------------------------------- | ----------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `control_plane/`                                   | Django permissions, account IDs, private Admin, portal/download authorization | Local root DB sync, inventory authority transitions, generic legacy authentication      |
| `control_plane/services.py`                        | Authorization and rendering requirements                                      | Inventory-shaped projection, token scan, trigger-file sync and privilege-switching code |
| `deploy.py`, `provision.py`, `ssh.py`              | Security requirements and native fixtures                                     | Remote orchestration with Ansible roles. Keep only a narrow SSH management adapter      |
| `release/`                                         | Signing, allowlists, hashes, rules and protocol builders                      | Monolithic node artifacts and packaged runtime rosters                                  |
| `traffic_*.py`, `connection_*.py`                  | Accounting fixtures, calendars, replay and reconciliation semantics           | Live Python collectors and central SQLite after Go/PostgreSQL parity                    |
| `desktop_config.py`                                | Route/DNS/security policy                                                     | Single-node assumptions and per-protocol selector tree                                  |
| `internal/install`, `internal/api`, `internal/tun` | Signed upgrades, ownership, locks and recovery                                | v1 assumptions, protocol-only selection and incomplete selector restoration             |
| Hosted installers                                  | Safe installation, service/TUN guards, POSIX sh and PowerShell 5.1            | Password and machine-token exchange after the explicit client boundary                  |

No production command is authorized by this sequence.
Disable replaced deployed services before optional removal. Keep old state and recovery artifacts private.

| Slice | Deliverable                                                                                        | Gate                                                                               |
| ----- | -------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| M5.1  | Typed node model, node-scoped credentials, subscription digest migration and transactional bundles | PostgreSQL concurrency, import parity, schema fixtures and render capacity         |
| M5.2  | Ninja node API, SSH bootstrap and Ansible node role                                                | Authentication isolation, lost replies, repeat deploy, TLS/firewall safety         |
| M5.3  | Forward-only Go apply and recovery                                                                 | Disposable systemd host, crash/reboot boundaries, no-op adoption and revocation    |
| M5.4  | Integrated collectors, local SQLite, PostgreSQL traffic and one-time import                        | Differential accounting fixtures, offline/retry/restore tests and history parity   |
| M5.5  | Original-node handover and retired-writer removal                                                  | One writer, unchanged original credentials/links, measured interruption            |
| M6.1  | Groups, publication lifecycle and second-node deployment                                           | Two disposable nodes, node-scoped secrets and honest offline revocation            |
| M6.2  | v2 renderer, transition client, fixed AI routing and mobile profile                                | Core routing/DNS, native desktop lifecycles, isolated TUN and mobile evidence      |
| M6.3  | Explicit contract cutover plan and compatibility deletion                                          | Upgrade path, refusal behavior, unchanged account URLs and no hidden v1 projection |
| M6.4  | Combined failure/replacement/restore acceptance and operator guides                                | All gates below with recorded residual limits                                      |

Dependency spikes precede implementation: validate Ninja/Pydantic integration, pinned Ansible roles and cgo-free SQLite/gRPC builds.
Choose maintained compatible versions and lock them. Do not turn an unverified package-version claim into a requirement.

Adoption freezes old writers under their shared lock, imports state and verifies the live process.
Hand ownership to the agent only after parity. Disable old units and reject the legacy sync command on managed nodes.
Retain migration fixtures, not two maintained writers.
Recovery to old tooling requires a current authorized projection and an explicit operator procedure.

M5 completes with one agent-managed node and unified central accounting.
M6 completes with two nodes, v2 clients, fixed AI egress and the required acceptance evidence.
Device management is reviewed only after M6. It is not a prerequisite for retiring obsolete client/authentication code.

## 11. Acceptance and evidence

### Gate sequence and operator involvement

G0 through G3 proceed without per-gate approval. Report a concrete blocker when a required check cannot pass.
Stop before public-network acceptance or production changes. Both require explicit target-specific authorization.

| Gate                         | Required evidence                                                                                                            |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| G0: minimum working system   | Ansible/SSH installation and registration, authenticated config pull, real proxy traffic, local SQLite and PostgreSQL upload |
| G1: single-node reliability  | Real systemd/engine failures, invalid config rejection, outage autonomy, crash/reboot recovery and forward repair            |
| G2: accounting and migration | Differential collector checks, historical parity, replay/idempotency, source recovery and honest coverage gaps               |
| G3: two nodes and clients    | Ordinary selection, fixed AI routing/DNS, client upgrades, native desktop checks and required iOS evidence                   |
| G4: pre-release review       | Gate reports, remaining limits, migration/repair procedure and an authorized public-network acceptance plan                  |

G0 uses one synthetic account and one protocol before broader protocol and migration tests.
Run Linux service tests in isolated disposable VMs with real PostgreSQL, systemd, SSH, TLS and sing-box.
Use GitHub-hosted native desktop jobs and the isolated Linux TUN VM for platform evidence.
Do not contact or change production nodes during these gates.
Local VMs do not establish public reachability, public ACME issuance or stable public exit addresses.

Before G4, run one two-hour isolated stability test with traffic, polling, upload, disconnection and recovery.
Check accounting consistency, backlog convergence, unexpected restarts, memory and resource growth.
Use controlled clocks and synthetic state for midnight, DST and long-backlog tests.
A two-hour run does not establish long-term production reliability.

Require recorded iOS device acceptance for the first release. Android acceptance does not block it.
Mark Android unverified until tested. Never infer device acceptance from core fixtures.

The failed-startup scenario verifies forward repair: correct the config, publish a newer generation and restore proxy service.
It does not require a simultaneous credential-revocation scenario.
This test boundary does not remove authentication, access control or existing revocation requirements.

Each gate report records the commit, environment, commands, expected/observed results and remaining limits.
Skipped, unavailable and untested checks are not passes.

### Behavior matrix

| Area             | Required checks                                                                                                               |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Policy           | Concurrent Admin/SSH edits, failed render rollback, equal-generation conflicts and capacity measurements                      |
| Authentication   | Cross-node denial, hashed API secrets, no management/subscription rights and private Admin                                    |
| Bootstrap        | Lost register reply, identity loss, conflicting host, repeated deploy and interrupted token rotation                          |
| Apply            | Invalid config/TLS, failed stop/start, full disk, private umask and matching-process no-op                                    |
| Crash recovery   | Death/reboot before and after pointer publication, startup-wrapper races, lost status replies and stop guard                  |
| Revocation       | Retired credentials rejected and existing sessions terminated. Offline nodes stay pending. Stop ignores broken TLS            |
| Outages          | Proxy works during control-plane/DB outage and API authentication failure                                                     |
| Publication      | No unprepared endpoint or unapplied credential appears. Removed access never returns from an old snapshot                     |
| Accounting       | Four protocols and two accounts, replay, counter reset, duplicates/reordering, source restore, disk pressure and midnight/DST |
| History          | Exact retained totals, domain/IP buckets, calendars and tombstones. No imported-baseline double counting                      |
| Routing          | Ordinary auto/manual selection, fixed AI IP and DNS, pin failure, direct mode and no AI interruption from ordinary switches   |
| Client lifecycle | Signed v1-to-v2 upgrade, selection mapping, invalid metadata, Linux/macOS/Windows services and TUN console maintenance        |
| Mobile           | Recorded official iOS import, selection, refresh, persistence and capture evidence. Android remains unverified until tested   |
| Deployment       | Host-key checks, real SSH port, dual-stack NAT, certificate renewal and no leaked secret output                               |
| Recovery         | Quarantined PostgreSQL restore, lost revocation barriers, stale local DB and binary downgrade refusal                         |

Preserve empty Naive/Snell inbound omission and server private/reserved-address rejection.
Do not add a test credential that keeps an otherwise empty inbound open.
Validate all rendered variants with the intended engine and synthetic TLS material.
A listener check does not replace real authenticated proxy traffic.

Use fixtures and disposable hosts only for development.
Public PR jobs stay on GitHub-hosted runners. Native Linux TUN runs inside its isolated VM.
Production acceptance requires separate target-specific authorization and backups.
Unavailable native evidence remains a limitation, never a pass.

Official component references for implementation review:
[Django Ninja](https://django-ninja.dev/),
[Ansible](https://docs.ansible.com/ansible/latest/),
[modernc SQLite](https://pkg.go.dev/modernc.org/sqlite),
[gRPC Go](https://grpc.io/docs/languages/go/).
These identify selected components, not verified versions, benchmarks or completed integration.

Update current-behavior guides when implementation lands.
Add source mappings for new code paths when those paths exist.
Keep [AGENTS.md](../AGENTS.md), `docstale.toml` and this specification aligned.
