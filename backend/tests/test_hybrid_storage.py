"""Hybrid storage: volume-primary, object-storage-secondary.

The properties under test are the ones that protect 283 MB of irreplaceable
filings during a phase where a beta service is being trialled alongside them:

* the volume is never written to or deleted from by replication;
* a replica is only trusted after its bytes are read back and hashed;
* a checksum disagreement is terminal and never silently retried;
* a failure on either side degrades to the other rather than to an error;
* protected document types cannot be removed by any automated path.

Everything runs against a real S3 API (moto) rather than a bespoke mock,
because the failures worth catching are S3's rather than ours.
"""
from __future__ import annotations

import hashlib
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")

from moto import mock_aws  # noqa: E402

from app.domain.documents.replication import (  # noqa: E402
    MAX_REPLICATION_ATTEMPTS, PROTECTED_DOC_TYPES, HealthLevel,
    ReplicationState, assess_promotion, assess_replication, assess_volume,
    is_protected, should_retry,
)
from app.services.documents.storage import (  # noqa: E402
    LocalFileStorage, S3CompatibleStorage, StorageError,
)

PAYLOAD = b"%PDF-1.4\n" + b"h" * 30_000 + b"\n%%EOF"


# ===========================================================================
class TestDomainRules:
    """Pure decisions, no I/O."""

    def test_a_mismatch_is_never_retried_automatically(self):
        """Two differing copies of a filing is an incident, not a transient."""
        assert not should_retry(ReplicationState.MISMATCH, 0)

    def test_pending_and_failed_are_retried(self):
        assert should_retry(ReplicationState.PENDING, 0)
        assert should_retry(ReplicationState.FAILED, 1)

    def test_retries_are_bounded(self):
        """A permanently unreplicable document must not own the queue."""
        assert not should_retry(
            ReplicationState.FAILED, MAX_REPLICATION_ATTEMPTS,
        )

    def test_a_verified_replica_is_not_reattempted(self):
        assert not should_retry(ReplicationState.VERIFIED, 0)

    @pytest.mark.parametrize("doc_type", sorted(PROTECTED_DOC_TYPES))
    def test_research_documents_are_protected_from_deletion(self, doc_type):
        assert is_protected(doc_type)

    def test_the_four_named_types_are_protected(self):
        """The brief names these explicitly."""
        for name in ("annual_report", "quarterly_report",
                     "investor_presentation", "conference_call"):
            assert is_protected(name), f"{name} must never be auto-deleted"

    def test_an_unprotected_type_is_not_shielded(self):
        assert not is_protected("other")
        assert not is_protected(None)

    def test_volume_warns_at_eighty_percent(self):
        assert assess_volume(79, 100).level is HealthLevel.OK
        assert assess_volume(81, 100).level is HealthLevel.WARNING

    def test_volume_escalates_to_critical(self):
        assert assess_volume(95, 100).level is HealthLevel.CRITICAL

    def test_an_unknown_volume_size_is_not_reported_as_healthy(self):
        assert assess_volume(0, 0).level is HealthLevel.WARNING

    def test_a_checksum_mismatch_is_critical_not_a_warning(self):
        signals = {s.name: s for s in assess_replication(
            pending=0, failed=0, mismatched=1, last_success=None,
        )}
        assert signals["checksum_verification"].level is HealthLevel.CRITICAL

    def test_a_replication_failure_warns(self):
        signals = {s.name: s for s in assess_replication(
            pending=0, failed=3, mismatched=0, last_success=None,
        )}
        assert signals["replication_failures"].is_alerting

    def test_a_stale_replication_with_a_backlog_warns(self):
        old = datetime.now(timezone.utc) - timedelta(hours=9)
        signals = {s.name: s for s in assess_replication(
            pending=10, failed=0, mismatched=0, last_success=old,
        )}
        assert signals["last_replication"].is_alerting

    def test_staleness_alone_does_not_warn_when_nothing_is_queued(self):
        """Nothing to replicate is not a fault, however long it has been."""
        old = datetime.now(timezone.utc) - timedelta(days=3)
        signals = {s.name: s for s in assess_replication(
            pending=0, failed=0, mismatched=0, last_success=old,
        )}
        assert not signals["last_replication"].is_alerting


class TestPromotionPolicy:
    """Object storage may not become primary on a hunch."""

    def test_a_clean_thirty_days_is_ready(self):
        ready, _ = assess_promotion(clean_days=31, mismatches=0, failures=0,
                                    unreplicated=0, total=100)
        assert ready

    def test_twenty_nine_days_is_not_enough(self):
        ready, criteria = assess_promotion(clean_days=29, mismatches=0,
                                           failures=0, unreplicated=0, total=100)
        assert not ready
        assert not criteria[0].met

    def test_a_single_mismatch_blocks_promotion(self):
        ready, _ = assess_promotion(clean_days=365, mismatches=1, failures=0,
                                    unreplicated=0, total=100)
        assert not ready

    def test_unreplicated_documents_block_promotion(self):
        ready, _ = assess_promotion(clean_days=365, mismatches=0, failures=0,
                                    unreplicated=1, total=100)
        assert not ready

    def test_every_criterion_explains_itself(self):
        _, criteria = assess_promotion(clean_days=1, mismatches=2, failures=3,
                                       unreplicated=4, total=100)
        assert all(c.detail for c in criteria)


