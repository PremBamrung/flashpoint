from apscheduler.schedulers.background import BackgroundScheduler

from .config import settings
from .database import SessionLocal
from .indexer import scan_roots

_scheduler: BackgroundScheduler | None = None


def _full_rescan() -> None:
    db = SessionLocal()
    try:
        scan_roots(settings.storage_root_list, db)
    finally:
        db.close()


def _integrity_verify() -> None:
    """Re-hash every present file to confirm integrity."""
    from .models import Location
    from .hasher import hash_file
    from datetime import datetime, timezone

    db = SessionLocal()
    try:
        locs = db.query(Location).filter_by(status="present").all()
        for loc in locs:
            try:
                digest = hash_file(loc.file_path)
                now = datetime.now(timezone.utc).isoformat()
                if digest == loc.blake3_hash:
                    loc.file.last_verified = now
                    loc.file.verify_status = "verified"
                else:
                    loc.file.verify_status = "error"
                loc.last_seen = now
            except Exception:
                loc.file.verify_status = "error"
        db.commit()
    finally:
        db.close()


def start_scheduler() -> None:
    global _scheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_full_rescan, "cron", hour=3, minute=0, id="nightly_rescan")
    _scheduler.add_job(_integrity_verify, "cron", day_of_week="sun", hour=2, minute=0, id="weekly_verify")
    _scheduler.start()


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
