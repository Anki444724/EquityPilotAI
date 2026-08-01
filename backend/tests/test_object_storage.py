"""Object storage backend and the volume → bucket migration.

Exercised against a real S3 API (moto) rather than a mock of our own, because
the failure modes worth catching are S3's, not ours: a put that succeeds and
stores nothing, a read that returns a truncated object, a key that collides
across companies.

The migration tests encode the properties that make it safe to run against
283 MB of irreplaceable filings: nothing is deleted, every copy is verified by
read-back before the database is repointed, and an interrupted run resumes.
"""
from __future__ import annotations

import hashlib
import tempfile

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from moto import mock_aws  # noqa: E402

from app.services.documents.storage import (  # noqa: E402
    LocalFileStorage, S3CompatibleStorage, StorageError,
)

PDF = b"%PDF-1.4\n" + b"x" * 20_000 + b"\n%%EOF"


@pytest.fixture()
def s3():
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="docs")
        yield S3CompatibleStorage(
            "docs", region="us-east-1", access_key="test", secret_key="test",
        )


class TestS3Backend:
    def test_a_document_round_trips_byte_identically(self, s3):
        stored = s3.put("companies/c1/a.pdf", PDF)
        assert s3.read("companies/c1/a.pdf") == PDF
        assert stored.content_hash == hashlib.sha256(PDF).hexdigest()
        assert stored.size_bytes == len(PDF)

    def test_the_backend_identifies_itself_as_s3(self, s3):
        assert s3.put("k.pdf", PDF).backend == "s3"

    def test_existence_is_reported_accurately(self, s3):
        s3.put("present.pdf", PDF)
        assert s3.exists("present.pdf")
        assert not s3.exists("absent.pdf")

    def test_a_missing_object_raises_rather_than_returning_empty(self, s3):
        """Returning b'' would be ingested as a zero-page document."""
        with pytest.raises(StorageError):
            s3.read("nope.pdf")

    def test_streaming_open_yields_the_whole_object(self, s3):
        s3.put("stream.pdf", PDF)
        with s3.open("stream.pdf") as handle:
            assert handle.read() == PDF

    def test_keys_are_namespaced_per_company(self, s3):
        """The failure that would serve one company's filing to another."""
        a, b = b"%PDF-A", b"%PDF-B"
        s3.put("companies/c1/x.pdf", a)
        s3.put("companies/c2/x.pdf", b)
        assert s3.read("companies/c1/x.pdf") == a
        assert s3.read("companies/c2/x.pdf") == b

    def test_a_file_object_is_accepted_not_only_bytes(self, s3):
        import io

        stored = s3.put("fileobj.pdf", io.BytesIO(PDF))
        assert stored.size_bytes == len(PDF)
        assert s3.read("fileobj.pdf") == PDF


