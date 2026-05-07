from pydantic import BaseModel, Field


class VerifyRequest(BaseModel):
    blake3: str = Field(..., description="BLAKE3 hex digest")
    size: int = Field(..., description="File size in bytes reported by client")


class VerifyResult(BaseModel):
    blake3: str
    result: str  # 'safe' | 'not_safe' | 'exists_unverified' | 'unknown'
    verified_copy_count: int
    locations: list[str]
    size_match: bool
    message: str


class BatchVerifyRequest(BaseModel):
    files: list[VerifyRequest] = Field(..., max_length=500)


class BatchVerifyResult(BaseModel):
    results: list[VerifyResult]


class VerifyPathRequest(BaseModel):
    path: str = Field(..., description="Absolute path on the NAS to verify immediately")


class ReindexResponse(BaseModel):
    job_id: str
    message: str


class JobStatus(BaseModel):
    job_id: str
    status: str  # 'running' | 'done' | 'error'
    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: int = 0
    message: str = ""


class ContentDetail(BaseModel):
    blake3: str
    file_size: int
    first_seen: str
    last_verified: str | None
    verify_status: str
    locations: list[str]


class StatsResponse(BaseModel):
    total_unique_files: int
    total_locations: int
    verified_count: int
    missing_count: int
    storage_roots: list[str]


class HealthResponse(BaseModel):
    status: str
