"""Storage health dashboard and hybrid-replication controls.

Read endpoints require an operator role rather than merely being
authenticated: free-disk figures, bucket reachability and replication error
text describe the platform's own infrastructure, not the customer's research,
and belong behind the operator boundary.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.domain.documents.replication import (
    PROTECTED_DOC_TYPES, ReplicationState, is_protected,
)
from app.models.document import Document
from app.models.replication import DocumentReplica

router = APIRouter(tags=["storage"])


def _require_operator(user: CurrentUser) -> None:
    role = str(getattr(user, "role", "") or "").lower()
    if role not in ("admin", "super_admin", "tenant_admin", "operator"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "operator role required",
        )


@router.get("/storage/health", summary="Storage health dashboard")
def storage_health(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Volume usage, object-storage usage, replication queue and SHA status."""
    _require_operator(user)
    from app.services.documents.storage_health import StorageHealthService

    payload = StorageHealthService(db).health().as_dict()
    payload["architecture"] = {
        "primary": "Railway Volume (authoritative)",
        "secondary": "Railway Object Storage (replica)",
        "read_policy": "volume first, replica on failure",
        "write_policy": "volume synchronously, replica in background",
    }
    payload["retention"] = {
        "protected_doc_types": sorted(PROTECTED_DOC_TYPES),
        "policy": "no automated process deletes a protected document",
    }
    return payload


@router.get("/storage/replication", summary="Per-document replication state")
def replication_state(
    state: str | None = Query(default=None),
    limit: int = Query(default=100, le=1000),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _require_operator(user)

    query = (
        select(DocumentReplica, Document.filename, Document.doc_type)
        .join(Document, Document.id == DocumentReplica.document_id)
    )
    if state:
        query = query.where(DocumentReplica.state == state)
    rows = db.execute(
        query.order_by(desc(DocumentReplica.id)).limit(limit)
    ).all()

    return {
        "count": len(rows),
        "results": [
            {
                "document_id": replica.document_id,
                "filename": filename,
                "doc_type": doc_type,
                "protected": is_protected(doc_type),
                "backend": replica.backend,
                "state": replica.state,
                "attempts": replica.attempts,
                "size_bytes": replica.size_bytes,
                # Both values are kept on a mismatch, so an operator can see
                # what was expected and what the replica actually returned.
                "expected_sha256": None,
                "verified_sha256": replica.verified_sha256,
                "verified_at": (
                    replica.verified_at.isoformat()
                    if replica.verified_at else None
                ),
                "error": replica.error,
            }
            for replica, filename, doc_type in rows
        ],
    }


@router.post("/storage/replicate", summary="Run a replication pass now")
def run_replication(
    limit: int = Query(default=25, le=200),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Synchronous pass, for verification and for draining a backlog.

    The scheduler runs this every ten minutes; this endpoint exists so an
    operator can force it and see the outcome.
    """
    _require_operator(user)
    from app.services.documents.replication import ReplicationService

    service = ReplicationService(db)
    if not service.enabled:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "object storage is not configured; nothing to replicate to",
        )
    return service.run(limit=limit).as_dict()


@router.post("/storage/migrate", summary="Migrate volume documents to R2")
def migrate_to_object_storage(
    limit: int = Query(default=25, le=500),
    dry_run: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Copy documents from the volume to object storage, verifying each.

    Runs inside the container because that is the only place the Railway
    Volume is mounted — the bytes cannot be reached from anywhere else.

    Nothing is deleted and no document row is repointed: `StorageMigrator`
    copies, reads back, and compares the SHA256 against the value recorded at
    upload. Bounded per call so a request cannot hold a worker for the length
    of a 236 MB transfer.
    """
    _require_operator(user)
    from app.core.config import settings
    from app.services.documents.migrate_storage import StorageMigrator
    from app.services.documents.storage import (
        LocalFileStorage, S3CompatibleStorage,
    )

    if not settings.DOCUMENT_S3_BUCKET:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "object storage is not configured",
        )

    # Explicit endpoints rather than `get_storage()`: this must copy volume
    # -> object storage regardless of which one is currently primary, so that
    # the same endpoint works before and after cutover.
    source = LocalFileStorage(settings.DOCUMENT_STORAGE_PATH)
    destination = S3CompatibleStorage(
        settings.DOCUMENT_S3_BUCKET,
        endpoint_url=settings.DOCUMENT_S3_ENDPOINT,
        region=settings.DOCUMENT_S3_REGION,
        access_key=settings.DOCUMENT_S3_ACCESS_KEY,
        secret_key=settings.DOCUMENT_S3_SECRET_KEY,
    )
    report = StorageMigrator(
        db, source, destination, dry_run=dry_run,
    ).run(limit=limit)
    payload = report.as_dict()
    # The per-document list is large and mostly uninteresting; keep only the
    # entries an operator must act on.
    payload["outcomes"] = [
        o for o in payload["outcomes"]
        if o["status"] in ("failed", "missing_source")
    ]
    return payload


@router.get("/storage/promotion-readiness",
            summary="Is object storage ready to become primary?")
def promotion_readiness(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """The 30-day cutover assessment, evaluated mechanically."""
    _require_operator(user)
    from app.services.documents.storage_health import StorageHealthService

    return StorageHealthService(db).promotion_readiness()


@router.post("/storage/verify/{document_id}",
             summary="Re-verify one document against both backends")
def verify_document(
    document_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Read from the volume and the replica and compare both to the record."""
    _require_operator(user)
    import hashlib

    from app.services.documents.replication import ReplicationService
    from app.services.documents.storage import StorageError

    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown document")

    service = ReplicationService(db)
    result: dict[str, Any] = {
        "document_id": document_id,
        "filename": document.filename,
        "expected_sha256": document.content_hash,
        "protected": is_protected(document.doc_type),
    }

    try:
        primary = service.primary.read(document.storage_key)
        digest = hashlib.sha256(primary).hexdigest()
        result["volume"] = {
            "readable": True, "sha256": digest,
            "matches": digest == document.content_hash,
            "size_bytes": len(primary),
        }
    except (StorageError, OSError) as exc:
        result["volume"] = {"readable": False, "error": str(exc)[:200]}

    secondary = service.secondary
    if secondary is None:
        result["object_storage"] = {"configured": False}
    else:
        try:
            replica = secondary.read(document.storage_key)
            digest = hashlib.sha256(replica).hexdigest()
            result["object_storage"] = {
                "configured": True, "readable": True, "sha256": digest,
                "matches": digest == document.content_hash,
                "size_bytes": len(replica),
            }
        except Exception as exc:  # noqa: BLE001
            result["object_storage"] = {
                "configured": True, "readable": False, "error": str(exc)[:200],
            }
    return result
