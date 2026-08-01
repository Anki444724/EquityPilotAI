# Hybrid Storage Architecture

Volume-primary, object-storage-secondary. Built, tested and deployed with the
volume remaining fully authoritative. **No production document has been moved
or deleted.**

## The architecture

```
upload → volume (synchronous, checksummed)  ← authoritative, all reads
              ↓ background job, every 10 min
         object storage (replica)           ← read only if the volume fails
              ↓
         read back, SHA256 vs the recorded hash
              ↓
         state = verified | failed | mismatch | skipped
```

| Concern | Decision |
|---|---|
| Authoritative copy | Railway Volume, `/data/documents` |
| Replica | Railway Object Storage, S3-compatible |
| Write | Volume synchronously; replica in the background |
| Read | Volume first; replica only if the volume cannot serve |
| Verification | Read-back SHA256 against `Document.content_hash` |
| Promotion | Blocked for ≥30 clean days — see below |

### Why replication is asynchronous

A user waiting on an S3 round trip to be told their PDF was accepted is a
worse product, and a bucket outage would otherwise become an upload outage.
The cost is that a document is briefly genuinely unreplicated; the dashboard
reports that as `pending` rather than implying a durability the system does
not yet have.

### Why a mismatch is terminal

A checksum disagreement means two copies of a financial filing differ and one
is wrong. An automatic retry that happened to succeed would erase the evidence
that it ever occurred. `MISMATCH` therefore never auto-retries, alerts as
CRITICAL, and retains **both** the expected and the observed hash.

## Components

| Piece | Path |
|---|---|
| Domain rules, thresholds, promotion policy | `app/domain/documents/replication.py` |
| Replication state | `app/models/replication.py`, migration `e91a4d2c7b60` |
| Replication + read fallback | `app/services/documents/replication.py` |
| Health, alerts, readiness | `app/services/documents/storage_health.py` |
| Dashboard API | `app/api/v1/storage_admin.py` |
| Scheduled job | `JobKind.STORAGE_REPLICATION`, every 10 min, BACKGROUND |

### Endpoints

```
GET  /api/v1/storage/health                 dashboard
GET  /api/v1/storage/replication            per-document state
POST /api/v1/storage/replicate              force a pass
GET  /api/v1/storage/promotion-readiness    the 30-day assessment
POST /api/v1/storage/verify/{document_id}   re-verify both backends
```

All require an operator role: free-disk figures and bucket reachability
describe our infrastructure, not the customer's research.

## Validation performed

Against a real S3 API (moto) and a real database:

| Check | Result |
|---|---|
| Upload → volume, replica not yet present | pass |
| Background replicate → `verified`, 40,021 bytes | pass |
| Read-back SHA256 == recorded hash | pass |
| Volume copy unchanged after replication | pass |
| Read serves the volume by default | pass |
| **Volume failover** — primary offline, replica serves identical bytes | pass |
| **Object-storage failover** — bucket dead, volume unaffected | pass |
| Checksum mismatch → terminal, both hashes retained, no auto-retry | pass |
| Missing primary → `skipped`, not `failed` | pass |
| Poisoned replica on fallback → refused on checksum | pass |
| Both backends down → clear error naming both | pass |

**44 hybrid-storage tests**, plus the full suite. A deliberately corrupted
document in the local database is currently reported CRITICAL by the health
service — that is the detector working, not a false alarm.

## Alerts

Raised to admins on: volume >80% (WARNING) and >92% (CRITICAL), replication
failures, SHA256 mismatch (CRITICAL), object storage unreachable.

Deduplicated on topic and level within a 6-hour cooldown. Replication runs
every ten minutes and an 85%-full volume will still be 85% full on the next
pass; notifying each time would train the recipient to filter the channel.
An escalation from WARNING to CRITICAL always notifies regardless.

## Retention

`PROTECTED_DOC_TYPES` — annual reports, quarterly results, investor
presentations, conference-call transcripts, ESG reports, credit ratings — are
excluded from every automated deletion path.

On temporary OCR files: **the pipeline already creates none.** OCR rasterises
and recognises entirely in memory (`ocr.recognise` takes and returns bytes),
and the only temporary files anywhere in the storage layer are
`tempfile.TemporaryFile` handles that the kernel removes on close. There was
nothing to add; verified by inspection rather than assumed.

## Promotion policy

`GET /storage/promotion-readiness` evaluates mechanically:

1. ≥30 consecutive days of successful replication;
2. zero checksum mismatches;
3. zero outstanding replication failures;
4. every document replicated and verified.

A mismatch resets the clock. All four must pass before
`DOCUMENT_STORAGE_BACKEND` is switched — the decision to trust a beta service
with the only copy of a research corpus should not rest on an impression that
it has been fine.

## Current state and caveats

- **Object storage is not yet provisioned.** Railway's bucket API creates a
  record but never instances it, because Object Storage is in "Priority
  Boarding", their opt-in beta. Until the workspace is enrolled and a bucket
  is created from the dashboard, `ReplicationService.enabled` is `False`, the
  scheduled job reports `skipped`, and the platform runs volume-only —
  exactly as it does today. **Nothing about this deployment changes current
  behaviour.**
- Consequently the replication path has been proven against moto, **not**
  against Railway's actual bucket. Latency, throughput and any S3 dialect
  quirks are unmeasured.
- The 30-day clock has not started; it begins at the first verified
  replication.
- The volume is at 283.6 MB of 500 MB and still the only copy. The hybrid
  design does not by itself solve the capacity problem — it solves the
  durability one, once a bucket exists.
- `DOCUMENT_STORAGE_BACKEND` remains `local` and must stay that way until the
  readiness report passes.