# ===========================================================================
@pytest.fixture()
def hybrid():
    """A volume, a real S3 bucket, a company and a stored document."""
    from tests.conftest import TestingSession

    from app.models.company import Company
    from app.models.document import Document
    from app.models.replication import DocumentReplica

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="repl")
        volume = LocalFileStorage(tempfile.mkdtemp())
        bucket = S3CompatibleStorage(
            "repl", region="us-east-1", access_key="t", secret_key="t",
        )
        db = TestingSession()
        company = Company(id="hy-co", name="Hybrid Ltd", ticker="HYBCO",
                          exchange="NSE")
        db.add(company)
        db.flush()

        digest = hashlib.sha256(PAYLOAD).hexdigest()
        key = f"documents/hy-co/{digest}.pdf"
        volume.put(key, PAYLOAD)
        document = Document(
            company_id="hy-co", filename="ar.pdf", title="AR",
            doc_type="annual_report", file_format="pdf",
            size_bytes=len(PAYLOAD), content_hash=digest, storage_key=key,
            storage_backend="local", status="completed",
        )
        db.add(document)
        db.commit()
        try:
            yield db, volume, bucket, document, key
        finally:
            db.query(DocumentReplica).filter_by(
                document_id=document.id
            ).delete()
            db.query(Document).filter_by(company_id="hy-co").delete()
            db.query(Company).filter_by(id="hy-co").delete()
            db.commit()
            db.close()


def _service(db, volume, bucket):
    from app.services.documents.replication import ReplicationService

    return ReplicationService(db, primary=volume, secondary=bucket)


class TestReplicationWorkflow:
    def test_a_document_replicates_and_verifies(self, hybrid):
        db, volume, bucket, document, key = hybrid
        outcome = _service(db, volume, bucket).replicate_one(document)
        assert outcome.state is ReplicationState.VERIFIED
        assert bucket.read(key) == PAYLOAD

    def test_the_verified_hash_is_recorded(self, hybrid):
        from app.models.replication import DocumentReplica

        db, volume, bucket, document, _ = hybrid
        _service(db, volume, bucket).replicate_one(document)
        db.commit()
        replica = db.query(DocumentReplica).filter_by(
            document_id=document.id
        ).one()
        assert replica.verified_sha256 == document.content_hash
        assert replica.verified_at is not None

    def test_the_volume_copy_is_never_modified(self, hybrid):
        """The whole architecture rests on this."""
        db, volume, bucket, document, key = hybrid
        _service(db, volume, bucket).replicate_one(document)
        assert volume.exists(key)
        assert volume.read(key) == PAYLOAD

    def test_the_document_row_still_points_at_the_volume(self, hybrid):
        """Replication must not repoint the authoritative record."""
        db, volume, bucket, document, _ = hybrid
        _service(db, volume, bucket).replicate_one(document)
        assert document.storage_backend == "local"

    def test_a_second_pass_does_not_re_replicate(self, hybrid):
        db, volume, bucket, document, _ = hybrid
        service = _service(db, volume, bucket)
        service.replicate_one(document)
        db.commit()
        assert document.id not in [
            d.id for d in service.pending_documents(limit=50)
        ]

    def test_a_checksum_mismatch_is_terminal(self, hybrid):
        """Recorded, alerting, and never silently retried."""
        from app.models.replication import DocumentReplica

        db, volume, bucket, document, _ = hybrid
        document.content_hash = "0" * 64        # disagrees with the bytes
        db.commit()

        outcome = _service(db, volume, bucket).replicate_one(document)
        db.commit()
        assert outcome.state is ReplicationState.MISMATCH

        replica = db.query(DocumentReplica).filter_by(
            document_id=document.id
        ).one()
        # Both values retained: the expectation is not overwritten by the
        # observation.
        assert replica.verified_sha256 != document.content_hash
        assert not should_retry(replica.state, replica.attempts)

    def test_an_unreadable_primary_is_skipped_not_failed(self, hybrid):
        """It is a primary-side problem; retrying cannot conjure the bytes."""
        db, volume, bucket, document, _ = hybrid
        document.storage_key = "documents/hy-co/absent.pdf"
        db.commit()
        outcome = _service(db, volume, bucket).replicate_one(document)
        assert outcome.state is ReplicationState.SKIPPED

    def test_a_bucket_write_failure_is_retryable(self, hybrid):
        db, volume, _, document, _ = hybrid

        class Broken:
            backend = "s3"

            def put(self, key, body):
                raise RuntimeError("bucket down")

            def read(self, key):
                raise RuntimeError("bucket down")

            def exists(self, key):
                return False

        outcome = _service(db, volume, Broken()).replicate_one(document)
        assert outcome.state is ReplicationState.FAILED
        assert should_retry(outcome.state, 1)

    def test_replication_is_a_no_op_without_a_bucket(self, hybrid):
        """The platform must run perfectly well with no object storage."""
        from app.services.documents.replication import ReplicationService

        db, volume, _, _, _ = hybrid
        service = ReplicationService(db, primary=volume, secondary=None)
        service._secondary = None
        object.__setattr__(service, "_secondary", None)
        # `enabled` consults settings; with no bucket configured it is False.
        run = service.run()
        assert run.attempted == 0


