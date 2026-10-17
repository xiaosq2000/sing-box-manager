# Supervised account copy

Checked on 2026-10-17.

**Approved non-serving account copy committed and verified.**
The implementation passed independent source review and synthetic PostgreSQL tests.
The exact-report-approved copy passed committed-receipt and database-loaded parity
checks. This command does not serve subscriptions, issue links, configure sing-box,
or change Django operators. Inventory remains authoritative for live access.
One source-bound admission remains enabled. Further copying needs exact-report approval.

## Scope

Use the [first-copy plan](CONTROL_PLANE_CAPTURE.md). Preserve usernames, protocol
credentials, portal verifiers, membership, and enabled/admin flags, including disabled
records. Copied portal admin flags grant no Django permissions.

The required policy is `fresh-links-at-cutover`. Export and import create no
`LegacyAccess` rows and do not read the counter database. Unavailable legacy history
stays unavailable, not verified zero. The separate
[managed-access implementation](CONTROL_PLANE_LIVE.md) implements fresh links and durable
revocation. Its isolated code and empty schema are staged, without serving or ownership.
Before serving, deliver new URLs and reject old links.

The [fixture importer](CONTROL_PLANE_IMPORT.md) retains its separate command, format,
and counter/token tests. Never use its marker or synthetic assertions for real data.
The witness/journal capture protocol is deferred and is not part of this implementation.

## Isolated installation

The copy tools use a versioned directory outside the active application tree.
They reuse the existing Python environment. No service points at that directory.
Copy support uses `control_plane.0002_fixture_imports` and `account_copy.0001_initial`.
Separate managed-access staging adds `managed_access.0001_initial`, with both tables empty.
The fixture marker is false. One source-bound admission is enabled and not closed.
The account copy and its receipt are committed. Source freshness, exact input bytes,
report seal, and destination binding passed before apply. Read-only post-commit checks
verified the receipt and database-loaded fields, client configs/ETags, and server rendering.
These checks establish snapshot parity, not loaded-process equality or live serving.

Django created the models' normal content-type and permission definitions. Existing
permission assignments, operator records, and sessions remain unchanged. Source files,
service identities, and the running Django readiness check passed after copying.
An encrypted post-copy backup is verified off-host. An isolated PostgreSQL 17 restore
matched all rows, stable IDs, logical schema, sequence values, and the copy receipt.
Restored accounts passed field/render checks. Native PostgreSQL 16 restoration and
full-host recovery remain untested.
Keep exact paths, dependency checks, and recovery receipts in private operator records.
Recheck compatibility if an ordinary deployment replaces the shared Python environment.

## Admission and schema

`account_copy.0001_initial` depends on `control_plane.0002_fixture_imports`, not the
prototype `0003_capture_records`. It creates one admission table, no records, and
no admin page. Admission defaults to disabled. Tests verify migration and copying
without applying the prototype migration.

`AccountCopyAdmission` binds the existing target to one namespace and source UUID.
`authorize` requires `fixture_enabled=false` and an empty account-import target.
Django operator accounts may already exist. Exact repeated authorization is a no-op.
A different binding rejects. Neither export nor preview authorizes an import.
The fixture service rejects any database with an account-copy admission, even if
someone later enables the fixture marker.

Reuse `ImportSource`, `ProxySubject`, `ImportedAccess`, and `ImportRun`. Apply locks
both source identities, then the target and admission. It compares the full expected
projection with database-loaded rows, including retained absent rows and stable IDs.
It checks fields, desktop envelopes/ETags, and server rendering. It derives no tokens.

`close` permanently disables this command's admission. It retains copied records,
reports, and receipts. No supported command reopens it. Close admission before later
ownership handover. This is not itself a serving cutover. Restore procedures must
recheck closure before returning a database to service.

## Commands

Every action requires explicit `--config` or `SBM_CONFIG`. These are command shapes,
not authorization for live use. Supply only synthetic paths during development.
Quote paths that contain spaces. All operations are local: no SSH, automatic migration,
service restart, or source repair.

| Action      | Required flags in addition to `--action`                                         | Effect                                                                                            |
| ----------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `export`    | `--snapshot --auth-file --proxy-config --namespace --source-identity --sequence` | Read selected stored source files and publish a private account snapshot. No database connection. |
| `authorize` | `--namespace --source-identity`                                                  | Explicitly enable the bound account-copy admission in the selected destination.                   |
| `preview`   | `--snapshot --report`                                                            | Read-only database transaction. Publish a sealed report, not copied records.                      |
| `apply`     | `--snapshot --approved-report`                                                   | Check that exact report and transactionally copy accounts plus a receipt.                         |
| `close`     | `--namespace --source-identity`                                                  | Permanently stop further previews and applications, including receipt-confirmation calls.         |

Reject unrelated or missing flags before reading configuration. Help opens no config
or database. UUID arguments must be canonical. Sequence is an integer from 1 through
`2^63-1`. The operator assigns increasing sequences and retains their association
with exact snapshots. Do not change a source UUID or relabel an old snapshot to evade
ordering checks. No automated witness allocates or proves these numbers.

