# Hash-Verified Media Safety Service

## Summary
Build a greenfield hobby project with two components: a FastAPI + SQLite service running on the NAS, and a Node.js CLI running on the laptop/desktop. The system’s purpose is to answer a narrow, high-trust question: “Does the NAS already hold this exact file, and is it safe for me to manually delete my local source copy?” File identity is based on full-file BLAKE3 hashes of raw bytes, not filenames or paths.

V1 is intentionally verification-first, not a full backup/orchestration platform. The NAS preserves original ingest folder structures, maintains a hash index over stored media, supports scheduled indexing plus immediate on-demand verification, and records one content object with many file instances. The user remains in control of deletion; the system never auto-deletes source files.

## Architecture And Responsibilities
### NAS service
- Run a FastAPI application on the NAS, exposed only over Tailscale.
- Store state in SQLite on the NAS local NVMe.
- Provide a trustable verification API, index job tracking, and lightweight audit history.
- Perform scheduled background scans of configured storage roots.
- Perform on-demand verification of specific paths when the client asks about recently copied files.
- Keep original human-readable storage folders unchanged; the service is an index and verification layer, not a content-addressed file store.

### Local client
- Build a Node.js CLI for laptop/desktop use.
- Compute full-file BLAKE3 locally on source files.
- Call the NAS API to ask whether that content already exists and whether the NAS considers it verified.
- Support targeted commands for single files, folders, and dry-run review of candidate deletions.
- Never delete automatically in v1; only print a clear safety verdict plus evidence.

### Trust boundary
- Tailscale network is required.
- Add a shared API token on top of Tailscale for app-layer protection.
- The API should assume requests come from trusted personal devices, but still authenticate every request.

## Core Data Model
### Content identity
- `content` table keyed by BLAKE3 digest.
- One row per unique byte-identical file content.
- Fields: `blake3`, `size_bytes`, `first_seen_at`, `last_seen_at`, `status`, `verified_copy_count`, `notes`.
- `status` should distinguish at least: `seen`, `verified`, `missing`, `quarantined`, `error`.
- `verified_copy_count` reflects how many currently verified NAS file instances exist for this content.

### File instances
- `file_instance` table for each concrete file path on NAS.
- Fields: stable id, `content_blake3`, absolute path, storage root id, filename, extension, size, mtime, ctime if available, discovered_at, last_verified_at, exists_flag, verification_state.
- This table is the source of truth for “one hash, many file records”.
- Moves and renames create updated path state without changing content identity.

### Storage roots
- `storage_root` table for indexed mount points or top-level media folders.
- Fields: id, label, absolute root path, enabled flag, scan policy, last_scan_at.

### Scan and verification jobs
- `scan_job` table for scheduled full scans and targeted rescans.
- Fields: id, type, scope, requested_at, started_at, finished_at, status, counts, error summary.
- `verification_event` or `query_audit` table for client-facing checks.
- Record local client requests, verdicts returned, and which NAS evidence supported the answer.

### Optional lightweight metadata
- Store core filesystem metadata always.
- Store lightweight media metadata when easy and cheap: EXIF capture datetime for photos if present, video duration/container codec basics if cheaply extractable.
- Media metadata is for audit/search only and must not influence file identity or safe-delete verdicts.

## Verification Semantics
### Primary identity rule
- File identity is the BLAKE3 of the entire file byte stream.
- Do not chunk before hashing for safe-delete decisions.
- Do not include filename, path, timestamps, or media metadata in the identity hash.

### Safe-to-delete rule in v1
- Return `safe_to_delete = true` only when:
  - the submitted BLAKE3 exists in the `content` table
  - at least one current NAS `file_instance` is marked verified and readable
  - the verified instance size matches the source-reported size
- Because the user chose manual deletion, the service should also return a confidence explanation, not just a boolean.

### NAS-side verification meaning
- A file instance is `verified` only after the NAS has itself hashed the stored file and matched it to the indexed content record.
- A file copied to the NAS but not yet NAS-hashed should not count as safe.
- Scheduled scans may establish verification later; on-demand verification may establish it immediately for recently copied files.

### Duplicate handling
- Multiple NAS files may map to one content hash.
- The API should report duplicate count and representative locations.
- Safe-delete approval does not require canonicalization to a single stored path.

## API Surface
### Client query endpoints
- `POST /v1/query/content`
- Input: local file hash, size, optional local path hint, optional filename, optional device id.
- Output: existence verdict, safe-to-delete boolean, verified copy count, representative NAS paths, last verification timestamp, explanatory reason code.

- `POST /v1/query/batch`
- Accept many file summaries for folder/card workflows.
- Return per-file verdicts plus aggregate counts.

### Verification and indexing endpoints
- `POST /v1/verify/path`
- Trigger immediate verification of a specific NAS path or folder after a copy operation.
- Useful when the user has just pushed new data and wants an up-to-date verdict.

- `POST /v1/index/scan`
- Trigger manual scan of one root or subpath.
- Restricted to authenticated trusted devices.

- `GET /v1/jobs/{id}`
- Job status for scans or targeted verifications.

### Inventory endpoints
- `GET /v1/content/{blake3}`
- Return content summary and associated verified file instances.

