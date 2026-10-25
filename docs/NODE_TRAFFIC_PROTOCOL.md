# Node traffic wire protocol v1

Checked on 2026-10-25.

This guide describes the `node-traffic-v1` wire schema implemented by the pure
Python and Go codecs. It is node traffic schema 1, not the desktop subscription
envelope. The codec has no HTTP, Django, database, persistence or export side
effects.

The `node-traffic-v1` codec is not wired into production: no
`/node/v1/traffic` route, authorization registry, ingestion, persistence,
exporter or history import uses this schema yet, and G2 remains unaccepted. The
retained `/node-g0/v1/` fixture transport and legacy traffic stores keep their
existing local SQLite writers; this codec does not replace or retire them. See
[MULTI_NODE_SPEC.md](MULTI_NODE_SPEC.md#7-traffic-accounting-and-synchronization)
for the target accounting contract and
[NODE_ACCEPTANCE.md](NODE_ACCEPTANCE.md#g2-progress) for the current evidence
boundary.

## Implementations

| Implementation | Path                                                 | Role                                              |
| -------------- | ---------------------------------------------------- | ------------------------------------------------- |
| Python         | `sing_box_manager/control_plane/traffic_contract.py` | Schema types, parsing, validation and encoding    |
| Go             | `node-agent/internal/traffic/`                       | The same schema, parsing, validation and encoding |

Both implementations read the frozen shared fixture
`tests/fixtures/node-traffic-v1.json` with SHA-256
`12f098d86018b5a82303eac188b418efde35d8c2935960277665a079ded52efb`.
The fixture pins canonical payload and envelope bytes, digests, equivalence
cases, rejection codes and size boundaries. The two test suites are
independent; neither implementation invokes the other.

## Request envelope

All fields are required. The envelope is `{"schema":1,"snapshots":[...]}`
with one through 1024 snapshots:

- `schema` is the strict integer 1.
- `snapshots` is a page, not a delta stream.

Every snapshot contains the common identity fields:

| Field             | Value                                             |
| ----------------- | ------------------------------------------------- |
| `source_id`       | Canonical lowercase UUID string                   |
| `credential_id`   | Canonical lowercase UUID string                   |
| `kind`            | `counter` or `attribution`                        |
| `accounting_date` | Real Gregorian `YYYY-MM-DD`, years 1 through 9999 |
| `version`         | Strict integer from 1 through 2^63-1              |
| `digest`          | Lowercase SHA-256 hex, exactly 64 characters      |

The key `(source_id, credential_id, kind, accounting_date)` identifies one
versioned bucket. Snapshot keys in a request must be unique. Requests carry no
node, account, username or protocol field; a future authenticated endpoint
resolves the node.

### Counter snapshots

A counter adds exactly `upload` and `download`. Both are strict integers from 0
through 2^63-1.

```json
{
  "source_id": "...",
  "credential_id": "...",
  "kind": "counter",
  "accounting_date": "2026-10-23",
  "version": 1,
  "upload": 1024,
  "download": 2048,
  "digest": "..."
}
```

Counter totals are authoritative. The codec validates each absolute snapshot;
later ingestion owns replacement, conflict and monotonic-counter rules.

Attribution is a derived, complete replacement bucket, not a second billing
total. Producers own compaction and removals; the codec does not merge values.

### Attribution snapshots

An attribution snapshot adds `detail`, `domains` and `destinations`.

- `detail` is `exact`, `compacted` or `degraded`. The codec validates the value
  but does not infer it from array contents.
- Each array contains objects with exactly `name`, `upload`, `download` and
  `connections`. All three values are strict nonnegative int64 values.
- Names are nonempty Unicode scalar strings. Their exact values are preserved:
  no case folding, NFC/NFD normalization, punctuation rewriting or spelling
  normalization occurs in the codec.
- Duplicate names within one array are rejected. The two arrays are separate
  sets, so the same name may appear in both.
- Arrays are encoded in exact name order by UTF-8 bytes. Input order is not
  significant.

Presentation sanitization remains a separate responsibility from codec
preservation. Producers own compaction, top-N selection and aggregate-slot
placement.

## Response and receipts

A receipt response uses `{"schema":1,"receipts":[...]}` with one through 1024
receipts.

A receipt contains exactly the common identity fields, without counter or
attribution values. Its `version` and `digest` identify stored state, not
necessarily the submitted version or digest.

Receipt coverage is checked before acknowledging anything:

- The receipt key set must equal the request key set.
- Missing, extra or duplicate receipt keys are rejected.
- Order is not significant.
- Duplicate request keys are rejected before receipt matching.

The codec does not compare receipt versions or digests with the request. A
future exporter acknowledges only an exact match to its current local
key/version/digest; that acknowledgement and any database write are outside
this codec.

## Canonical bytes and digest

The snapshot digest covers the snapshot without its `digest` field. It includes
the kind, identity, accounting date, version and all kind-specific values. The
envelope `schema` is outside the payload.

Canonical encoding:

1. Sort object keys.
2. Sort attribution entries by exact name after rejecting duplicates.
3. Use the shortest decimal integer form; never convert integers through
   floating point.
4. Emit compact UTF-8 JSON with no BOM, insignificant whitespace or trailing
   newline.
5. Disable HTML escaping. Preserve Unicode and quoted or backslashed names.
6. Write U+2028 and U+2029 as lowercase `\u2028` and `\u2029` escapes.
7. Hash those exact bytes with SHA-256.

Whitespace, object-key order, escaped spelling and attribution-array order do
not change the payload. NFC and NFD remain distinct names. C0 controls are
escaped; `<`, `>` and `&` remain literal.

## Strict parsing

The parser rejects:

- malformed JSON, trailing JSON documents and duplicate JSON object keys;
- missing, null, unknown or wrongly typed fields at every object level;
- booleans, floats and exponents where a strict integer is required;
- integer overflow, invalid dates, uppercase UUIDs and uppercase digest hex;
- invalid UTF-8 and unpaired surrogate escapes;
- invalid attribution names or duplicate names within one array;
- duplicate snapshot keys and wrong page sizes.

All object levels reject unknown fields. Input strings are not echoed in
errors; errors carry only the fixed codes below.

## Bounds and counts

| Bound                                     | Value                     |
| ----------------------------------------- | ------------------------- |
| Raw request body                          | 1 MiB                     |
| Raw response body                         | 1 MiB                     |
| Full canonical snapshot, including digest | 768 KiB                   |
| Snapshots or receipts per page            | 1 through 1024            |
| Named attribution identities per bucket   | 10,000 across both arrays |
| Counter and attribution entry values      | 0 through 2^63-1          |
| Version                                   | 1 through 2^63-1          |

The request and response byte checks include the complete encoded envelope,
including commas, brackets and digest fields. A raw body within 1 MiB whose
canonical request exceeds 1 MiB is still rejected.

The five reserved aggregate array slots are excluded from the named-identity
count exactly per array:

- domains: `__other__`, `__unattributed__`, `__ip__`;
- destinations: `__other__`, `__unattributed__`.

A destination named `__ip__` is a named identity. There is no codec rule tying
`detail` to aggregate-slot presence.

The codec enforces these wire limits only. Admission reservation, local storage
budgets and collector policy are later responsibilities.

## Errors

| Code                       | Condition                                                               |
| -------------------------- | ----------------------------------------------------------------------- |
| `invalid_request`          | Malformed or invalid fields, values, JSON, Unicode, names or page shape |
| `unsupported_schema`       | A valid integer schema other than 1                                     |
| `duplicate_snapshot_key`   | Repeated request snapshot key                                           |
| `duplicate_name`           | Repeated name within one attribution array                              |
| `snapshot_digest_mismatch` | Snapshot digest differs from its canonical payload                      |
| `request_too_large`        | Raw request/response or canonical request/response exceeds 1 MiB        |
| `snapshot_too_large`       | Canonical snapshot including digest exceeds 768 KiB                     |
| `receipt_key_mismatch`     | Missing, extra or duplicate receipt key                                 |

Invalid schema types produce `invalid_request`; only a valid integer other
than 1 produces `unsupported_schema`. HTTP status mapping, ingestion conflicts
and authorization errors are not part of this codec.

## Current limits

- This codec slice has no production `/node/v1/traffic` endpoint, authentication
  or authorization registry.
- It adds no PostgreSQL ingestion, version-conflict handling, ownership check or
  receipt persistence.
- It adds no local SQLite writer, export scheduler, anti-entropy sweep, retention
  job or history import. The retained G0 transport and legacy traffic stores keep
  their existing local writers.
- The codec does not resolve per-credential accounting calendars, provider
  resets or time zones. It validates the supplied `accounting_date`.
- The isolated `/node-g0/v1/` transport remains a prototype fixture and is not
  replaced or retired by this schema.
- G2 remains in progress; shared codec vectors are not gate acceptance.