class TestMigration:
    """Volume → bucket, run against production data.

    Every run here is scoped to this fixture's own company. The test session
    is shared across files and `StorageMigrator.pending()` correctly returns
    *all* documents, so an unscoped run picks up rows other tests created and
    reports them as `missing_source`. That was a harness fault, not a product
    one: scanning everything is exactly what a real migration must do.
    """

    @staticmethod
    def _run(db, source, destination, **kwargs):
        """Migrate only the documents this test created."""
        from app.models.document import Document
        from app.services.documents.migrate_storage import StorageMigrator

        migrator = StorageMigrator(db, source, destination, **kwargs)
        mine = list(
            db.query(Document).filter_by(company_id="mig-co")
            .order_by(Document.id).all()
        )
        migrator.pending = lambda limit=None: mine  # noqa: ARG005
        return migrator.run()

    def _fixture(self, db, s3, count=3):
        from app.models.company import Company
        from app.models.document import Document

        local = LocalFileStorage(tempfile.mkdtemp())
        company = Company(id="mig-co", name="Mig Ltd", ticker="MIGCO",
                          exchange="NSE")
        db.add(company)
        db.flush()

        docs = []
        for index in range(count):
            payload = PDF + str(index).encode()
            key = f"companies/mig-co/doc_{index}.pdf"
            local.put(key, payload)
            doc = Document(
                company_id="mig-co", filename=f"doc_{index}.pdf",
                title=f"Doc {index}", doc_type="annual_report",
                file_format="pdf", size_bytes=len(payload),
                content_hash=hashlib.sha256(payload).hexdigest(),
                storage_key=key, storage_backend="local", status="completed",
            )
            db.add(doc)
            docs.append(doc)
        db.commit()
        return local, docs

    @pytest.fixture()
    def db(self):
        from tests.conftest import TestingSession

        from app.models.company import Company
        from app.models.document import Document

        session = TestingSession()
        try:
            yield session
        finally:
            session.query(Document).filter_by(company_id="mig-co").delete()
            session.query(Company).filter_by(id="mig-co").delete()
            session.commit()
            session.close()

    def test_a_dry_run_writes_nothing(self, db, s3):
        from app.services.documents.migrate_storage import StorageMigrator

        local, _ = self._fixture(db, s3)
        report = self._run(db, local, s3, dry_run=True)
        assert report.migrated == 3
        assert not s3.exists("companies/mig-co/doc_0.pdf")

    def test_every_document_is_copied_and_verified(self, db, s3):
        from app.services.documents.migrate_storage import StorageMigrator

        local, docs = self._fixture(db, s3)
        report = self._run(db, local, s3)
        assert report.ok
        assert report.migrated == 3
        for doc in docs:
            assert s3.read(doc.storage_key) == local.read(doc.storage_key)

    def test_the_source_is_never_deleted(self, db, s3):
        """No rollback exists if the original is removed."""
        from app.services.documents.migrate_storage import StorageMigrator

        local, docs = self._fixture(db, s3)
        self._run(db, local, s3)
        for doc in docs:
            assert local.exists(doc.storage_key), "source object was removed"

    def test_rows_are_repointed_to_the_destination(self, db, s3):
        from app.services.documents.migrate_storage import StorageMigrator

        local, docs = self._fixture(db, s3)
        self._run(db, local, s3)
        for doc in docs:
            assert doc.storage_backend == "s3"
            assert doc.storage_location.startswith("s3://")

    def test_the_storage_key_does_not_change(self, db, s3):
        """The key is identical in both backends; only the backend moves."""
        from app.services.documents.migrate_storage import StorageMigrator

        local, docs = self._fixture(db, s3)
        before = [d.storage_key for d in docs]
        self._run(db, local, s3)
        assert [d.storage_key for d in docs] == before

    def test_a_second_run_is_idempotent(self, db, s3):
        """An interrupted migration must be safe to re-run."""
        from app.services.documents.migrate_storage import StorageMigrator

        local, _ = self._fixture(db, s3)
        self._run(db, local, s3)
        second = self._run(db, local, s3)
        assert second.migrated == 0
        assert second.already_present == 3
        assert second.ok

    def test_an_unreadable_source_is_reported_not_swallowed(self, db, s3):
        """A lost volume object must be surfaced before anything is
        decommissioned."""
        from app.models.document import Document
        from app.services.documents.migrate_storage import StorageMigrator

        local, _ = self._fixture(db, s3)
        orphan = Document(
            company_id="mig-co", filename="ghost.pdf", title="Ghost",
            doc_type="other", file_format="pdf", size_bytes=1,
            content_hash="deadbeef", storage_key="companies/mig-co/ghost.pdf",
            storage_backend="local", status="completed",
        )
        db.add(orphan)
        db.commit()

        report = self._run(db, local, s3)
        assert report.missing_source == 1
        assert not report.ok, "a lost object must fail the run"

    def test_a_checksum_mismatch_fails_the_document(self, db, s3):
        """A row whose recorded hash disagrees with the copied bytes must not
        be quietly repointed."""
        from app.services.documents.migrate_storage import StorageMigrator

        local, docs = self._fixture(db, s3)
        docs[0].content_hash = "0" * 64
        db.commit()

        report = self._run(db, local, s3)
        assert report.failed == 1
        assert docs[0].storage_backend == "local", "repointed despite mismatch"

    def test_one_failure_does_not_abandon_the_batch(self, db, s3):
        from app.services.documents.migrate_storage import StorageMigrator

        local, docs = self._fixture(db, s3)
        docs[0].content_hash = "0" * 64
        db.commit()

        report = self._run(db, local, s3)
        assert report.failed == 1
        assert report.migrated == 2

    def test_throughput_is_measured(self, db, s3):
        from app.services.documents.migrate_storage import StorageMigrator

        local, _ = self._fixture(db, s3)
        report = self._run(db, local, s3)
        assert report.bytes_copied > 0
        assert report.throughput_mbps >= 0.0


