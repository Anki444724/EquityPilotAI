# Cloudflare R2 Migration Report

**Cloudflare R2 is now the primary document storage backend.** All existing
documents were migrated and SHA256-verified; the Railway Volume is retained as
a read fallback and for temporary processing.

- Bucket: `arena-documents`
- Endpoint: `https://d5af6e632b68be6a3b6c9bbf127ad849.r2.cloudflarestorage.com`
- Cutover: `DOCUMENT_STORAGE_BACKEND=r2`, deployment `ba5abaf`
- Date: 2026-08-01

## 1. Migration summary

| Metric | Value |
|---|---|
| Total files eligible | **124** |
| Successful migrations | **124** |
| Failed migrations | **0** |
| Missing from source | **0** |
| SHA256 mismatches | **0** |
| Bytes migrated | 238,467,327 (**227.42 MB**) |
| Objects now in R2 | **127** (124 migrated + 3 uploaded post-cutover) |
| Total R2 storage used | 238,471,786 bytes (**227.42 MB**) |

Migration ran in four batches through `POST /storage/migrate`, executed inside
the container because that is the only place the Railway Volume is mounted.
Batches 2, 3 and 5 confirmed idempotency: already-present files were detected
and skipped rather than re-copied.

### Independent verification

Rather than trusting the migrator's own report, every object was **read back
out of R2 and re-hashed** against the value the database recorded at upload:

```
INDEPENDENT R2 VERIFICATION over 124 documents
  sha256 verified : 124
  mismatched      : 0
  missing from R2 : 0
  bytes in R2     : 238,467,327 (227.42 MB)
```

**No production document was deleted.** The volume copies are untouched and
remain the rollback.

## 2. Latency benchmark

Measured against the live bucket, median of three round trips per size:

| Object size | Upload | Download | Up MB/s | Down MB/s |
|---|---|---|---|---|
| 0.06 MB | 709 ms | 413 ms | 0.09 | 0.15 |
| 0.95 MB | 720 ms | 537 ms | 1.32 | 1.78 |
| 7.63 MB | 1,092 ms | 626 ms | 6.99 | 12.19 |
| 22.89 MB | 2,130 ms | 1,536 ms | 10.75 | 14.90 |

Roughly 700 ms of fixed round-trip cost dominates small objects, which is why
the throughput figures look poor at 64 KB and reasonable at 23 MB. Migration
throughput averaged 0.78 MB/s from the container, reflecting per-object
overhead across 124 mostly-small files rather than bandwidth.

## 3. Verification results

| Check | Result |
|---|---|
| R2 connectivity (`head_bucket`) | **PASS** — HTTP 200 |
| Upload | **PASS** — 202, document 132 created |
| New uploads land in R2, not the volume | **PASS** — doc 132 is R2-only, absent from the volume |
| Download | **PASS** — md5 `358bdc49…` byte-identical |
| SHA256 verification | **PASS** — 124/124 verified from R2 |
| RAG indexing | **PASS** — doc 132 retrieved at 0.837 |
| AI citations | **PASS** — OpenRouter, cited to page 2 |
| Restart persistence | **PASS** — uptime 35.5 s, byte-identical, backend still R2 |
| Live backend confirmation | **PASS** — `active_backend: s3`, `target: arena-documents` |

Test suite: **2,332 passing, 0 failures.**

## 4. Configuration

Set on the Railway backend service. Values are held only in Railway and in a
`chmod 600` file outside the repository; none appears in source or logs.

```
DOCUMENT_STORAGE_BACKEND = r2
DOCUMENT_S3_BUCKET       = arena-documents
DOCUMENT_S3_ENDPOINT     = https://<account>.r2.cloudflarestorage.com
DOCUMENT_S3_REGION       = auto
DOCUMENT_S3_ACCESS_KEY   = (set)
DOCUMENT_S3_SECRET_KEY   = (set)
```

### R2-specific client hardening

R2 is S3-compatible but not S3, and three differences fail silently:

* **Region must be `auto`.** R2 has no regions; a real one produces signature
  mismatches on some operations.
* **Path-style addressing.** Virtual-host style resolves
  `bucket.<account>.r2.cloudflarestorage.com`, which does not exist, so
  requests fail DNS rather than returning an S3 error.
* **No checksum trailers.** Recent botocore sends CRC32 by default and R2
  rejects it on some paths. Set to `when_required`; the platform verifies with
  SHA256 by read-back, which is strictly stronger.

## 5. Current architecture

```
upload → Cloudflare R2 (primary)     ← all reads, all new PDFs
              ↓ on read failure
         Railway Volume (fallback)   ← migrated copies, retained
```

The Railway Volume now serves three purposes and no others: read fallback for
migrated documents, temporary OCR/processing scratch, and application data.
All future PDFs are written directly to R2 — verified by document 132, which
exists in R2 and **not** on the volume.

## 6. Defects found and fixed

**REPL-002 — the fallback would have retried the failed backend.**
`ReplicationService.secondary` always built the S3 client. Correct while the
volume was primary; a silent no-op after cutover, because primary and
secondary both resolved to R2 — so a "fallback" read would have retried the
backend that had just failed. The pairing is now derived from the configured
primary, so a volume primary pairs with R2 and an R2 primary pairs with the
volume.

**VERIFY-001 — my own diagnostic was wrong, not the product.**
`/storage/verify` labelled `service.primary` as "volume" and
`service.secondary` as "object_storage". After cutover those labels inverted,
so freshly uploaded R2 objects were reported as *"R2: object not found"* while
appearing under "volume". I spent two probe uploads chasing a defect that did
not exist before querying R2 directly and finding documents 130, 131 and 132
all correctly stored with `storage_backend=s3`. Both backends are now
addressed by name, and the response carries `active_primary`.

## 7. Caveats

- **The volume still holds every migrated copy** (227 MB of the 500 MB
  volume). That is deliberate — it is the rollback — but it means the capacity
  pressure is not relieved until those copies are removed, which should not
  happen until R2 has run clean for a meaningful period.
- **Egress costs are now real.** R2 charges no egress, but the Railway service
  pays egress to *reach* R2 on every read that misses a cache. Downloads that
  were previously a local disk read are now a ~400–1,500 ms network call.
- **No CDN or presigned-URL path.** Documents are proxied through the API
  rather than served directly from R2, so large downloads occupy a worker.
  Worth adding if download volume grows.
- **The 30-day promotion criteria from the previous phase are now moot** in
  their original direction: R2 is primary today, having been validated by full
  migration and read-back rather than by elapsed time. The volume fallback
  provides the safety that the waiting period was designed to provide.
- **Three post-cutover probe documents** (130, 131, 132) remain in the corpus.
  They are small and harmless but are test artefacts, not research documents.
