import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from .hasher import hash_file
from .models import File, Location


@dataclass
class ScanResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _needs_rehash(loc: Location, stat: os.stat_result) -> bool:
    """Skip re-hashing if size and mtime are unchanged."""
    stored_size = loc.file.file_size
    stored_mtime = float(loc.last_seen) if loc.last_seen.replace(".", "").isdigit() else 0
    return stat.st_size != stored_size or abs(stat.st_mtime - stored_mtime) > 1


def index_paths(paths: list[str], db: Session) -> ScanResult:
    result = ScanResult()
    for path_str in paths:
        path = Path(path_str)
        if not path.is_file():
            continue
        try:
            _index_one(path, db, result)
        except Exception:
            result.errors += 1
    db.commit()
    return result


def _index_one(path: Path, db: Session, result: ScanResult) -> None:
    stat = path.stat()
    now = _now()

    existing_loc = db.query(Location).filter_by(file_path=str(path)).first()

    if existing_loc:
        file_row = existing_loc.file
        if stat.st_size == file_row.file_size:
            # Cheap path: size unchanged, trust previous hash
            existing_loc.last_seen = now
            existing_loc.status = "present"
            result.updated += 1
            return

    digest = hash_file(path)

    file_row = db.get(File, digest)
    if not file_row:
        file_row = File(
            blake3_hash=digest,
            file_size=stat.st_size,
            first_seen=now,
            last_verified=now,
            verify_status="verified",
        )
        db.add(file_row)
        result.added += 1
    else:
        file_row.last_verified = now
        file_row.verify_status = "verified"

    if existing_loc:
        existing_loc.blake3_hash = digest
        existing_loc.last_seen = now
        existing_loc.status = "present"
        result.updated += 1
    else:
        loc = Location(
            blake3_hash=digest,
            file_path=str(path),
            last_seen=now,
            status="present",
        )
        db.add(loc)
        result.added += 1


def scan_roots(roots: list[str], db: Session) -> ScanResult:
    result = ScanResult()
    seen_paths: set[str] = set()

    for root in roots:
        for dirpath, _, filenames in os.walk(root):
            for fname in filenames:
                full = str(Path(dirpath) / fname)
                seen_paths.add(full)
                path = Path(full)
                try:
                    _index_one(path, db, result)
                except Exception:
                    result.errors += 1

    # Mark paths that disappeared as missing
    present_locs = db.query(Location).filter_by(status="present").all()
    for loc in present_locs:
        if loc.file_path not in seen_paths and any(
            loc.file_path.startswith(r) for r in roots
        ):
            loc.status = "missing"
            result.removed += 1

    db.commit()
    return result
