# Client diagnostics design

Checked on 2026-10-22.

Status: implemented, with local fixtures and KVM core acceptance.
Independent Astra xhigh review and one fix iteration are complete.
PR checks remain required before merge. This document defines the contracts
and acceptance gates. See [the client guide](README_SBC.md#network-diagnostics)
for usage.

## Scope

Diagnose traffic through sbc under its active destination rules. Do not measure
an unrelated shell proxy or force every destination through the remote proxy.

| Runtime   | Request path                                                                                    |
| --------- | ----------------------------------------------------------------------------------------------- |
| Mixed     | Installed ordinary mixed inlet, with its authentication, even when shell proxy settings are off |
| Linux TUN | Native connections, with environment proxies disabled and committed owner state validated       |

Both paths retain direct exceptions and the selected route. The selected protocol
is not necessarily the outbound used by a particular request. These results do not
prove that another application's proxy settings, caches, or existing connections
are clean.

The first release includes website checks, exit-IP observations, bounded downloads,
a local website list, localized output, cancellation, and versioned JSON.

Do not add a service, database, external dependency, privileged diagnostic API,
protocol benchmark, browser probe, or background monitor. Keep Cobra/pflag and the
Go standard library. Build with cgo disabled. Do not change TUN activation,
shutdown, recovery, ownership, or routing policy for this feature.

## CLI contract

The command interfaces are:

```text
sbc ip [--dual-stack] [--json]
sbc check [URL...] [--preset NAME] [--samples N] [--json]
sbc check targets list
sbc check targets add URL
sbc check targets remove ID
sbc check targets import FILE
sbc check targets export
sbc check targets reset
sbc speed download [--size 20MB] [--json]
```

- Bare `sbc speed` prints help and sends no request.
- Explicit URLs and `--preset` are mutually exclusive. Otherwise, use the saved list.
- Help, completion, and list operations send no destination request.
- Validate the complete invocation before runtime access or requests.
- Reject retired flags before requests. Do not redirect them to another action.
- Do not switch routes or protocols, restart services, invoke sudo, or repair state.
- Do not retry through direct access or an old mixed endpoint after a failure.

### Website checks

| Item               | Contract                                                                                                |
| ------------------ | ------------------------------------------------------------------------------------------------------- |
| Request            | HTTPS GET to the specified URL, with TLS verification and no cookies or account credentials             |
| Samples            | Default 1 per target, accepted range 1–5, no hidden retries                                             |
| Connections        | Fresh client-side connection per sample, without clearing engine or system caches                       |
| First response     | Elapsed time from request start to the first HTTP response byte                                         |
| Summary            | Median of recorded HTTP response times, including non-2xx responses, with response and failure counts   |
| HTTP               | Preserve each status. Do not follow redirects. A response does not prove that the server allowed access |
| Body               | Read at most 8 KiB per sample. Do not download a complete page                                          |
| Unavailable timing | Report unknown, not zero. Mixed remote DNS timing is not a local DNS measurement                        |

Count missing responses separately from non-2xx responses. Preserve individual
samples when results differ. Never call request failures packet loss.

A 403 means the server responded but denied access. A 3xx means the requested
visit ended at a redirect. Neither meets the default URL-access goal of HTTP 2xx.
A valid first-response time remains useful when HTTP access fails.

These checks do not measure browser rendering, subresources, authenticated access,
video playback, or HTTP/3 performance. Fresh client connections do not imply fresh
underlying proxy sessions. A small resource on a CDN does not represent another host.

### Exit IP

Retain IPify and ident.me as separate source rows. Default to IPv4. An explicit
`--dual-stack` also requests their IPv6 observations, subject to the active policy.

- Validate each returned address and its requested family.
- Keep different source results separate. Split routing can produce different exits.
- A source proves only the exit it observed, not the exit used for another website.
- Ordinary websites cannot supply an exit-IP observation unless they expose that information.
- Send each distinct observed IP to ipwho.is once per command for optional details.
- A detail failure retains the IP, reports unknown details, and produces a warning.
- Database estimates do not prove a provider's region classification or account eligibility.

The family describes the observed exit, not the loopback connection to a mixed
proxy. A failed IPv6 request does not prove a network-wide outage or working rejection.

Linux TUN keeps its public IPv6 restriction. Distinguish configured policy,
policy-skipped checks, observed rejection, and unexplained failures. A skipped
check is not tested or verified. Only use an expected-policy label when it applies
to the active generation. Otherwise, report unknown policy or the actual failure.
A policy-skipped IPv6 check does not invalidate a successful IPv4 observation.
Do not bypass the restriction to complete a dual-stack report.

### Download throughput

The explicit `download` action authorizes a bounded payload transfer. Default to
20 decimal MB. Accept whole-MB sizes from 1 through 99 MB.

- Use the existing Cloudflare download source through the current sbc path.
- Do not accept arbitrary download URLs or perform upload tests in the first release.
- Request identity encoding. Measure a single HTTP response stream, not maximum line capacity.
- Report actual payload bytes, body-transfer duration, and Mbit/s separately from first-response latency.
- Distinguish payload completion, transfer-window expiry, stalls, and failed responses in both output formats.
- Bound connection time, total command time, transfer time, idle time, and payload reads.
- Print the payload budget before transfer. Support cancellation throughout the request.
- Reject invalid responses and stalled transfers. A useful result requires received bytes.

Application read limits are not exact wire-byte limits. Kernel buffers, TLS,
protocol overhead, and proxy accounting can add traffic. Do not claim an exact
server bill, or zero server traffic, from the route mode alone.

## Website lists and privacy

Maintain one private, versioned JSON list in the owner's client config directory.
Limit each saved or executed list to 24 targets. List mutations use atomic writes
and the existing operation lock. Network diagnostics do not acquire a write lock.
Normal request counters and engine caches can change during diagnostics.

| Operation        | Contract                                                                                   |
| ---------------- | ------------------------------------------------------------------------------------------ |
| Missing list     | Use a small built-in non-AI default in memory. Do not write a file                         |
| Saved empty list | Keep it empty. A check with no targets fails before requests                               |
| Add/remove       | Change only the requested saved targets, using stable target IDs                           |
| Import           | Read a bounded local file, validate all entries, then atomically replace the saved list    |
| Export           | Write the saved list to stdout. Do not create an automatic backup or upload                |
| Reset            | Explicitly replace the saved list with the built-in default                                |
| Preset           | Select a small read-only built-in set for this invocation, without changing the saved list |

Provide small `default`, `china`, `global`, and explicitly selected `ai` presets.
Review exact endpoints during implementation. Prefer stable HTTPS resources on the
target host. A 200 response with a small body can improve evidence collection,
but must not become a required server behavior.

Reject non-HTTPS URLs, embedded credentials, invalid ports, control characters,
and obvious local literal destinations by default. Preserve legitimate paths and
query parameters. Do not silently rewrite a supplied URL or deduplicate by hostname.
Different endpoints on one host can test different behavior. Exports preserve full
URLs for round trips and must be treated as private configuration.

Apply the 64 KiB limit to the final serialized file before atomic replacement.
Reject special files and symbolic links. File opens must not wait for FIFO writers.

Do not resolve mixed targets locally for validation. Engine DNS and route behavior
must remain intact. These checks do not provide complete SSRF protection. Imported
lists are user-selected local requests, not remotely scheduled jobs.

Do not support executable templates, custom authentication headers, remote list
subscriptions, or persistent result history. Preserve existing file protection.
Sanitize terminal output. Redact query values and potentially secret path components
in errors and reports. Do not emit subscription links, proxy credentials, API
secrets, or full connection-table responses.

Default checks make no AI-provider request. Explicit AI targets follow the active
rules and can expose the direct public IP. State that risk. A guard for `direct`
mode alone does not protect every split route or direct exception. Remove provider
page scraping from IP diagnostics. Do not promise account access or protection from
provider enforcement.

IP lookup services receive observed IPs. Observed addresses remain intentional
output from `sbc ip`, so JSON reports can contain sensitive data. Do not store,
upload, or commit real reports automatically.

## Runtime context and capture evidence

Use one shared execution path for context checks, transports, observations, and
rendering. Keep measurements separate from CLI printing and file mutations.

1. Validate invocation and target configuration.
2. Select the runtime before destination access.
3. Validate committed TUN state, ownership, API identity, and available generation metadata.
4. Record mode, route, selected protocol, and generation before each sample.
5. Execute the request and collect timing and routing evidence independently.
6. Recheck context after samples and before the final result.

Pending, dirty, inconsistent, missing-owner, and unavailable-API states fail
without destination requests. A TUN error never selects mixed mode. Do not read
privileged engine configuration or reduce file protection to read metadata.

Cancel when context checks detect a mode, route, protocol, or generation change. Preserve
completed observations as invalidated or partial. Equal start/end snapshots cannot
exclude an intervening change that returns to the original state. Do not claim an
atomic runtime freeze.

### Asynchronous observation

Use one bounded, command-scoped, in-memory observer for the authenticated local
API. Collect event-triggered, rate-limited connection snapshots while requests run.
Match source sockets, inbound identity, new connection IDs, and available
destination metadata. Derive inlet tags from validated managed metadata, not
hardcoded names. Do not expose unrelated connections.

A complete snapshot interval must fit inside the tracked socket's lifetime.
End that lifetime on closure or a read/write error without an API wait.
Each connection ID has one socket owner. Port reuse cannot transfer proof.
Ambiguous IDs have no capture proof or path. Contradictory chains have an unknown path.

Trace callbacks only record timestamps or enqueue signals. They must not block on
API queries. Download reads must not pause for evidence. API deadlines, snapshot
sizes, sampling rate, retained records, and total observer lifetime must be bounded.
Observer cancellation must not leave a worker running.

Capture observations are best effort. Remote closure, an empty 204 response, or
engine cleanup can remove a record before inspection. An open response body or a
wrapped connection close cannot force the engine to retain a record.

| Evidence                          | Result                                                                                       |
| --------------------------------- | -------------------------------------------------------------------------------------------- |
| Matching TUN record               | Capture verified for that request. Report an outbound only when its chain supports the claim |
| Matching mixed record             | Report the observed chain, not the selected protocol as a substitute                         |
| Missing or ambiguous record       | Capture or path unverified. Never infer direct access                                        |
| HTTP response but no TUN evidence | Retain the HTTP observation, but fail overall scope verification                             |

A post-connect check cannot prevent source-IP exposure when the initial TCP
connection escapes capture. Do not describe this observer as a leak-prevention
mechanism. Do not insert delays into a request to make capture evidence appear.

## Output and exit status

Human output identifies scope as sbc, the runtime, route, selected protocol,
measurement target, and actual evidence. Keep HTTP errors distinct from connection
failures and unverified scope. Preserve useful partial results.

`--json` emits one versioned result to stdout, without progress text. Use a shared
envelope with `schema_version: 1`, `command`, `scope`, `mode`, `route`, `protocol`,
`context_valid`, `interrupted`, and `observations`. Use null for unavailable values.

Each observation separates transport outcome, HTTP data, capture evidence, and
IP validation where applicable. Retain per-sample results. Put optional detail
failures in warnings. Human and JSON output use the same result model.

Samples include `transport`, `issue`, nullable `http_status` and
`first_response_ms`, `http_outcome`, payload/transfer data, download `stop_reason`, and `capture`.
HTTP outcomes distinguish accepted, redirect-not-followed, restricted,
client-error, server-error, and not-tested. IP rows include `ip_validation` and
optional database details. Reports identify hosts and stable IDs, not full URLs.

| Code | Meaning                                                                                                    |
| ---- | ---------------------------------------------------------------------------------------------------------- |
| 0    | All required goals pass, with no observed context change and verified successful TUN core requests         |
| 1    | A required goal fails, context becomes invalid, or TUN scope cannot be verified                            |
| 2    | Invalid invocation or configuration, retired flags, or an empty selected list, before destination requests |
| 130  | SIGINT cancellation, with available observations marked partial                                            |
| 143  | POSIX SIGTERM cancellation, with available observations marked partial                                     |

For `check`, every requested sample must return HTTP 2xx. A 3xx, 4xx, 5xx, or
failed sample returns 1. For `ip`, every required source must return a valid
address. Optional detail failures are warnings. For downloads, require a valid
2xx response, payload bytes, and no stall or scope failure. Policy-skipped IPv6
is explicit and never described as a verified dual-stack success.

## Implementation order and cut list

D0–D4 implementation is present. Local fixtures and isolated KVM core acceptance
pass. Independent review and one fix iteration are complete. CI remains a merge gate. This work does not authorize
a release, deployment, live network probe, privileged installation, or operator-state cleanup.

| Stage | Deliverable and gate                                                                                                    |
| ----- | ----------------------------------------------------------------------------------------------------------------------- |
| D0    | Fixture-only feasibility prototype: runtime identity, asynchronous evidence, timing isolation, and rapid-close behavior |
| D1    | Shared context, transports, result model, bounded cancellation, and observer based on D0 evidence                       |
| D2    | `check`, saved targets, presets, validation, and offline list operations                                                |
| D3    | IP observations, explicit download action, localized renderers, and shared JSON contract                                |
| D4    | Remove obsolete code and generated config elements. Update docs, help, completion, and acceptance tests                 |

The owner state binds transaction ID, inlet tag, and recognized IPv6 policy to
the committed generation. Missing generation metadata fails before destination
access and requires local refresh.

| Bound                                                  | Value               |
| ------------------------------------------------------ | ------------------- |
| Command lifetime / concurrent targets                  | 60 seconds / 4      |
| Website or IP request                                  | 5 seconds           |
| Download request / transfer window / idle              | 30 / 15 / 5 seconds |
| Snapshot deadline / retained records / tracked sockets | 250 ms / 4096 / 256 |
| Snapshot body / selection body                         | 2 MiB / 64 KiB      |
| Observer periodic interval / minimum query interval    | 20 ms / 5 ms        |
| Target-list file / managed metadata read               | 64 KiB / 8 MiB      |

Snapshot queries are serialized. The observer retains only tracked socket evidence.
Normal small responses and immediate closure have distinct test coverage. Evidence
misses stay visible. If a change needs timing interference or weaker success
criteria, stop and report the blocker before expanding the work.

Remove these old diagnostic contracts:

- Default `speed` API-delay ranking and fastest-protocol recommendations.
- `speed --all`, `speed --download`, `ip --ai`, and `--allow-direct-ai` behavior.
- Provider-region scraping and proxy-only diagnostic inlet validation.
- The generated `speed-test` inbound, selector, and route override after removing their consumers.
- Obsolete translations, completion entries, documentation, and tests for those contracts.

Replace obsolete tests with tests for the new contracts. Do not remove safety
coverage. Remove generated inlets through normal candidate publication, never
through a diagnostic rewriting installed or active configuration. Superseded root
generations remain subject to the existing retention policy.

## Acceptance checklist

Use synthetic data and the isolated harness described in
[Development](DEVELOPMENT.md). Local Go checks and the KVM core suite exercise the
contracts below. CI must verify supported platforms before merge. Never use live
AI endpoints in automated tests.

| Area              | Required evidence                                                                                                                           |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Invocation        | New syntax, retired flags, conflicts, empty lists, help, and completion validated before requests                                           |
| Request semantics | Authenticated ordinary mixed inlet with shell proxies off. Native TUN ignores environment proxies. No local mixed target pre-resolution     |
| Route parity      | All five modes, direct and proxy branches, exact direct exceptions, and a dead unused proxy under `direct`                                  |
| Scope proof       | Real-engine TUN matching, small 200 responses, empty 204, immediate FIN, missing records, and ambiguous records                             |
| Timing            | Delayed or stalled local API cannot add a synchronous wait to TTFB or download reads                                                        |
| Errors            | DNS, TCP, TLS, malformed response, redirects, HTTP denial, rate limits, 5xx, partial samples, and optional detail failure                   |
| Guards            | Pending/dirty capture, invalid owner state, unavailable APIs, and an answering stale mixed endpoint never cause fallback                    |
| Runtime changes   | Detected route, protocol, mode, and generation changes cancel work and invalidate partial results                                           |
| IPv6              | Policy labels match evidence. Unknown failures never count as proof of rejection. DNS suppression and literal-address rejection stay intact |
| Lists             | Missing/empty distinction, 24-target bound, exact endpoint identity, atomic import, no import requests, URL redaction, and file protection  |
| Resource limits   | Finite requests, concurrency, deadlines, body reads, observer memory/API rate, and shutdown on cancellation                                 |
| Download          | Size bounds, byte counting, latency/rate separation, invalid responses, stalls, cancellation, and honest budget notices                     |
| Output            | Shared JSON fixtures, null unknowns, strict exit codes, Chinese/English parity, and no credentials or unrelated connections                 |
| No mutations      | Diagnosis preserves PID, generation, route, protocol, installed config bytes, services, and client settings                                 |
| Native isolation  | Real sing-box in a disposable VM. Host network and services remain unchanged. Loopback tests alone cannot establish TUN acceptance          |

Unsafe TUN state still refuses requests. A matching committed generation and
owner-readable diagnostic metadata are required. Existing generations without that
metadata need manual local refresh. The core VM suite checks five modes, direct
exceptions, expected native IP/download paths, and honest rapid-close evidence outcomes.
An independent negative control changes only the owner's diagnostic inlet tag.
HTTP must still succeed, but capture proof and the overall command must fail.

## Sources

- [Current client behavior](README_SBC.md#network-diagnostics),
  [TUN scope](REFACTOR_SPEC.md#m1b-linux-tun), and
  [secret handling](SECRET_HANDLING.md).
- Runtime selection: `internal/cli/runtime_api.go` and `internal/cli/checks.go`.
- Measurements: `internal/diagnostic/`, `internal/probe/`, and `internal/api/`.
- Config generation: `internal/singbox/`, `internal/tun/config.go`, and
  `sing_box_manager/desktop_config.py`.
- MyIP inspiration: [website checks](https://github.com/jason5ng32/MyIP/blob/b7cf99fc367423874d286ab194e518a4e56ec6c0/frontend/components/ConnectivityTest.vue)
  and [website lists](https://github.com/jason5ng32/MyIP/blob/b7cf99fc367423874d286ab194e518a4e56ec6c0/frontend/utils/features/connectivity-lists.js).
  Adopt the destination-focused interaction, not browser timing or success assumptions.