class TestBackendSelection:
    """`get_storage()` must honour the configured backend."""

    def test_an_s3_backend_without_a_bucket_is_refused(self, monkeypatch):
        """Starting with S3 selected and no bucket would silently fall back to
        a volume that is about to be decommissioned."""
        import app.services.documents.storage as storage_module
        from app.core.config import settings

        monkeypatch.setattr(storage_module, "_STORAGE", None)
        monkeypatch.setattr(settings, "DOCUMENT_STORAGE_BACKEND", "s3")
        monkeypatch.setattr(settings, "DOCUMENT_S3_BUCKET", None)
        with pytest.raises(StorageError, match="DOCUMENT_S3_BUCKET"):
            storage_module.get_storage()

    def test_boto3_is_a_declared_dependency(self):
        """S3CompatibleStorage imports it lazily, so a missing entry in
        requirements.txt only fails in production, on the first upload."""
        import pathlib

        requirements = (
            pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"
        ).read_text()
        assert "boto3" in requirements


class TestCloudflareR2Compatibility:
    """R2 is S3-compatible but not S3, and its differences fail silently.

    Each of these was a real risk when pointing the existing backend at R2:
    a wrong region produces signature mismatches, virtual-host addressing
    resolves a hostname that does not exist, and botocore's default CRC32
    trailers are rejected by R2 on some paths.
    """

    def _client(self, **kwargs):
        return S3CompatibleStorage(
            "b", endpoint_url="https://acct.r2.cloudflarestorage.com",
            access_key="k", secret_key="s", **kwargs,
        )

    def test_path_style_addressing_is_forced(self):
        """Virtual-host style resolves bucket.<account>.r2... which does not
        exist, so requests fail DNS rather than returning an S3 error."""
        with mock_aws():
            storage = self._client(region="auto")
            assert storage._config.s3["addressing_style"] == "path"

    def test_a_missing_region_defaults_to_auto(self):
        """R2 has no regions; a real one causes signature mismatches."""
        with mock_aws():
            storage = self._client()
            assert storage._client.meta.region_name == "auto"

    def test_checksum_trailers_are_not_sent_unless_required(self):
        """R2 rejects x-amz-checksum trailers on some paths. The platform
        verifies with SHA256 by read-back, which is strictly stronger."""
        with mock_aws():
            storage = self._client(region="auto")
            mode = getattr(
                storage._config, "request_checksum_calculation", None,
            )
            if mode is None:
                pytest.skip("botocore too old to express this setting")
            assert mode == "when_required"

    def test_the_endpoint_is_preserved(self):
        with mock_aws():
            assert "r2.cloudflarestorage.com" in self._client()._endpoint


class TestFallbackDirection:
    """REPL-002 — the secondary must be whichever backend the primary is not."""

    def test_a_volume_primary_pairs_with_object_storage(self, monkeypatch):
        from app.core.config import settings
        from app.services.documents.replication import ReplicationService
        from tests.conftest import TestingSession

        monkeypatch.setattr(settings, "DOCUMENT_STORAGE_BACKEND", "local")
        monkeypatch.setattr(settings, "DOCUMENT_S3_BUCKET", "b")
        monkeypatch.setattr(settings, "DOCUMENT_S3_ENDPOINT",
                            "https://acct.r2.cloudflarestorage.com")
        monkeypatch.setattr(settings, "DOCUMENT_S3_ACCESS_KEY", "k")
        monkeypatch.setattr(settings, "DOCUMENT_S3_SECRET_KEY", "s")
        with mock_aws():
            secondary = ReplicationService(TestingSession()).secondary
            assert isinstance(secondary, S3CompatibleStorage)

    def test_an_object_primary_pairs_with_the_volume(self, monkeypatch, tmp_path):
        """Without this the fallback retries the backend that just failed."""
        from app.core.config import settings
        from app.services.documents.replication import ReplicationService
        from tests.conftest import TestingSession

        monkeypatch.setattr(settings, "DOCUMENT_STORAGE_BACKEND", "r2")
        monkeypatch.setattr(settings, "DOCUMENT_STORAGE_PATH", str(tmp_path))
        monkeypatch.setattr(settings, "DOCUMENT_S3_BUCKET", "b")
        with mock_aws():
            secondary = ReplicationService(TestingSession()).secondary
            assert isinstance(secondary, LocalFileStorage)
