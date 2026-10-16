# Database-owned access

Checked on 2026-10-16.

**Local candidate. Not approved for deployment or cutover.**
Independent source review found no remaining blocking issues.
The VPS still uses inventory and FastAPI for live access. This work does not
authorize installation, admission closure, service changes, or cutover.

## Scope

| Area          | Contract                                                                                                                            |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Accounts      | Reuse `ProxySubject` and `ImportedAccess`. Preserve IDs, memberships, flags, protocol credentials, and portal password verifiers.   |
| Ownership     | Separate copying, preparation, node application, and activation. Migrations, release, and deploy never activate ownership.          |
| Public portal | Separate Django listener. Login, link display/reset, installers, subscriptions, and signed client downloads only.                   |
| Private admin | Retain the SSH-only operator service. Portal flags and sessions grant no Django permissions.                                        |
| Fresh links   | Independent signing key, stable subject ID, and durable generation. Retain the 22-character token format. No legacy-token fallback. |
| Edits         | Serialize edits with an ownership lock. Reject stale forms. Retire accounts without deleting rows or reusing names.                 |
| Application   | Root validates a plan before replacing files and restarting sing-box. A restart disconnects existing clients.                       |
| Deployment    | Managed mode renders from PostgreSQL. It never installs the release's inventory-derived `server/config.json`.                       |

Do not add devices, quotas, traffic dashboards/history migration, multi-node support,
invitations, payments, a general node agent, or the capture protocol prototype.
Retain legacy code, data, collectors, backups, and worktrees. Removal needs approval.

## Configuration and boundaries

| Setting                             | Meaning                                                                                                                         |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `control_plane.enabled`             | Keep the private operator service enabled.                                                                                      |
| `control_plane.portal_enabled`      | Permit the separate public service. Default: `false`. This setting does not start or route it.                                  |
| `control_plane.portal_port`         | Loopback listener. Default: `47072`. It must not conflict with another configured TCP listener.                                 |
| `control_plane.portal_origin`       | HTTPS origin without credentials, query, fragment, or a path. Managed deployment requires the deployment host on port 443.      |
| `control_plane.subscription_secret` | Independently generated signing key. Use 50–128 ASCII letters, digits, `_`, or `-`. Deploy preserves it but never generates it. |

Use a cryptographic generator, such as `secrets.token_urlsafe(48)`, for the new key.
Write it directly into protected infrastructure configuration. Do not print it or
put it in command arguments, environment variables, tests, or public source.
Preparation rejects reuse of Django keys, legacy web keys, or protocol credentials.

The public service binds to loopback and rejects requests without HTTPS proxy
context. Nginx overwrites the host, client-address, and protocol headers.
Public and private services use separate cookies. Public cookies require HTTPS.
Public routes expose no operator admin, traffic UI, machine-token API, or archives.
Request logs are disabled for the public application and its nginx site.

The public password checker bounds Argon2 work: memory 131072 KiB, iterations 6,
and parallelism 8. A PostgreSQL lock permits one password computation at a time
across workers. Preparation rejects unsupported verifiers rather than replacing them.
Per-user/address failure lockouts remain separate from aggregate request budgets.
Address and process-wide budgets permit 30 attempts per 60-second window.
Sparse successful logins do not accumulate toward a lifetime lockout.

## Access rules

| Operation                                                        | Required access                                                                            |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Portal login                                                     | Enabled portal membership and correct password                                             |
| Subscription config, link display, password exchange, user reset | Enabled portal membership and an enabled protocol                                          |
| Protected client download                                        | Current link and enabled portal membership. A protocol is not required.                    |
| Direct proxy connection                                          | Enabled protocol membership in the applied node config. Portal membership is not required. |
| Private account management                                       | Django operator permissions. Portal flags and tokens never grant them.                     |

`POST /api/sub` accepts password Basic authentication only. It rechecks the verifier
before issuing a link. A password change during verification cannot expose a
replacement URL. Changing a verifier invalidates existing portal sessions.

A link cannot manage accounts or reset itself. Link reset invalidates subsequent
requests with the old URL. Requests already in progress can complete.
Reset does not revoke cached proxy credentials or disconnect proxy sessions.
Disable or change protocol credentials and apply the node plan for that purpose.

## Private administration

After activation, use **Proxy subjects** in the existing private admin.
Blank password fields retain credentials. New memberships need passwords.
Usernames and subject IDs cannot change. Retired names remain reserved.

Account forms and lists show desired and applied revisions. **Pending or failed**
means that PostgreSQL changed but the node application is not confirmed.
**Recorded applied** describes the last successful application, not a live connection test.
Subscriptions return 503 while the desired revision or render context is unapplied.
Login and protected client downloads remain subject to their separate access rules.

Account editors can copy current subscription URLs from private admin.
View-only operators cannot read those URLs. Prepared URLs are visible to editors,
but they do not work before activation and public routing.
Treat each URL as a credential. Deliver it through a private authenticated channel.
The operator's **Revoke the current subscription URL** action also works for disabled
accounts. It does not change protocol credentials or require a proxy restart.

## Explicit ownership stages

Run commands against the protected plaintext runtime configuration.
Use the existing copied namespace. Do not allocate replacement identities.
Commands below name interfaces, not permission to operate the VPS.

