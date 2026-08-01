"""Durable storage for uploaded document bytes.

The original upload used to be discarded once the request finished. Only
derived content — page text, chunks, embeddings — was persisted, which had
three consequences that are unacceptable in production:

* **Re-index was impossible.** Changing the chunker, the OCR settings or the
  embedding model needed the source bytes, and they were gone. The only
  recovery was to ask the user to upload the file again.
* **A failed ingestion lost the document.** A 200 MB annual report that failed
  on page 900 left a row that could never be completed.
* **Nothing could be audited.** A citation pointing at "p.412" could not be
  checked against the page it came from.

This module defines a small storage interface and two implementations. The
interface exists so the deployment target is a configuration decision rather
than a code change: `LocalFileStorage` writes to a Railway Volume (or any
mounted path), and `S3CompatibleStorage` speaks to S3, Cloudflare R2, MinIO or
anything else with an S3 API. Nothing above this module knows which is in use.

Keys are content-addressed:

    documents/<company_id>/<sha256>.<ext>

Content addressing means a byte-identical re-upload writes the same key rather
than a second copy, the key cannot collide, and a corrupted read is detectable
by rehashing. The company prefix keeps a tenant's documents together, which
makes a per-tenant purge a prefix delete rather than a table scan.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

import structlog

log = structlog.get_logger(__name__)

#: Read/write chunk size. Large reports are streamed rather than loaded whole:
#: a 200 MB file must not become 200 MB of resident memory per worker.
STREAM_CHUNK_BYTES = 1024 * 1024


class StorageError(Exception):
    """Raised when the backing store cannot satisfy a request."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    """The result of a successful write."""

    key: str
    size_bytes: int
    content_hash: str
    backend: str
    #: Filesystem path or object URI, recorded so an operator can find the
    #: bytes without reading application code.
    location: str


class DocumentStorage(ABC):
    """Where uploaded bytes live between the request and the worker."""

    #: Short identifier persisted alongside the key, so a database restored
    #: against a different backend reports the mismatch instead of 404ing.
    backend: str = "abstract"

    @abstractmethod
    def put(self, key: str, source: BinaryIO | bytes) -> StoredObject:
        """Store `source` under `key`, overwriting any existing object."""

    @abstractmethod
    def open(self, key: str) -> BinaryIO:
        """Return a readable binary stream. The caller closes it."""

    @abstractmethod
    def read(self, key: str) -> bytes:
        """Return the whole object. Prefer `open` for large files."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Remove the object. True if it existed."""

    @abstractmethod
    def location(self, key: str) -> str:
        """A human-readable pointer for operators and logs."""

    # -------------------------------------------------------------- helpers
    @staticmethod
    def build_key(company_id: str, content_hash: str, filename: str) -> str:
        """Content-addressed key. Extension retained for content sniffing."""
        suffix = Path(filename).suffix.lower()[:12]
        return f"documents/{company_id}/{content_hash}{suffix}"


