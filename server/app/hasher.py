import blake3
from pathlib import Path

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB — N100-friendly


def hash_file(path: str | Path) -> str:
    h = blake3.blake3()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()
