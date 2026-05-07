# HashVault — Content-Hash Verification System

## Context

A solo hobby project for a photographer/videographer who needs mathematical certainty before deleting files. The core problem: 2TB NVMe fills up, SD cards fill up on travel, and wiping them requires absolute confidence that the NAS already has every file. Existing backup tools (restic, borg) solve storage — this project solves the *verification query*: "Does my NAS already have this exact file?"

Stack: FastAPI + SQLite on NAS (Docker on OMV), Python CLI on client machines. BLAKE3 for hashing. Tailscale for network transport + API key for auth.

---

## Architecture Overview

```
[Desktop / Laptop]                    [NAS - DXP2800]
                                       OMV7 / Docker
 hashvault CLI                         hashvault-server
 ┌─────────────────┐  HTTPS/Tailscale  ┌──────────────────────────┐
 │ blake3 hash file│ ─────POST /verify─▶│ FastAPI                  │
 │ local SQLite    │ ◀──────────────── │ SQLite (WAL mode)        │
 │ cache           │   {exists: true}  │ inotify watcher          │
 └─────────────────┘                   │ APScheduler (nightly +   │
                                       │   weekly bit-rot scan)   │
                                       │ mounts HDD-1 + HDD-2 RO │
                                       └──────────────────────────┘
```

---

## Repository Layout

```
hashvault/
├── server/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .env.example
│   └── app/
│       ├── main.py          # FastAPI app, startup/shutdown lifecycle
│       ├── config.py        # Settings via pydantic-settings + .env
│       ├── database.py      # SQLAlchemy engine, session factory, WAL setup
│       ├── models.py        # ORM models
│       ├── schemas.py       # Pydantic request/response schemas
│       ├── hasher.py        # BLAKE3 hashing utility, chunked reads
│       ├── indexer.py       # Scan directories, upsert DB, mark missing
│       ├── watcher.py       # watchdog inotify integration, debounce queue
│       └── scheduler.py     # APScheduler: nightly rescan + weekly verify
├── client/
│   ├── hashvault.py         # Click CLI entrypoint
│   ├── hasher.py            # BLAKE3 hashing (same logic as server)
│   ├── cache.py             # Local SQLite cache (path+mtime → hash)
│   ├── api.py               # httpx client wrapper
│   ├── config.py            # ~/.hashvault/config.toml loader
│   └── requirements.txt
└── README.md
```

---

## Database Schema (Server — SQLite in WAL mode)

Two tables: one for unique content, one for physical locations. Same file on both HDDs = one `files` row, two `locations` rows.

```sql
CREATE TABLE files (
    blake3_hash     TEXT PRIMARY KEY,       -- 64-char hex
    file_size       INTEGER NOT NULL,
    first_seen      DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_verified   DATETIME,
    verify_status   TEXT DEFAULT 'ok'       -- 'ok' | 'corrupted'
);

CREATE TABLE locations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    blake3_hash     TEXT NOT NULL REFERENCES files(blake3_hash) ON DELETE CASCADE,
    file_path       TEXT NOT NULL UNIQUE,
    last_seen       DATETIME DEFAULT CURRENT_TIMESTAMP,
    status          TEXT DEFAULT 'present'  -- 'present' | 'missing'
);

CREATE INDEX idx_locations_hash ON locations(blake3_hash);
```

### Client Cache Schema (local SQLite)

```sql
CREATE TABLE hash_cache (
    file_path    TEXT PRIMARY KEY,
    mtime        REAL NOT NULL,      -- os.stat().st_mtime
    file_size    INTEGER NOT NULL,
    blake3_hash  TEXT NOT NULL,
    computed_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

Cache hit condition: `path matches AND mtime matches AND file_size matches` → skip rehashing.

---

## Server: API Endpoints

All endpoints require `X-API-Key: <token>` header (constant, set in `.env`).

### `POST /verify`
Check a single hash.
```json
// Request
{"hash": "abc123..."}

// Response 200
{
  "exists": true,
  "file_size": 52428800,
  "first_seen": "2025-03-01T14:22:00",
  "last_verified": "2025-03-08T02:00:00",
  "verify_status": "ok",
  "locations": ["/mnt/hdd1/photos/2025/", "/mnt/hdd2/backup/2025/"]
}

// Response 200 (not found)
{"exists": false}
```

### `POST /verify/batch`
Check multiple hashes in one round trip. Client uses this for directory scans.
```json
// Request
{"hashes": ["abc123...", "def456...", "ghi789..."]}

