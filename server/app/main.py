import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db, init_db
from .indexer import index_paths, scan_roots
from .models import File, Location, StorageRoot, VerificationEvent
from .scheduler import start_scheduler, stop_scheduler
from .watcher import start_watcher, stop_watcher
from .schemas import (
    BatchVerifyRequest,
    BatchVerifyResult,
    ContentDetail,
    HealthResponse,
    JobStatus,
    ReindexResponse,
    StatsResponse,
    VerifyPathRequest,
    VerifyRequest,
    VerifyResult,
)

_jobs: dict[str, JobStatus] = {}

api_key_header = APIKeyHeader(name="X-API-Token", auto_error=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _ensure_storage_roots()
    start_watcher(settings.storage_root_list)
    start_scheduler()
    yield
    stop_watcher()
    stop_scheduler()


def _ensure_storage_roots():
    from .database import SessionLocal
    db = SessionLocal()
    try:
        for root in settings.storage_root_list:
            if not db.query(StorageRoot).filter_by(path=root).first():
                db.add(StorageRoot(path=root, added_at=datetime.now(timezone.utc).isoformat()))
        db.commit()
    finally:
        db.close()


app = FastAPI(title="Flashpoint", version="0.1.0", lifespan=lifespan)


def require_auth(key: str | None = Depends(api_key_header)) -> None:
    if key != settings.api_token:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_verify_result(blake3: str, client_size: int, db: Session, client_host: str | None) -> VerifyResult:
    file_row = db.get(File, blake3)

    if not file_row:
        result = "not_safe"
        message = "Hash not found in index"
        locations = []
        size_match = False
        count = 0
    else:
        present = [loc for loc in file_row.locations if loc.status == "present"]
        size_match = file_row.file_size == client_size
        locations = [loc.file_path for loc in present]
        count = len(present)

        if not size_match:
            result = "not_safe"
            message = "Size mismatch"
        elif file_row.verify_status != "verified":
            result = "exists_unverified"
            message = "File exists but has not been verified yet"
        elif count == 0:
            result = "not_safe"
            message = "No present verified copy found"
        else:
            result = "safe"
            message = f"{count} verified copy(s) on NAS"

    db.add(VerificationEvent(
        blake3_hash=blake3,
        queried_at=_now(),
        result=result,
        client_host=client_host,
    ))
    db.commit()

    return VerifyResult(
        blake3=blake3,
        result=result,
        verified_copy_count=count,
        locations=locations,
        size_match=size_match,
        message=message,
    )


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(status="ok")


@app.post("/v1/verify", response_model=VerifyResult, dependencies=[Depends(require_auth)])
def verify(req: VerifyRequest, request: Request, db: Session = Depends(get_db)):
    return _build_verify_result(req.blake3, req.size, db, request.client.host)


@app.post("/v1/verify/batch", response_model=BatchVerifyResult, dependencies=[Depends(require_auth)])
def verify_batch(req: BatchVerifyRequest, request: Request, db: Session = Depends(get_db)):
    results = [_build_verify_result(f.blake3, f.size, db, request.client.host) for f in req.files]
    return BatchVerifyResult(results=results)


@app.post("/v1/verify/path", response_model=VerifyResult, dependencies=[Depends(require_auth)])
def verify_path(req: VerifyPathRequest, request: Request, db: Session = Depends(get_db)):
    from pathlib import Path
    from .hasher import hash_file

    p = Path(req.path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Path not found on NAS")

    digest = hash_file(p)
    size = p.stat().st_size
    now = _now()

    file_row = db.get(File, digest)
    if not file_row:
        file_row = File(blake3_hash=digest, file_size=size, first_seen=now, last_verified=now, verify_status="verified")
        db.add(file_row)
    else:
        file_row.last_verified = now
        file_row.verify_status = "verified"

    loc = db.query(Location).filter_by(file_path=str(p)).first()
    if not loc:
        loc = Location(blake3_hash=digest, file_path=str(p), last_seen=now, status="present")
        db.add(loc)
    else:
        loc.status = "present"
        loc.last_seen = now

    db.commit()
    return _build_verify_result(digest, size, db, request.client.host)


@app.post("/v1/admin/reindex", response_model=ReindexResponse, dependencies=[Depends(require_auth)])
def trigger_reindex(db: Session = Depends(get_db)):
    job_id = str(uuid.uuid4())
    _jobs[job_id] = JobStatus(job_id=job_id, status="running")

    def _run():
        from .database import SessionLocal
        _db = SessionLocal()
        try:
            r = scan_roots(settings.storage_root_list, _db)
            _jobs[job_id] = JobStatus(
                job_id=job_id, status="done",
                added=r.added, updated=r.updated, removed=r.removed, errors=r.errors,
            )
        except Exception as e:
            _jobs[job_id] = JobStatus(job_id=job_id, status="error", message=str(e))
        finally:
            _db.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return ReindexResponse(job_id=job_id, message="Reindex started")


@app.get("/v1/jobs/{job_id}", response_model=JobStatus, dependencies=[Depends(require_auth)])
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/v1/content/{blake3_hash}", response_model=ContentDetail, dependencies=[Depends(require_auth)])
def get_content(blake3_hash: str, db: Session = Depends(get_db)):
    file_row = db.get(File, blake3_hash)
    if not file_row:
        raise HTTPException(status_code=404, detail="Hash not found")
    return ContentDetail(
        blake3=file_row.blake3_hash,
        file_size=file_row.file_size,
        first_seen=file_row.first_seen,
        last_verified=file_row.last_verified,
        verify_status=file_row.verify_status,
        locations=[loc.file_path for loc in file_row.locations if loc.status == "present"],
    )


@app.get("/v1/stats", response_model=StatsResponse, dependencies=[Depends(require_auth)])
def get_stats(db: Session = Depends(get_db)):
    total_files = db.query(File).count()
    total_locs = db.query(Location).count()
    verified = db.query(File).filter_by(verify_status="verified").count()
    missing = db.query(Location).filter_by(status="missing").count()
    roots = [r.path for r in db.query(StorageRoot).all()]
    return StatsResponse(
        total_unique_files=total_files,
        total_locations=total_locs,
        verified_count=verified,
        missing_count=missing,
        storage_roots=roots,
    )


def main():
    import uvicorn
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