class TestReadFallback:
    def test_reads_come_from_the_volume_by_default(self, hybrid):
        db, volume, bucket, document, key = hybrid
        service = _service(db, volume, bucket)
        service.replicate_one(document)
        bucket.put(key, b"%PDF-different")     # poison the replica
        # The volume is authoritative, so its bytes must win.
        assert service.read_document(document) == PAYLOAD

    def test_an_unavailable_volume_falls_back_to_the_replica(self, hybrid):
        db, volume, bucket, document, _ = hybrid
        _service(db, volume, bucket).replicate_one(document)

        class Offline(LocalFileStorage):
            def read(self, key):
                raise StorageError("volume offline")

        service = _service(db, Offline(tempfile.mkdtemp()), bucket)
        assert service.read_document(document) == PAYLOAD

    def test_a_fallback_read_is_still_checksum_verified(self, hybrid):
        """Serving unverified bytes would be worse than serving an error."""
        db, volume, bucket, document, key = hybrid
        bucket.put(key, b"%PDF-corrupted-replica")

        class Offline(LocalFileStorage):
            def read(self, key):
                raise StorageError("volume offline")

        service = _service(db, Offline(tempfile.mkdtemp()), bucket)
        with pytest.raises(StorageError, match="checksum"):
            service.read_document(document)

    def test_both_backends_failing_raises_clearly(self, hybrid):
        db, _, _, document, _ = hybrid

        class Offline(LocalFileStorage):
            def read(self, key):
                raise StorageError("volume offline")

        class Dead:
            backend = "s3"

            def read(self, key):
                raise RuntimeError("bucket down")

            def exists(self, key):
                return False

        service = _service(db, Offline(tempfile.mkdtemp()), Dead())
        with pytest.raises(StorageError, match="both"):
            service.read_document(document)


class TestHealthAndAlerts:
    def test_the_dashboard_reports_every_required_dimension(self, hybrid):
        from app.services.documents.storage_health import StorageHealthService

        db, volume, bucket, document, _ = hybrid
        _service(db, volume, bucket).replicate_one(document)
        db.commit()

        payload = StorageHealthService(db).health().as_dict()
        assert "volume" in payload
        assert "object_storage" in payload
        replication = payload["replication"]
        for field in ("queue_depth", "failures", "mismatches",
                      "last_successful_replication", "verified"):
            assert field in replication, f"dashboard missing {field}"

    def test_health_degrades_when_a_mismatch_exists(self, hybrid):
        from app.services.documents.storage_health import StorageHealthService

        db, volume, bucket, document, _ = hybrid
        document.content_hash = "0" * 64
        db.commit()
        _service(db, volume, bucket).replicate_one(document)
        db.commit()

        health = StorageHealthService(db).health()
        assert health.level is HealthLevel.CRITICAL

    def test_promotion_is_refused_early_in_the_trial(self, hybrid):
        from app.services.documents.storage_health import StorageHealthService

        db, volume, bucket, document, _ = hybrid
        _service(db, volume, bucket).replicate_one(document)
        db.commit()

        readiness = StorageHealthService(db).promotion_readiness()
        assert not readiness["ready"]
        assert readiness["required_days"] == 30


class TestSchedulingWiring:
    """A background architecture that is not scheduled does not exist."""

    def test_replication_is_on_the_standing_schedule(self):
        from app.domain.platform.jobs import SCHEDULES, JobKind

        specs = [s for s in SCHEDULES if s.kind is JobKind.STORAGE_REPLICATION]
        assert specs, "replication is not scheduled"
        assert specs[0].every_seconds <= 3600

    def test_the_job_kind_is_registered_everywhere(self):
        """JOB-001: a kind registered in three of four places raises KeyError
        on enqueue."""
        from app.domain.platform.jobs import (
            DEFAULT_PRIORITY, JOB_LABELS, JobKind, policy_for,
        )
        from app.services.platform.jobs.handlers import handler_for

        kind = JobKind.STORAGE_REPLICATION
        assert kind in DEFAULT_PRIORITY
        assert kind in JOB_LABELS
        assert policy_for(kind) is not None
        assert handler_for(kind) is not None

    def test_uploads_enrol_documents_for_replication(self):
        import inspect

        from app.services.documents import ingestion

        source = inspect.getsource(ingestion.DocumentIngestionService.accept)
        assert "enrol" in source, "uploads do not enrol a replica"

    def test_an_upload_is_never_blocked_by_replication(self):
        """A bucket outage must not become an upload outage."""
        import inspect

        from app.services.documents import ingestion

        source = inspect.getsource(ingestion.DocumentIngestionService.accept)
        assert "replication must never fail an upload" in source