- `GET /v1/health`
- Liveness/readiness plus database schema version and scan backlog summary.

### API response design
- Standardize explicit reason codes such as `NOT_FOUND`, `FOUND_UNVERIFIED`, `FOUND_VERIFIED`, `SIZE_MISMATCH`, `PATH_MISSING`, `INDEX_STALE`, `INTERNAL_ERROR`.
- Include timestamps in every trust-sensitive response so the CLI can show how fresh the evidence is.

## Indexing Strategy
### Scheduled indexing
- Run periodic full scans over configured roots using a scheduler inside the FastAPI process or a sidecar worker.
- The scan pipeline should enumerate files, gather cheap metadata first, and hash only files that are new, changed, or missing verification.
- Use path + size + mtime as a cheap change detector to avoid needless rehashing on every run.
- If metadata changed but full hash and size are unchanged after verification, update file-instance metadata only.

### On-demand verification
- Support immediate rehash of specific NAS files or folders after ingest.
- This is the main way to avoid waiting for the next scheduled scan when traveling.

### Reconciliation rules
- If an indexed path disappears, mark the file instance missing but keep the content row.
- Recompute `verified_copy_count` from live file instances.
- If a stored file becomes unreadable or size mismatched, downgrade its verification state and exclude it from safe-delete decisions.

## CLI Workflow
### Commands
- `hash` for ad hoc full-file hashing.
- `check <file|folder>` to compute local hashes and ask the NAS for verdicts.
- `report <folder>` to summarize safe, unsafe, pending-verification, and missing files.
- `verify-remote <nas-path>` to request immediate NAS-side verification after an upload.
- `manifest` optional helper to emit a local JSON/CSV manifest for a travel ingest batch.

### UX expectations
- Show one-line verdicts per file plus a final summary.
- Make the distinction between `exists` and `safe_to_delete` visually obvious.
- Print exact reasons when unsafe: not found, not yet verified on NAS, stale index, size mismatch, auth failure, NAS unreachable.
- Support dry-run only in v1; do not include a destructive delete command.

## Implementation Details
### Backend stack
- FastAPI for HTTP API.
- SQLite with SQLAlchemy or SQLModel; enable WAL mode for better concurrent read/write behavior.
- Pydantic models for request/response contracts.
- Background task mechanism: APScheduler or equivalent for periodic scans; keep the first version single-node and simple.
- BLAKE3 hashing via mature Python package.
- Optional lightweight EXIF/container parsing through small focused libraries only if cheap and stable.

### CLI stack
- Node.js TypeScript preferred for maintainability.
- BLAKE3 via a native or WASM-backed package with stable cross-platform support.
- HTTP client with retries and clear timeout handling.
- Local concurrency limits for hashing large folders so the CLI does not saturate laptop thermals or SSD unnecessarily.

### Performance policies
- Full-file hashing is the source of truth; do not add chunk-based dedupe in v1.
- Optimize with parallel hashing and incremental scan heuristics, not with weaker identity semantics.
- For large travel batches, batch API lookups after hashing locally rather than one request per file.

## Failure Modes And Safety Rules
- NAS unreachable: CLI must return `unknown`, never `safe`.
- API auth failure: return `unknown`.
- Index stale or verification pending: return `exists_but_not_safe`.
- Hash match but no currently verified readable NAS file instance: return `not_safe`.
- Local file changed during hashing: detect via stat-before/stat-after and abort verdict for that file.
- SQLite corruption or scan crash: fail closed; never approve deletion from incomplete evidence.

## Test Plan
### Unit tests
- Hashing and identity semantics for exact byte matches, renamed files, different metadata, and changed bytes.
- Safe-delete decision matrix for verified, unverified, missing, stale, and mismatched states.
- Duplicate-content scenarios with one and many file instances.
- Reconciliation when paths disappear or become unreadable.

### Integration tests
- End-to-end flow: local hash -> API query -> not found.
- Upload/copy simulation -> on-demand NAS verification -> safe verdict.
- Batch folder query with mixed safe and unsafe results.
- Scheduled scan discovers new files and updates duplicate counts correctly.
- Authentication and token failure behavior.

### Real-media validation
- JPEG/RAW/MOV/MP4 samples across small and large sizes.
- Files with same content but different names/paths.
- Media with metadata-only edits to confirm they intentionally hash differently.
- Large folder/card ingest simulation over Tailscale-like latency.

### Acceptance criteria
- For any local file, the CLI can produce a deterministic verdict with human-readable evidence.
- The NAS never claims safe unless it has itself verified at least one readable stored copy.
- Renames and path changes on NAS do not break content identity.
- Manual deletion decisions are supported by explicit audit data.

## Assumptions And Defaults
- V1 targets a single-user personal system, not multi-tenant use.
- The NAS is reachable only through the user’s Tailnet.
- One verified NAS copy is sufficient for the app to say “safe to delete,” because the user has chosen manual deletion and separately scheduled HDD backups.
- Original folder structures remain the canonical browsing layout.
- BLAKE3 is the only required identity hash in v1; SHA-256 interoperability is out of scope unless later needed.
- Auto-delete, GUI, full backup orchestration, cloud replication, and content-defined chunk dedupe are intentionally deferred beyond v1.