// Response 200
{
  "results": {
    "abc123...": {"exists": true, "locations": [...], ...},
    "def456...": {"exists": false},
    "ghi789...": {"exists": true, ...}
  }
}
```

### `GET /stats`
```json
{
  "total_unique_files": 18432,
  "total_size_bytes": 9876543210,
  "corrupted_count": 0,
  "missing_count": 0,
  "last_full_scan": "2025-03-08T03:00:00",
  "last_verify_run": "2025-03-08T02:00:00"
}
```

### `POST /admin/reindex`
Trigger an immediate full rescan. Returns `{"job_id": "...", "status": "queued"}`.

### `GET /health`
Unauthenticated. Returns `{"status": "ok", "uptime_seconds": 3600}`.

---

## Server: Hashing (`hasher.py`)

```python
import blake3

CHUNK_SIZE = 4 * 1024 * 1024  # 4MB reads — fits N100 L3 cache well

def hash_file(path: str) -> tuple[str, int]:
    """Returns (hex_hash, file_size). Reads in 4MB chunks."""
    h = blake3.blake3()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size
```

Same implementation copy-pasted verbatim into `client/hasher.py`. No shared package needed for a hobby project.

BLAKE3 is natively multithreaded for large inputs — the N100's 4 cores will be utilized automatically on large video files.

---

## Server: Indexer (`indexer.py`)

Core logic used by both the watcher and the scheduler:

```
scan_directory(path):
  for each file in path (recursive, skip hidden, skip .db):
    hash, size = hash_file(file)
    upsert files(blake3_hash, file_size)
    upsert locations(blake3_hash, file_path, last_seen=now, status='present')

mark_missing():
  for each location where last_seen < (now - 25h):
    update status = 'missing'
```

File extensions to index: `.jpg .jpeg .cr3 .cr2 .nef .arw .orf .rw2 .dng .heic .mov .mp4 .m4v` (configurable via `WATCH_EXTENSIONS` env var).

---

## Server: inotify Watcher (`watcher.py`)

Uses `watchdog` library. Watches all configured `WATCH_DIRS`.

**Events handled:** `FileCreatedEvent`, `FileMovedEvent` (covers both `cp` and `mv`).

**Debounce:** events go into an `asyncio.Queue`. A consumer coroutine waits 10 seconds of inactivity before processing the batch — prevents hammering the DB when rsync copies 5000 files.

```python
# Pseudocode
async def event_consumer(queue, db):
    pending = set()
    while True:
        try:
            path = await asyncio.wait_for(queue.get(), timeout=10.0)
            pending.add(path)
        except asyncio.TimeoutError:
            if pending:
                for path in pending:
                    index_single_file(path, db)
                pending.clear()
```

---

## Server: Scheduler (`scheduler.py`)

Uses `APScheduler` with `AsyncIOScheduler`.

| Job | Schedule | What it does |
|---|---|---|
| `full_rescan` | Nightly 3:00 AM | Scans all `WATCH_DIRS`, indexes new files, calls `mark_missing()` |
| `integrity_verify` | Weekly Sunday 2:00 AM | Re-hashes every `files` row, compares to stored hash, sets `verify_status='corrupted'` on mismatch, logs warning |

Integrity verify processes files in batches of 100 with `asyncio.sleep(0)` yields to keep the event loop responsive.

---

## Server: Configuration (`config.py`)

Via `pydantic-settings`, loaded from `.env`:

```
API_KEY=changeme_use_secrets_generate
WATCH_DIRS=/mnt/hdd1,/mnt/hdd2
DB_PATH=/data/hashvault.db
PORT=8000
RESCAN_CRON=0 3 * * *
VERIFY_CRON=0 2 * * 0
WATCH_EXTENSIONS=.jpg,.jpeg,.cr3,.arw,.nef,.dng,.mov,.mp4,.heic
LOG_LEVEL=INFO
```

---

## Docker Setup

### `docker-compose.yml`
```yaml
services:
  hashvault:
    build: .
    restart: unless-stopped
    ports:
      - "8000:8000"          # Tailscale handles external access
    volumes:
      - /mnt/hdd1:/mnt/hdd1:ro   # read-only, NAS HDD-1
      - /mnt/hdd2:/mnt/hdd2:ro   # read-only, NAS HDD-2
      - hashvault_data:/data      # SQLite DB persistence
    env_file: .env

volumes:
  hashvault_data:
```

### `Dockerfile`
```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    inotify-tools && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### `requirements.txt` (server)
```
fastapi>=0.115
uvicorn[standard]>=0.30
sqlalchemy>=2.0
blake3>=0.4
watchdog>=4.0
apscheduler>=3.10
pydantic-settings>=2.0
python-dotenv
```

---

## Client CLI

### Configuration

