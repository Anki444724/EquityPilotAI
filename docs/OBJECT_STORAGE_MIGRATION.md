# Object Storage Migration — Status and Procedure

**Status: blocked on Railway. Code complete and tested; the bucket cannot be
provisioned from the API.**

Nothing in production was changed. The volume remains authoritative and all
127 documents are intact.

## What blocks it

`bucketCreate` succeeds and returns a bucket record, but **no bucket instance
is ever provisioned for the environment**, so no S3 credentials can be issued:

```
bucketCreate            -> id 7b74c7ae-8f16-4f42-a3f8-e686ebcbc4cd  ✓
bucketS3Credentials     -> "BucketInstance not found"   (still, 20+ min later)
bucketInstanceDetails   -> "BucketInstance not found"
```

Tried both with and without `environmentId`; both buckets behave identically.
There is no `bucketDelete` mutation, so the two records remain (the spare is
renamed `unused-delete-me`).

**Cause: Railway Object Storage is in "Priority Boarding", their opt-in beta.**
The changelog states buckets are *"available in Priority Boarding for users on
the Hobby or Pro plans"*. The API exposes the mutations to everyone, but the
provisioning backend only serves enrolled workspaces — which is why creation
succeeds and instancing silently never happens.

Your plan is **not** the obstacle. Confirmed from your own account and the docs:

| | Value |
|---|---|
| Plan | `HOBBY` |
| Bucket storage cap | **1 TB** (docs: "Hobby Plan has a combined maximum storage capacity of 1TB") |
| Bucket pricing | $0.015 per GB-month; S3 operations and egress free |
| Volume cap (`maxSizeMB`) | 500 MB — the reason we are doing this at all |

### What you need to do

Enrol the workspace in **Priority Boarding** (Railway dashboard → account
settings → feature previews), then create the bucket from the project canvas:
right-click → **Bucket**. Railway's own guidance is *"right-click in your
project canvas and choose the Bucket option"* — the dashboard path provisions
the instance, the API path evidently does not.

Once a bucket shows credentials, everything below is ready to run.

## What is built and proven

| Piece | State |
|---|---|
| `S3CompatibleStorage` | Already existed; now verified against a real S3 API |
| `boto3` | Added to `requirements.txt` (was imported lazily and undeclared) |
| `StorageMigrator` | New — `app/services/documents/migrate_storage.py` |
| Migration CLI + benchmark | New — `deploy/migrate_to_bucket.py` |
| Tests | 19 new, all passing against `moto` |

Verified behaviours: byte-identical round trip, per-company key isolation,
missing object raises rather than returning empty, file-object and bytes
inputs, dry run writes nothing, source never deleted, rows repointed to
`s3://`, storage key unchanged, idempotent re-run, unreadable source reported,
checksum mismatch fails that document without abandoning the batch.

Local benchmark against moto (in-process, so these measure the code path, not
Railway's network):

```
   size     upload   download   up MB/s  down MB/s
0.06MB      5.2ms      5.6ms     11.78      10.93
0.95MB      7.0ms      5.7ms    136.61     167.69
```

Real figures must be taken from Railway after provisioning —
`deploy/migrate_to_bucket.py --benchmark-only` prints them.

## Procedure once the bucket exists

**1. Read the credentials** (never printed; written chmod 600 outside the repo):

```bash
python3 - <<'PY'
import sys; sys.path.insert(0,'/tmp'); from rw import gql
r=gql('''query($b:String!,$e:String!,$p:String!){
  bucketS3Credentials(bucketId:$b,environmentId:$e,projectId:$p){
    bucketName endpoint region accessKeyId secretAccessKey } }''',
  {"b":"<bucketId>","e":"10393811-a8ac-46bb-a488-2a798a27cb98",
   "p":"86f8e0b5-8db8-4a3f-84e9-df161295f2c6"})
PY
```

**2. Set the Railway variables on the backend service** — note
`DOCUMENT_STORAGE_BACKEND` stays `local` for now:

```
DOCUMENT_S3_BUCKET, DOCUMENT_S3_ENDPOINT, DOCUMENT_S3_REGION,
DOCUMENT_S3_ACCESS_KEY, DOCUMENT_S3_SECRET_KEY
```

**3. Dry run, then migrate.** The volume stays authoritative throughout:

```bash
python3 deploy/migrate_to_bucket.py --dry-run
python3 deploy/migrate_to_bucket.py
```

The run must report `ok: true`. Any `missing_source` means the volume lost an
object and must be investigated before proceeding.

**4. Only then flip the backend** and redeploy:

```
DOCUMENT_STORAGE_BACKEND=s3
```

This ordering matters: migrating first means a failed cutover is a one-variable
rollback, because every object still exists on the volume.

**5. Verify** — upload, download, reprocess, RAG, AI citations, restart
persistence — then re-run `verify_deployment.py` and `verify_live_modules.py`.

## The volume afterwards

Keep it mounted at `/data/documents`. Once `DOCUMENT_STORAGE_BACKEND=s3`,
`get_storage()` returns the S3 backend and **all new PDFs go to the bucket**;
the volume then holds only OCR scratch space and the migrated copies, which are
the rollback. Do not delete those copies until the bucket has been the live
backend through at least one restart and a spot-check of several documents.

## Caveats

- **Nothing has been migrated and no production configuration was changed.**
  `DOCUMENT_STORAGE_BACKEND` is still `local`.
- Benchmark numbers above are from an in-process S3 mock and are **not**
  representative of Railway's network. Real numbers require the bucket.
- Railway Object Storage is a beta wrapping Wasabi (per Railway's own engineer
  on Hacker News). Worth weighing before it becomes the sole home of the
  filing corpus — which is why the migration deliberately leaves the volume
  copies in place.
- Two orphaned bucket records exist in the project and cannot be removed via
  the API; delete them from the dashboard if they are untidy.
