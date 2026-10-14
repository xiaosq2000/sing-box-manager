# Fixture-only access importer

Checked on 2026-10-14.

The importer makes a checkable PostgreSQL copy of synthetic access records.
It does not serve subscriptions, configure sing-box, or manage devices.
Inventory and the existing portal remain authoritative for live access.

**Production import is unsupported.** No live capture or export command exists.
A snapshot's fixture assertions do not prove a real server's state or freshness.

## Scope and fixture gate

Migration `0002_fixture_imports` creates separate import tables and one target
identity. Its `fixture_enabled` marker defaults to `false`. Both import actions
reject a target whose marker is false. There is no CLI option to enable it.
Tests enable it only in their disposable PostgreSQL databases.
Do not enable this marker in an operational database.

Imported proxy subjects are not Django operators. Copied portal admin flags do
not grant Django permissions. Import records have no admin pages or public routes.
Legacy access retains token usernames and counters. It creates no enrolled devices
and consumes no device slots. Protocol credentials remain usable plaintext in
database fields. Portal passwords remain one-way verifiers.
Protect database files and dumps as described in
[secret handling](SECRET_HANDLING.md).

## Commands

These operations require a migrated, explicitly enabled fixture database and a
synthetic snapshot. They do not run migrations, capture data, contact another host,
or restart services. `CONFIG`, `SNAPSHOT`, and `REPORT` are private path placeholders.
Quote paths that contain spaces. Do not substitute production data.

```sh
pixi run sbm --config CONFIG control-plane-import \
  --action preview --snapshot SNAPSHOT --report REPORT

pixi run sbm --config CONFIG control-plane-import \
  --action apply --snapshot SNAPSHOT --approved-report REPORT
```

Preview writes no database records. It projects the proposed copy in memory,
checks fields/configs/tokens, and writes a sealed, redacted report.
Apply verifies the report and inputs, then compares the complete database-loaded
copy with the expected change before committing. This includes absent records,
retained payloads, and existing subject IDs. Only genuinely new subjects receive
new IDs.

Every successful apply stores a report-bound receipt, including a no-op.
A no-op leaves the imported revision unchanged. Exact retries confirm that receipt
even after a newer import. They do not replay an older snapshot over newer state.

| Input               | Rule                                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------------- |
| Config              | Select with `--config` or exported `SBM_CONFIG`; supplies local database and protected rendering inputs |
| Snapshot            | Required for both actions; supplies all access records, never the config's user lists                   |
| `--report`          | Required only for preview; new output file, never overwritten                                           |
| `--approved-report` | Required only for apply; successful reviewed report that matches these exact inputs                     |

There is no default action or force option. Help needs no config.
Reports bind the bundle bytes, source identities and sequence, rendering inputs,
destination database identity, revision, and a digest of the imported database state.
Changed input bytes, altered reports, another database, or changed imported state
require another preview. Operator sessions do not invalidate a preview.

Snapshot/report files must be regular files, owned by the calling account, with
mode `0600` and one hard link. Their immediate parent directory must have mode
`0700` and the same owner. File and immediate-parent symlinks are rejected.
Each input is limited to 8 MiB.
Standard output contains counts only. Errors omit secret-bearing details.

| Exit | Meaning                                                                              |
| ---- | ------------------------------------------------------------------------------------ |
| `0`  | Successful preview, committed apply, or confirmed receipt/no-op                      |
| `2`  | Invalid arguments, rejected snapshot/report, stale state, or binding conflict        |
| `1`  | Operational failure; retry the exact apply inputs if the commit outcome is uncertain |

## Synthetic snapshot format

Tests construct private JSON snapshots. There is no production snapshot generator.
Unknown fields, duplicate JSON keys, non-finite JSON values, duplicate memberships,
and malformed counter rows are rejected. Access strings must be representable in
PostgreSQL UTF-8. NUL and unpaired Unicode surrogates are rejected before a ready
report can be published. Other valid Unicode remains unchanged.

| Field                                 | Contents                                                                                                        |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `version`, `purpose`                  | `1`, `synthetic-fixture`                                                                                        |
| `namespace`, `source_identity`        | Stable UUIDs; changing file or host labels must not bypass identity history                                     |
| `sequence`, `captured_at`             | Positive source capture sequence and timezone-aware cutoff                                                      |
| `evidence`                            | Explicit synthetic runtime/auth/proxy match, ordering, and username-continuity assertions; no identity mappings |
| `context_identity`, `secret_identity` | Keyed digests computed from the selected fixture infrastructure                                                 |
| `counter_state`, `counters`           | Complete table rows, or explicit verified initial-zero fixture evidence                                         |
| `access`                              | Complete portal, Trojan, Hysteria2, and Naive records, including disabled records and flags                     |

Absent memberships remain inactive. IDs and counter history remain.
Counters cannot decrease, including counters for names absent from inventory.
Declared renames/reassignments and known retired-name reappearance are rejected.
The prototype cannot detect an undeclared person change from a username.

The renderer comparison uses deployment fields, shared Hysteria2 settings, version
pins, and defaults from the config. It ignores that config's access rosters.
Self-signed TLS is rejected in this fixture implementation to avoid TLS-file reads.
The importer does not inspect release files or prove a deployed manifest's identity.
A separate synthetic HTTP harness checks signed downloads, path/authentication
rejection, version negotiation, ETags, and the existing access-case distinctions.

## Verification and remaining work

```sh
SBM_REQUIRE_POSTGRES=1 pixi run -e control-plane-test test \
  tests/test_control_plane_import.py tests/test_control_plane_import_integration.py
```

Tests use a temporary cluster, private sockets, and synthetic configuration.
They cover rollback, races, retries, stale captures/previews, counter retention,
identity conflicts, disabled fields, report integrity, and database-backed parity.
Post-copy corruption of retained fields, rows, or IDs must cause rollback.
No-op tests cover failed receipt storage, concurrent retries, and later supersession.
Credential tests cover rejected strings and valid Unicode database round trips.

Before production support, define trusted capture provenance, coordinated writers,
loaded-state evidence, counter-backup validation, and frozen manifest/context binding.
Before serving, coordinate a fresh handover that includes intervening changes.
See [the safe-copy contract](REFACTOR_SPEC.md#m4b-2-safe-copy-contract).
A live import, deployment, ownership cutover, or Git shipping needs separate approval.