`~/.hashvault/config.toml`:
```toml
[server]
url = "http://100.x.x.x:8000"   # Tailscale IP of NAS
api_key = "changeme..."

[cache]
db_path = "~/.hashvault/cache.db"
```

### Commands

```bash
# Check single file — most common use case
hashvault check photo.CR3
# SAFE TO DELETE  abc1234...  photo.CR3  (2 copies on NAS)
# NOT ON NAS      def5678...  other.CR3

# Check all files in a directory (recursive)
hashvault check /Volumes/SD_CARD/DCIM/

# Check with summary at end
hashvault check /Volumes/SD_CARD/DCIM/ --summary
# Checked: 342 files
# Safe to delete: 338
# NOT on NAS: 4  ← lists them explicitly
# Exiting with code 1 if any file is NOT confirmed

# Force rehash even if cache hit (e.g. file was modified)
hashvault check photo.CR3 --no-cache

# Just compute and print hash, no server query
hashvault hash photo.CR3

# Cache management
hashvault cache stats
hashvault cache clear

# Test server connection
hashvault ping
```

**Exit codes:** `0` = all files confirmed on NAS. `1` = one or more files NOT confirmed. Makes it scriptable.

### Client internals (`api.py`)

Uses `httpx` with a 30-second timeout (Tailscale over travel wifi can be slow).

For directory scans: collect all hashes first, send one `POST /verify/batch` call per 500 files. Much faster than one request per file.

---

## Implementation Order

1. **`server/app/database.py`** — engine, session, WAL pragma, create_all
2. **`server/app/models.py`** — `File`, `Location` ORM classes
3. **`server/app/hasher.py`** — `hash_file()` utility
4. **`server/app/indexer.py`** — `scan_directory()`, `mark_missing()`
5. **`server/app/schemas.py`** — Pydantic request/response models
6. **`server/app/main.py`** — FastAPI app, `/verify`, `/verify/batch`, `/stats`, `/health`, `/admin/reindex`
7. **`server/app/watcher.py`** — watchdog integration with debounce
8. **`server/app/scheduler.py`** — APScheduler nightly + weekly jobs
9. **`server/app/config.py`** — pydantic-settings
10. **`Dockerfile` + `docker-compose.yml`**
11. **`client/hasher.py`** — same hash_file() as server
12. **`client/cache.py`** — SQLite cache with mtime check
13. **`client/api.py`** — httpx wrapper for /verify and /verify/batch
14. **`client/config.py`** — TOML config loader
15. **`client/hashvault.py`** — Click CLI, all commands

---

## Key Libraries

| Component | Library | Why |
|---|---|---|
| Server framework | `fastapi` + `uvicorn` | Async, fast, auto-docs |
| ORM | `sqlalchemy 2.0` (async) | Clean, type-safe, no overhead |
| Hashing | `blake3` | 3x faster than SHA-256, cryptographic |
| File watching | `watchdog` | Cross-platform inotify wrapper |
| Scheduling | `apscheduler` | In-process, no external Redis/Celery needed |
| Config | `pydantic-settings` | .env + type validation |
| HTTP client | `httpx` | Async, cleaner than requests |
| CLI | `click` | Standard, composable |
| Config file | `tomllib` (stdlib 3.11+) | No extra dep for TOML |

---

## Verification / Testing Plan

### Server
```bash
# Start server locally for dev
cd server && docker compose up

# Run a quick manual index
curl -X POST http://localhost:8000/admin/reindex \
  -H "X-API-Key: changeme"

# Verify a known hash
curl -X POST http://localhost:8000/verify \
  -H "X-API-Key: changeme" \
  -H "Content-Type: application/json" \
  -d '{"hash": "abc123..."}'

# Check stats
curl http://localhost:8000/stats -H "X-API-Key: changeme"
```

### Client
```bash
# Hash a file and manually check it exists on server
hashvault hash test.cr3
hashvault check test.cr3

# Test batch with a known-present and known-absent file
hashvault check present_file.cr3 absent_file.cr3
# Expect exit code 1, absent file listed clearly

# Verify cache is working (second run should be instant)
time hashvault check /large/directory/
time hashvault check /large/directory/   # should be ~10x faster
```

### End-to-end travel workflow test
1. Copy 10 test files to a temp directory representing an SD card
2. Run `hashvault check /tmp/sd_card/` → expect NOT ON NAS for all
3. Copy files to NAS watched directory, wait 15s for inotify debounce
4. Run `hashvault check /tmp/sd_card/` → expect SAFE TO DELETE for all
5. Corrupt one file on NAS, run weekly verify job manually → expect `verify_status='corrupted'` in DB