```sh
pixi run sbm --config SOURCE_CONFIG account-copy --action export \
  --snapshot SNAPSHOT --auth-file AUTH_FILE --proxy-config PROXY_CONFIG \
  --namespace NAMESPACE_UUID --source-identity SOURCE_UUID --sequence 1

pixi run sbm --config TARGET_CONFIG account-copy --action authorize \
  --namespace NAMESPACE_UUID --source-identity SOURCE_UUID

pixi run sbm --config TARGET_CONFIG account-copy --action preview \
  --snapshot SNAPSHOT --report REPORT

pixi run sbm --config TARGET_CONFIG account-copy --action apply \
  --snapshot SNAPSHOT --approved-report REPORT
```

Authorize only the intended destination. Review the exact successful report before
apply. Authorization enables copying for this source, not automatic approval of
future reports. New input bytes or changed destination state require a new preview.

For the later, separately approved end of account copying:

```sh
pixi run sbm --config TARGET_CONFIG account-copy --action close \
  --namespace NAMESPACE_UUID --source-identity SOURCE_UUID
```

## Source and private files

Export requires a complete plaintext runtime inventory. It rejects SOPS paths before
decryption. It checks source permissions, file type, and size before the first config
read. YAML duplicate keys, aliases, and merge keys are rejected without merge expansion.
It checks the full stored auth snapshot against runtime portal records. The proxy
comparison preserves JSON types, so numbers cannot stand in for booleans. It rereads
source bytes before publication and rejects changes. Pause operator configuration
edits during export.
Self-signed TLS is unsupported. No TLS material, reset database, traffic database,
or running-process state is inspected.

Source files must be regular, single-link, root/current-user-owned files with mode
`0600` or `0640`. Ancestors must be root/current-user-owned and not group/world-writable,
except root-owned sticky temporary directories. Reject symlinks. The operator must
select the actual source paths. Matching arbitrary files does not prove deployed or
loaded-state equality.

Snapshots and reports require current-user-owned `0600` files in a current-user-owned
`0700` directory. Inputs and outputs are bounded to 8 MiB. Publication is durable and
never overwrites existing files. A failed sync can leave an uncertain output: retain
it for inspection rather than overwrite it. Source export may need root to read
proxy configuration. A separately approved handoff must give the database operator
its own protected copy. The command does not change ownership or permissions.

The snapshot is canonical JSON with one final newline. Its purpose is
`production-account-copy`. It contains exact account records, source UUIDs/sequence,
a capture timestamp, source-file SHA-256 digests, and a keyed rendering-context
identity. Legacy counters are `not-imported`; identity history is `not-established`.
It excludes subscription/session/Django signing secrets and full runtime inventory.
Its protocol credentials and portal verifiers are still sensitive. Verifier validation
checks the Argon2id encoding without computing a password hash. It preserves the
original string. This check does not prove a password match or approve its login cost.

Source and destination must use the same approved rendering context and Django
report-sealing key for this single-host path. Destination access rosters are ignored.
Snapshot custody and selection are operator responsibilities. Digests and timestamps
are not independent provenance, loaded-state evidence, or rollback-proof freshness.

## Reports, retries, and failures

Reports use a distinct HMAC domain from fixture reports. They bind the fresh-link
policy, admission, namespace/source, snapshot bytes, rendering context, actual database
identity, target UUID, expected revision, and complete imported-state identity.
They contain counts and check results, not account records or usable credentials.

Apply rejects stale captures, changed previews, retired-name reappearance, and any
legacy-token state or unrelated import source in the target. Known renames and
reassignments need a separate contract. Current usernames alone do not prove person
continuity. A historical exact receipt confirms its operation without replaying
superseded access. A newly approved no-op also stores a durable receipt. After
admission closure, all copy calls reject rather than bypass the gate for a retry.

Exit `0` means success. Exit `2` means rejected arguments, input, state, or admission.
Exit `1` means an operational failure. Output uses counts and fixed error codes.
For an uncertain apply, retry the exact snapshot/report while admission remains open.
Do not repeat export, rotate keys, restore the database, or activate links as recovery.

## Retire temporary copy tools

Close admission before ownership handover. Retain the tools and receipts through an
agreed recovery period after full live migration, not just after the account copy.

After that period, remove unused copy commands, copy-only helpers, and their dedicated
tests. Replace the first-copy procedure with a short recovery guide. Keep live account
records, stable IDs, shared validation, and tests for retained behavior.

Do not delete tables merely because their names contain `Import`. These tables hold
account data. Managed access also uses the source/target bindings, closed admission,
and shared context validation. Retain these records and helpers until a reviewed
schema change removes their serving and recovery dependencies. Do not simply delete Django migration files.
Cleanup needs separate approval.

## Verification

```sh
SBM_REQUIRE_POSTGRES=1 pixi run -e control-plane-test test \
  tests/test_control_plane_account_copy.py \
  tests/test_control_plane_account_copy_review.py \
  tests/test_control_plane_account_copy_integration.py \
  tests/test_control_plane_import.py \
  tests/test_control_plane_import_integration.py
```

The checks use synthetic credentials and temporary databases. They cover complete
exports, strict shapes, private files, no token/counter use, separate gates, independent
migrations, read-only previews, exact retries, no-op receipts, stale/racing applications,
post-copy corruption, rollback, closure, and unchanged fixture-import behavior.
Review regressions cover first-read safety, YAML expansion, type-preserving proxy
comparison, and verifier validation without password computation.
These checks require PostgreSQL and sing-box, not the deferred prototype.
The account-copy source review found no remaining blocking issues in this non-serving
slice. It did not cover the managed-access candidate, execute tests, or inspect live state.
Deployment, real-data validation, fresh links, serving, and ownership remain separate.
