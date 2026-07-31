# Asynchronous Document Ingestion

Replaces the synchronous upload path, in which parsing, OCR, chunking and
embedding all ran inside the HTTP request and the uploaded bytes were
discarded when it finished.

---

## Why it was replaced

| Problem | Consequence |
|---|---|
| Pipeline ran inside the request | A 500–1000 page report takes minutes. Railway's edge closes the connection first, so the client sees a failure for work that may still be running. |
| Original bytes discarded | Re-index had nothing to re-parse. The only remedy was to ask the user to upload again. |
| Failure lost the document | A 200 MB report failing on page 900 left a row that could never be completed. |
| One request per upload | A slow upload occupied a request worker for its whole duration. |

---

## Architecture

```
POST /documents/upload
  ├─ validate extension, size, free disk
  ├─ stream bytes to storage       ← Railway Volume or S3/R2
  ├─ INSERT document (status=uploaded → queued)
  ├─ INSERT document_job (status=queued)
  └─ 202 Accepted + status_url          ~280 ms, nothing parsed

DocumentWorker (separate loop, own thread or own service)
  ├─ claim job          conditional UPDATE — safe with N workers
  ├─ read bytes back from storage
  ├─ parse → OCR → layout → tables → sections
  │    → entities → financials → chunking → embedding → knowledge
  │    …each stage appends to processing_log and advances progress
  └─ status=completed          (source bytes retained)
```

The request never parses. The worker never receives bytes over the wire — it
reads them from storage, which is what makes retry and re-index possible.

---

## 1–2. Storage and the recorded path

`app/services/documents/storage.py` defines `DocumentStorage`, with two
implementations, chosen by configuration:

| Backend | Class | Used for |
|---|---|---|
| `local` | `LocalFileStorage` | Railway Volume, or any mounted path |
| `s3` / `r2` / `minio` | `S3CompatibleStorage` | S3, Cloudflare R2, MinIO |

Nothing above the interface knows which is active.

Keys are content-addressed — `documents/<company_id>/<sha256>.pdf` — so a
byte-identical re-upload writes the same key rather than a second copy, keys
cannot collide, and a corrupt read is detectable by rehashing.

Writes are atomic: a temporary file in the same directory, `fsync`, then
`os.replace`. A worker crashing mid-write leaves the previous object intact
rather than a truncated PDF.

Three columns record where the bytes went:

```python
storage_key:      str | None   # documents/<company>/<sha256>.pdf
storage_backend:  str | None   # "local" | "s3"
storage_location: str | None   # absolute path or s3:// URI
```

`storage_key` is nullable **only** so rows created before this change remain
readable. Those cannot be re-indexed, and the API says so explicitly rather
than failing obscurely.

---

## 3. Upload endpoint

`POST /api/v1/documents/upload` → **202 Accepted** (was 201).

```json
{
  "document": { "id": 1, "status": "queued", "chunk_count": 0 },
  "action": "created",
  "job_id": 1,
  "status_url": "/api/v1/documents/1/status",
  "message": "Upload stored and queued for ingestion. Poll status_url for progress."
}
```

The file is streamed to storage via `file.file`, not `await file.read()`, so a
200 MB report costs a 1 MB buffer rather than 200 MB of resident memory.

---

## 4–5. Statuses and progress

| Status | Progress | Meaning |
|---|---|---|
| `uploaded` | 0 % | bytes durably stored; no job yet |
| `queued` | 2 % | job enqueued |
| `processing` | 5 % | worker claimed it |
| `ocr_complete` | 45 % | text extracted |
| `chunked` | 70 % | chunks built |
| `embedded` | 90 % | vectors generated |
| `completed` | 100 % | indexed and citable |
| `failed` | 100 % | terminal; **source retained** |

`uploaded` is distinct from `queued` deliberately: the bytes are safe before
any job exists, so a failure to enqueue does not lose the document.

Only `completed` counts as indexed — `DocumentStatus.is_indexed`. The AI
context builder and search filter on it, so a half-ingested document is never
cited.

---

## 6. Re-index without re-uploading

```
POST /api/v1/documents/{id}/reprocess   → 202
```

Re-reads the stored original and re-runs the whole pipeline. Verified on the
300-page fixture: `603 chunks → reprocess → 603 chunks`, with **no file in the
request**.

`POST /documents/reindex` still exists and is the cheaper path — it re-embeds
stored chunks without re-parsing, correct when only the embedding model
changed.

---

## 7. Failure keeps the source

The exception handler records the error and marks the job for retry, but never
touches storage. Test `test_a_failed_run_leaves_the_original_stored` injects a
parser crash and asserts the bytes are still readable afterwards.

Retries are automatic up to `max_attempts` (3); because the source is durable,
a retry costs nothing.

---

## 8. Large reports

- Uploads streamed in 1 MB blocks, never held whole in memory
- `DOCUMENT_MAX_UPLOAD_MB` (default 256)
- `DOCUMENT_MIN_FREE_DISK_MB` — refuses at the door rather than dying mid-write
- OCR page cap 400; downloads streamed back in blocks

Measured on a 300-page report: **request 278 ms**, worker 10.9 s, 603 chunks.

---

## 9. Progress and logs

`processing_log` is a JSON column on the document — persisted, not just
emitted to stdout, because "why does this report have no chunks?" is asked
days later, long after the container has gone.

```
queued        0%  stored 206,602 bytes at documents/…/73c09664….pdf
parse         5%  reading stored source
parse         9%  parse finished in 849 ms
ocr          45%  ocr finished in 0 ms
…
done        100%  completed: 603 chunks, 3 fields, 3 entities
```

Capped at 200 entries so a retry loop cannot grow the row without bound.

---

## 10. Migration and API changes

Migration `76d7c501666f` adds `storage_key`, `storage_backend`,
`storage_location`, `processing_log`, `attempts`, indexes `storage_key`, and
migrates `ready → completed`. Verified on SQLite and round-trips through
`downgrade`.

| Endpoint | Change |
|---|---|
| `POST /documents/upload` | 201 → **202**; streams; returns `job_id`, `status_url` |
| `GET /documents/{id}/status` | **new** — status, percent, counters, log |
| `POST /documents/{id}/reprocess` | **new** — re-run from stored source |
| `GET /documents/{id}/source` | **new** — stream the original back |

---

## Deployment

A Railway Volume **must** be mounted at `DOCUMENT_STORAGE_PATH`. Without one
the container filesystem is ephemeral and every uploaded PDF is lost on the
next deploy — the exact failure this redesign exists to prevent.

```bash
# Railway: attach a Volume to the API service, mount path /data/documents
DOCUMENT_STORAGE_BACKEND=local
DOCUMENT_STORAGE_PATH=/data/documents
DOCUMENT_MAX_UPLOAD_MB=256
WORKER_ENABLED=true
```

In production the service refuses to start if that path is not writable,
rather than silently falling back and losing uploads. Outside production it
falls back to a temp directory with a warning, so CI and developer machines
need no volume.

To scale the worker out, run `python -m app.services.documents.worker` as its
own Railway service against the same database and volume. The claim is a
conditional UPDATE, so any number may run.