class LocalFileStorage(DocumentStorage):
    """Files on a mounted filesystem — a Railway Volume in production.

    Writes go to a temporary file in the same directory and are then renamed.
    `os.replace` is atomic on POSIX within one filesystem, so a worker crashing
    mid-write leaves the previous object intact rather than a truncated file
    that would parse as a corrupt PDF.
    """

    backend = "local"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            # A volume mounted onto a path the application user does not own.
            # Raised as a StorageError naming the uid so the cause is obvious
            # from one log line, rather than surfacing as an opaque 500 on
            # every upload.
            raise StorageError(
                f"cannot create {self.root}: permission denied for uid "
                f"{os.getuid()}. The volume is mounted but owned by another "
                f"user — chown the mount point to the application user, or "
                f"point DOCUMENT_STORAGE_PATH at a writable directory."
            ) from exc

        # Existence is not enough: a root-owned mount is readable and
        # traversable but not writable, and the failure would otherwise
        # appear only on the first upload.
        if not os.access(self.root, os.W_OK):
            raise StorageError(
                f"{self.root} is not writable by uid {os.getuid()}. "
                f"The Railway Volume is mounted but owned by another user."
            )

    def _path(self, key: str) -> Path:
        # Refuse traversal. A key is generated internally, but this is the
        # boundary where a crafted filename would otherwise escape the root.
        candidate = (self.root / key).resolve()
        if not str(candidate).startswith(str(self.root) + os.sep):
            raise StorageError(f"refusing key outside the storage root: {key!r}")
        return candidate

    def put(self, key: str, source: BinaryIO | bytes) -> StoredObject:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0

        handle = tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".upload-", suffix=".part", delete=False,
        )
        try:
            with handle as tmp:
                if isinstance(source, (bytes, bytearray)):
                    tmp.write(source)
                    digest.update(source)
                    size = len(source)
                else:
                    while block := source.read(STREAM_CHUNK_BYTES):
                        tmp.write(block)
                        digest.update(block)
                        size += len(block)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(handle.name, target)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise

        log.info("document stored", key=key, size_bytes=size, backend=self.backend)
        return StoredObject(
            key=key, size_bytes=size, content_hash=digest.hexdigest(),
            backend=self.backend, location=str(target),
        )

    def open(self, key: str) -> BinaryIO:
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"object not found: {key}")
        return path.open("rb")

    def read(self, key: str) -> bytes:
        with self.open(key) as handle:
            return handle.read()

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).is_file()
        except StorageError:
            return False

    def delete(self, key: str) -> bool:
        try:
            path = self._path(key)
        except StorageError:
            return False
        if not path.is_file():
            return False
        path.unlink()
        return True

    def location(self, key: str) -> str:
        return str(self._path(key))

    def usage_bytes(self) -> int:
        """Total bytes held. Used by the storage panel and quota checks."""
        return sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())


class S3CompatibleStorage(DocumentStorage):
    """S3, Cloudflare R2, MinIO — anything with an S3 API.

    boto3 is imported lazily and is not a hard dependency: a deployment using
    a volume must not be forced to install an AWS SDK it never calls.
    """

    backend = "s3"

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        try:
            import boto3  # noqa: PLC0415
            from botocore.config import Config  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise StorageError(
                "S3 storage selected but boto3 is not installed. "
                "Add boto3 to requirements.txt or set DOCUMENT_STORAGE_BACKEND=local."
            ) from exc

        self.bucket = bucket
        self._endpoint = endpoint_url

        # Cloudflare R2 is S3-compatible but not S3, and three of its
        # differences bite silently rather than loudly:
        #
        # * **Region must be "auto".** R2 has no regions; sending a real one
        #   produces signature mismatches on some operations.
        # * **Path-style addressing.** Virtual-host style resolves
        #   `bucket.<account>.r2.cloudflarestorage.com`, which does not exist,
        #   so requests fail DNS rather than returning an S3 error.
        # * **No `x-amz-checksum-*` trailers.** Recent botocore releases send
        #   CRC32 checksums by default; R2 rejects the trailer with a 501 or,
        #   worse, silently stores a corrupted object on some paths. Both are
        #   pinned off here — the platform verifies with SHA256 by read-back,
        #   which is strictly stronger than a transport-level CRC.
        config_kwargs: dict[str, object] = {
            "signature_version": "s3v4",
            "s3": {"addressing_style": "path"},
            "retries": {"max_attempts": 3, "mode": "standard"},
        }
        try:
            # botocore >= 1.36 only. Guarded so an older pin does not break
            # start-up on a TypeError for an unknown Config field.
            self._config = Config(
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                **config_kwargs,
            )
        except TypeError:  # pragma: no cover - depends on the installed pin
            self._config = Config(**config_kwargs)

        self._client = boto3.client(
            "s3", endpoint_url=endpoint_url,
            region_name=(region or "auto"),
            aws_access_key_id=access_key, aws_secret_access_key=secret_key,
            config=self._config,
        )

    def put(self, key: str, source: BinaryIO | bytes) -> StoredObject:
        digest = hashlib.sha256()
        if isinstance(source, (bytes, bytearray)):
            digest.update(source)
            size = len(source)
            body: object = source
        else:
            # Hash while spooling to a temp file so the upload can be retried
            # without the caller having to rewind a possibly-unseekable stream.
            spool = tempfile.TemporaryFile()
            size = 0
            while block := source.read(STREAM_CHUNK_BYTES):
                spool.write(block)
                digest.update(block)
                size += len(block)
            spool.seek(0)
            body = spool

        try:
            self._client.upload_fileobj(  # type: ignore[arg-type]
                body if not isinstance(body, (bytes, bytearray)) else _wrap(body),
                self.bucket, key,
            )
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"S3 upload failed for {key}: {exc}") from exc

        log.info("document stored", key=key, size_bytes=size, backend=self.backend)
        return StoredObject(
            key=key, size_bytes=size, content_hash=digest.hexdigest(),
            backend=self.backend, location=self.location(key),
        )

    def open(self, key: str) -> BinaryIO:
        spool = tempfile.TemporaryFile()
        try:
            self._client.download_fileobj(self.bucket, key, spool)
        except Exception as exc:  # noqa: BLE001
            spool.close()
            raise StorageError(f"object not found: {key} ({exc})") from exc
        spool.seek(0)
        return spool

    def read(self, key: str) -> bytes:
        with self.open(key) as handle:
            return handle.read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def delete(self, key: str) -> bool:
        if not self.exists(key):
            return False
        self._client.delete_object(Bucket=self.bucket, Key=key)
        return True

    def location(self, key: str) -> str:
        if self._endpoint:
            return f"{self._endpoint.rstrip('/')}/{self.bucket}/{key}"
        return f"s3://{self.bucket}/{key}"


