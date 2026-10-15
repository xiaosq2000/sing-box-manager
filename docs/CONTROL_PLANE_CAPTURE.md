# One-time first-copy plan

Checked on 2026-10-15.

**First checkpoint of basic live migration: a supervised, non-serving account copy.**
The Django foundation is live. No real proxy accounts are copied into PostgreSQL.
The [account-copy path](CONTROL_PLANE_ACCOUNT_COPY.md) is implemented, independently
source-reviewed, and tested with synthetic sources. It is not deployed.
Database-backed serving and fresh-link issuance remain unimplemented.
Goal or policy approval does not authorize deployment, capture, import, or cutover.

## Delivery goal

Existing users connect and refresh subscriptions while the operator manages their
access through Django admin. PostgreSQL becomes authoritative for these accounts.
The copy below is a verification checkpoint, not the final delivery target.
See [M4b](REFACTOR_SPEC.md#m4b-control-plane-devices-and-subscriptions) for completion checks.

Defer traffic dashboards/history migration, quotas, device enrollment, multi-node
support, and the general node agent. Preserve existing data, backups, and collectors
unless an approved cutover disables them. Do not extend the capture protocol prototype.

## Selected link policy: fresh links at cutover

Preserve account names, memberships, flags, protocol credentials, and portal password
verifiers. Issue new subscription links under independent signing state at cutover.
Do not reuse the old subscription secret or use the Django session/signing key.
This is new link issuance, not proof of old reset history or a reset of proxy passwords.

- Copy accounts without activating old or new subscription links.
- Record unavailable legacy counters as unavailable. Never manufacture verified zeros
  or usable legacy-token records to satisfy the fixture import format.
- Missing legacy reset history does not block this account-copy path. Preserve any
  retained history without lowering counters or relabeling old state as new evidence.
- Give each installation its new URL through an authenticated, private handoff.
  Coordinate updates before accepting the cutover as complete.
- After cutover, reject old links on every subscription and token-protected download
  route. Do not retain a fallback verifier or old portal route that still accepts them.
- Existing proxy configurations may keep connecting, but old URLs will stop refreshing.
  Test new-link refresh and connectivity on current clients, including the operator's
  connection. Do not assume automatic client reconfiguration.
- New links need durable reset/revocation state. This security state is not optional
  traffic statistics. Rotating one user's new link must invalidate its predecessor.

The policy does not rotate secrets now. Keep old source services and signing state
unchanged during the copy. A later cutover needs explicit operational approval.

## First-copy checkpoint

```text
Selected stored inventory + checked auth/proxy rendering
                         ↓
              private, operator-selected inputs
                         ↓
                 preview → approved apply
                         ↓
            checked PostgreSQL account copy
                  (no links activated)
```

The first verified real-account copy starts data migration. Inventory, FastAPI, and
sing-box keep serving clients until cutover. Devices and the node agent are not
prerequisites. Copy success alone does not transfer serving or ownership.

## Keep the claim narrow

The copy must match the selected stored-source snapshot. It must not claim that
running portal/proxy processes loaded those files. Disk consistency is not loaded-state
proof. Record the source window, exact input identities, destination, and limitations
privately. Reconcile fresh account/credential state before serving. Report discrepancies
with effective access before a config apply, rather than silently activating pending changes.

Trust the operator and source host. Acquire inputs through an approved authenticated
connection and retain the original private artifacts. Checksums bind bytes, not
provenance alone. Operator selection supplies custody for this one-time procedure.
It does not establish automated proof against source/database rollback.

Record unknown username or reset history. Stop on known identity ambiguity, rename,
reassignment, or retired-name reuse. Fresh links do not resolve those account issues.
Do not claim lifetime revocation proof for the old system.

## Keep and set aside

| Keep for this migration                                  | Defer                                            |
| -------------------------------------------------------- | ------------------------------------------------ |
| Django/PostgreSQL and isolated account copying           | Witness ledger and reservation tickets           |
| Complete field and renderer comparisons                  | Source journal and capture coordinator           |
| Strict private inputs and sealed reports                 | Peer RPC, launch permits, and worker attestation |
| Transactional apply, destination binding, and receipts   | General automatic crash/restore recovery         |
| Backups, fresh-link tests, and explicit cutover approval | Traffic features, devices, and node agent        |

Preserve any separately retained prototype code, tests, schema, worktrees, and check
receipts. The witness/journal protocol is not part of this implementation or a prerequisite.
Do not include prototype migration `0003_capture_records` or its model additions
in first-copy support by default. Select only required support changes. No cleanup
is authorized.

## First action: read-only source preflight

Use the target and read-only scope approved by the operator. Inspect stored runtime,
auth, and proxy inputs for agreement. Check counter-file availability without creating
missing databases or recovering SQLite sidecars. An absent file may be normal for
code that creates it on first reset, but absence is not proof of no earlier resets.

Check target schema/support availability, resources, and retained backup metadata.
A backup's existence is not a restoration test. Keep detailed findings in a private
operator report. Print counts and fixed error codes, not account records, credentials,
verifiers, tokens, URLs, or private keys. Request no plaintext password.

Make no service changes. A new source backup, helper installation, or retained capture
needs its own scope. Do not inspect unrelated traffic records.

## Minimum implementation

The separate `account-copy` command implements export, authorization, preview,
apply, and permanent closure. It reuses the [fixture importer's](CONTROL_PLANE_IMPORT.md)
field/render comparisons, transaction boundaries, private-file checks, and receipts.
Do not relabel real inputs as fixtures or enable `fixture_enabled` operationally.
Keep the existing fixture format and tests unchanged. Deferred capture records do
not supply production admission. `account_copy.0001_initial` adds the separate gate
and depends on the fixture account tables, not the deferred capture migration.
See the [command and schema guide](CONTROL_PLANE_ACCOUNT_COPY.md).

Preserve every portal/protocol membership, including disabled records, exact flags,
credentials, and password verifiers. Preserve exact usernames and the rendering
context. Compare the complete expected projection against database-loaded rows.
Do not use the local workstation's access roster as a substitute for selected source bytes.

Pause operator configuration edits during extraction. Verify the same bytes used for
validation and retain private originals without overwriting prior artifacts. Ordinary
legacy link resets may continue because the copy serves nothing and does not promise
legacy-link continuity. If counter artifacts are retained, validate them consistently,
including WAL state. Do not use the permissive portal counter reader as an export validator.
The account copy needs no proxy restart or loaded-state attestation.

## Supervised operation

Each live step needs explicit scope. These are not runnable commands.

1. **Prepare.** Approve source, destination, support installation/migrations, and private
   backups. Check recovery artifacts. Do not use a proxy deploy merely to install import support.
2. **Capture.** Pause operator configuration edits. Retain and validate exact inputs.
   End that pause after extraction checks, not after an indefinitely open import session.
3. **Preview.** Check production admission, destination, all fields, and rendered configs.
   Bind the report to the fresh-link policy. Do not claim old-token parity.
4. **Apply.** Approve the exact report. Recheck bytes and destination state under database
   locks. Commit copied rows and their receipt atomically. Activate no subscription links.
5. **Confirm.** Read the committed copy and receipt. Retain inputs, reports, and backups.
   Record snapshot parity, not equality with later source state.

Resolve an uncertain commit through the exact report-bound receipt before retrying.
Reject stale/conflicting reports. Do not restore databases, lower counters, rotate
credentials, delete evidence, or repeat service operations as automatic recovery.

## First-copy completion

Done when an approved real-account snapshot is copied into PostgreSQL and complete
field/config parity passes. No link or proxy configuration changes occur in this step.
Continue to database-backed serving, independent fresh-link issuance, and basic account
ownership. Test new-link success, old-link rejection, reset/revocation, protected
downloads, and client connectivity before the separately approved cutover.

See the [roadmap](REFACTOR_SPEC.md#m4b-2-safe-copy-contract) and
[fixture-only importer](CONTROL_PLANE_IMPORT.md) for retained behavior.
