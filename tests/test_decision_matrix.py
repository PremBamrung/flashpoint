"""Tests for the safe-to-delete decision matrix with an in-memory SQLite DB."""
import os
import tempfile

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("API_TOKEN", "test-token")
os.environ.setdefault("STORAGE_ROOTS", "/tmp")

with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as _f:
    os.environ["DB_PATH"] = _f.name

from app.database import Base
from app.models import File, Location, VerificationEvent
from app.main import _build_verify_result


def make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragma(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


BLAKE3 = "a" * 64
SIZE = 1024


def _add_file(db, verify_status="verified", file_size=SIZE):
    db.add(File(blake3_hash=BLAKE3, file_size=file_size, first_seen="2026-01-01T00:00:00+00:00",
                verify_status=verify_status))
    db.commit()


def _add_location(db, status="present"):
    db.add(Location(blake3_hash=BLAKE3, file_path="/mnt/disk1/test.jpg",
                    last_seen="2026-01-01T00:00:00+00:00", status=status))
    db.commit()


def test_safe_when_verified_present_size_match():
    db = make_session()
    _add_file(db, verify_status="verified", file_size=SIZE)
    _add_location(db, status="present")
    res = _build_verify_result(BLAKE3, SIZE, db, None)
    assert res.result == "safe"
    assert res.verified_copy_count == 1
    assert res.size_match is True


def test_not_safe_when_hash_missing():
    db = make_session()
    res = _build_verify_result(BLAKE3, SIZE, db, None)
    assert res.result == "not_safe"


def test_not_safe_when_no_present_location():
    db = make_session()
    _add_file(db, verify_status="verified")
    _add_location(db, status="missing")
    res = _build_verify_result(BLAKE3, SIZE, db, None)
    assert res.result == "not_safe"


def test_exists_unverified_when_status_seen():
    db = make_session()
    _add_file(db, verify_status="seen")
    _add_location(db, status="present")
    res = _build_verify_result(BLAKE3, SIZE, db, None)
    assert res.result == "exists_unverified"


def test_not_safe_when_size_mismatch():
    db = make_session()
    _add_file(db, verify_status="verified", file_size=SIZE)
    _add_location(db, status="present")
    res = _build_verify_result(BLAKE3, SIZE + 1, db, None)
    assert res.result == "not_safe"
    assert res.size_match is False


def test_audit_event_written():
    db = make_session()
    _build_verify_result(BLAKE3, SIZE, db, None)
    events = db.query(VerificationEvent).all()
    assert len(events) == 1
    assert events[0].blake3_hash == BLAKE3