def _wrap(payload: bytes) -> BinaryIO:
    import io

    return io.BytesIO(payload)


def iter_stream(handle: BinaryIO, chunk: int = STREAM_CHUNK_BYTES) -> Iterator[bytes]:
    """Yield a stream in blocks, for streaming downloads back to a client."""
    while block := handle.read(chunk):
        yield block


_STORAGE: DocumentStorage | None = None


def get_storage() -> DocumentStorage:
    """The configured backend, built once per process.

    Selected by `DOCUMENT_STORAGE_BACKEND`; everything above this function is
    written against the interface and never learns which one it got.
    """
    global _STORAGE
    if _STORAGE is not None:
        return _STORAGE

    from app.core.config import settings

    backend = (settings.DOCUMENT_STORAGE_BACKEND or "local").strip().lower()
    if backend in {"s3", "r2", "minio"}:
        if not settings.DOCUMENT_S3_BUCKET:
            raise StorageError(
                "DOCUMENT_STORAGE_BACKEND is S3-compatible but "
                "DOCUMENT_S3_BUCKET is unset."
            )
        _STORAGE = S3CompatibleStorage(
            settings.DOCUMENT_S3_BUCKET,
            endpoint_url=settings.DOCUMENT_S3_ENDPOINT,
            region=settings.DOCUMENT_S3_REGION,
            access_key=settings.DOCUMENT_S3_ACCESS_KEY,
            secret_key=settings.DOCUMENT_S3_SECRET_KEY,
        )
    else:
        try:
            _STORAGE = LocalFileStorage(settings.DOCUMENT_STORAGE_PATH)
        except (OSError, PermissionError, StorageError) as exc:
            # The configured volume is not writable. In production that must
            # be loud — uploads cannot be retained and the whole redesign is
            # void — but a developer machine and CI have no /data mount, and
            # refusing to start there helps nobody.
            if settings.is_production:
                raise StorageError(
                    f"document storage path {settings.DOCUMENT_STORAGE_PATH!r} "
                    f"is not writable ({exc}). Attach a volume at that path or "
                    "set DOCUMENT_STORAGE_PATH / DOCUMENT_STORAGE_BACKEND."
                ) from exc
            fallback = Path(tempfile.gettempdir()) / "ierp-documents"
            log.warning(
                "document storage path unavailable; using a temporary "
                "directory. Uploads will not survive a restart.",
                configured=settings.DOCUMENT_STORAGE_PATH,
                fallback=str(fallback), error=str(exc),
            )
            _STORAGE = LocalFileStorage(fallback)

    log.info(
        "document storage ready", backend=_STORAGE.backend,
        location=getattr(_STORAGE, "root", getattr(_STORAGE, "bucket", "?")),
    )
    return _STORAGE


def reset_storage() -> None:
    """Drop the cached backend. Tests point the root at a tmp_path."""
    global _STORAGE
    _STORAGE = None


def free_disk_bytes(path: str | Path) -> int:
    """Bytes available on the volume holding `path`.

    Checked before accepting a large upload: refusing at the door with a clear
    message is better than a worker dying half way through a 200 MB write.
    """
    try:
        usage = shutil.disk_usage(Path(path).expanduser().resolve())
        return usage.free
    except Exception:  # noqa: BLE001
        return -1