| Stage          | Explicit operation                                                                                     | Result                                                                                       |
| -------------- | ------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| Copied         | Existing account-copy workflow                                                                         | Non-serving accounts and receipt. Inventory still owns access.                               |
| Copy closed    | `account-copy --action close` with the exact admission binding                                         | Permanent closure. Fixture admission must remain disabled.                                   |
| Prepared       | `managed-access --action prepare --namespace COPIED_NAMESPACE`, as `sbm`                               | Bind the destination and independent key. Create new link generations. No serving or edits.  |
| Host fenced    | `managed-access --action fence --acknowledge-interrupt`, as root                                       | Stop/disable the old portal. Publish the persistent ownership marker after freshness checks. |
| Node applied   | `managed-access --action node-plan`, then `node-apply --run-dir PATH --acknowledge-interrupt`, as root | Validate, retain before files, replace, restart, and record the result. No activation.       |
| Active         | `managed-access --action activate`, as `sbm`                                                           | Enable database-backed management and serving gates only after a successful application.     |
| Public routing | Explicit public service and nginx publication                                                          | Expose the new portal, not the old FastAPI routes.                                           |

`managed-access --action status` reports the recorded state.
`--require-active` also requires active ownership and the matching host fence.
`serve` and `health` concern the public listener. They do not migrate or activate data.

Preparation requires one committed production-copy source, closed admission,
no legacy token rows, supported verifiers, and current account/render parity.
The first activation rechecks stored-account parity. Stop if inputs changed.
Do not silently capture, approve, or copy replacement inputs.

The marker is `/var/lib/sing-box-manager-control-plane/owner.json`.
It survives application-tree replacement. Any existing or unsafe marker blocks the
legacy portal. Checks inspect ancestors without following symlinks. A dangling link
or non-directory ancestor is not safe absence. Deployment needs system `python3`
for this read-only check, including before bootstrap. Missing Python blocks the operation. Only the exact bound marker permits managed serving.
Use compatible deployment tools after cutover. Older code can bypass these guards
when run as root and is not a supported recovery method.

## Deployment and first cutover

After activation, the explicit deployment mode is:

```sh
pixi run sbm --config /path/to/operator.sops.yaml deploy --hostname vpn-host \
  --managed-accounts --acknowledge-interrupt
```

It requires full code and all protocols. It rejects bootstrap and `--skip-code`.
Dry runs still contact the host and check ownership. They do not restart services.
Managed publication retains signing keys and stored runtime rosters for recovery
and collectors. Retained collectors are not authoritative accounting for new database accounts.
It does not export new database passwords into release metadata.
Database binding, public host, and public listener changes require separate migration.

For the first cutover, prepare a separately approved operational plan:

1. Check source freshness, destination binding, capacity, and recovery inputs.
2. Stage reviewed code, schema, and protected configuration without activating ownership.
3. Pause account edits and deployments for the handover.
4. Close copying, prepare ownership, fence the host, and apply the exact node plan.
5. Activate ownership only after the recorded node application succeeds.
6. Start the public service, check readiness, and switch nginx.
7. Deliver fresh URLs privately to every installation, including the operator's AI connection.
8. Test refresh, protected downloads, connections, and rejection of old links and routes.
9. Retain a new recovery set with ownership, generations, keys, marker, and application records.

The deployment helper `managed_access.deployment.finish` starts the public service,
checks readiness, and switches nginx. An approved operator script can use it for
step 6 without another node application. It contacts the selected host over SSH.
It requires active ownership and uses that host's stored runtime ports.
It never closes copying, prepares ownership, or activates it.
A full managed deploy applies the node again and therefore causes another restart.
It prepares private Django before node application. It verifies collectors after
application, so changed API listeners exist before the collector uses them.

Do not run deploy, installer, fence, or node operations concurrently.
Keep account edits paused until the first cutover checks finish.

## Failure and recovery

| State                               | Required action                                                                                                                                         |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Partial fence                       | Keep the marker. Legacy access is blocked, but activation is not authorized. Inspect source stability and the stopped portal before selecting recovery. |
| Plan invalidated before application | Retain it. Review the changed inputs before creating another plan.                                                                                      |
| `started.json` exists               | Do not repeat that plan. Inspect installed files, process state, and database application state. The outcome may be uncertain.                          |
| Application failed                  | Keep the candidate and before files. Do not automatically restore old credentials: that can undo revocation.                                            |
| Nginx validation failed             | The helper retains before/after files and restores only the prior nginx file. Ownership and node state do not roll back.                                |
| Nginx reload failed or timed out    | Inspect the live route and retained files. A failed command does not prove that the reload failed.                                                      |
| Database restored                   | Keep serving disabled. Check IDs, generations, closure, keys, destination binding, marker, and node state before live use.                              |

The node supervisor requires the stock installed `sing-box.service`, without drop-ins,
and an active process at the expected binary/config paths. Unknown layouts require review.

Node plans and recovery files contain credentials. Keep their directories root-private.
Process capture has a deadline and a streaming size limit. It does not print checker
output or private database snapshots. Failed application does not restore old files.
A partially replaced binary can make the normal running-process check refuse a new
apply. Recovery then needs an explicit inspected repair, not a guard bypass.

There is no automatic restore/rebind command. A restored database has a distinct
binding. Do not force old report identities or reopen admission to make it serve.
Restoring all trusted state together cannot prove that no later revocation existed.
Resolve that uncertainty before selecting credentials or signing state for recovery.

## Verification boundary

The full Python suite passed 1,478 tests with required PostgreSQL and sing-box checks,
and no skips. Go tests passed with cgo disabled. Independent review covered public
source, docs, and synthetic fixtures. It did not execute tests or inspect live state.

Synthetic tests cover PostgreSQL transactions, migrations, admin permissions,
CSRF, session isolation, link reset and password races, subscriptions, downloads,
deployment mode, private IPC limits, and retained failure outputs.
Node services, privileged installation, and nginx commands use stand-ins locally.
These checks do not prove native service behavior, full-host recovery, or peak VPS capacity.
See [Development](DEVELOPMENT.md#control-plane-checks).
