"""Replication integrity tests using only in-memory storage adapters."""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.domain.documents.replication import ReplicationState
from app.models.document import Document
from app.services.documents.replication import ReplicationService
from app.services.documents.storage import StorageError


class MemoryStorage:
    def __init__(self, values=None, *, read_error=None, put_error=None):
        self.values = dict(values or {})
        self.read_error, self.put_error = read_error, put_error
        self.puts = []
    def read(self, key):
        if self.read_error: raise self.read_error
        return self.values[key]
    def put(self, key, data):
        if self.put_error: raise self.put_error
        self.values[key] = data; self.puts.append((key, data))


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close(); engine.dispose()


def document(db, payload=b"filing", key="docs/a.pdf", digest=True):
    row = Document(company_id="company", filename="a.pdf", doc_type="annual_report", file_format="pdf", content_hash=hashlib.sha256(payload).hexdigest() if digest else "", storage_key=key, size_bytes=len(payload))
    db.add(row); db.commit(); return row


def test_replicate_one_verifies_written_bytes(db):
    payload = b"verified filing"; doc = document(db, payload)
    secondary = MemoryStorage()
    outcome = ReplicationService(db, primary=MemoryStorage({doc.storage_key: payload}), secondary=secondary).replicate_one(doc)
    assert outcome.state is ReplicationState.VERIFIED
    assert secondary.values[doc.storage_key] == payload


def test_replicate_one_skips_missing_primary_and_missing_key(db):
    doc = document(db, b"x")
    service = ReplicationService(db, primary=MemoryStorage(read_error=StorageError("gone")), secondary=MemoryStorage())
    assert service.replicate_one(doc).state is ReplicationState.SKIPPED
    doc.storage_key = None
    assert service.replicate_one(doc).state is ReplicationState.SKIPPED


def test_replicate_one_handles_no_secondary_write_failure_and_checksum_mismatch(db):
    doc = document(db, b"expected")
    no_target = ReplicationService(db, primary=MemoryStorage({doc.storage_key: b"expected"}), secondary=None)
    assert no_target.replicate_one(doc).state is ReplicationState.PENDING
    failed = ReplicationService(db, primary=MemoryStorage({doc.storage_key: b"expected"}), secondary=MemoryStorage(put_error=StorageError("down")))
    assert failed.replicate_one(doc).state is ReplicationState.FAILED
    corrupt = MemoryStorage(); corrupt.put = lambda key, data: corrupt.values.__setitem__(key, b"corrupt")
    mismatch = ReplicationService(db, primary=MemoryStorage({doc.storage_key: b"expected"}), secondary=corrupt).replicate_one(doc)
    assert mismatch.state is ReplicationState.MISMATCH


def test_read_document_uses_primary_then_verified_fallback_and_rejects_corruption(db):
    payload = b"source"; doc = document(db, payload)
    assert ReplicationService(db, primary=MemoryStorage({doc.storage_key: payload}), secondary=MemoryStorage()).read_document(doc) == payload
    fallback = ReplicationService(db, primary=MemoryStorage(read_error=StorageError("volume lost")), secondary=MemoryStorage({doc.storage_key: payload}))
    assert fallback.read_document(doc) == payload
    broken = ReplicationService(db, primary=MemoryStorage(read_error=StorageError("volume lost")), secondary=MemoryStorage({doc.storage_key: b"bad"}))
    with pytest.raises(StorageError): broken.read_document(doc)
    both = ReplicationService(db, primary=MemoryStorage(read_error=StorageError("volume lost")), secondary=MemoryStorage(read_error=StorageError("bucket lost")))
    with pytest.raises(StorageError): both.read_document(doc)


def test_run_counts_verified_mismatch_and_limit(db):
    first = document(db, b"one", "docs/one")
    second = document(db, b"two", "docs/two")
    secondary = MemoryStorage()
    service = ReplicationService(db, primary=MemoryStorage({"docs/one": b"one", "docs/two": b"two"}), secondary=secondary)
    limited = service.run(limit=1)
    assert limited.attempted == limited.verified == 1
    remaining = service.run(limit=10)
    assert remaining.attempted == remaining.verified == 1


def test_run_disabled_and_health_helpers(db):
    doc = document(db, b"one")
    disabled = ReplicationService(db, primary=MemoryStorage({doc.storage_key: b"one"}), secondary=None)
    assert disabled.run().attempted == 0
    active = ReplicationService(db, primary=MemoryStorage({doc.storage_key: b"one"}), secondary=MemoryStorage())
    active.replicate_one(doc); db.commit()
    assert active.counts()["total_documents"] == 1
    assert active.replicated_bytes() == 3
    assert active.last_success() is not None
    assert active.clean_since() is not None
