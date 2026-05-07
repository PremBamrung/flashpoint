import tempfile
from pathlib import Path

from app.hasher import hash_file


def test_hash_file_consistency():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(b"hello flashpoint" * 1000)
        path = Path(f.name)
    try:
        h1 = hash_file(path)
        h2 = hash_file(path)
        assert h1 == h2
        assert len(h1) == 64
    finally:
        path.unlink()


def test_hash_file_changes_with_content():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"aaa")
        p1 = Path(f.name)
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"bbb")
        p2 = Path(f.name)
    try:
        assert hash_file(p1) != hash_file(p2)
    finally:
        p1.unlink()
        p2.unlink()


def test_hash_empty_file():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        p = Path(f.name)
    try:
        h = hash_file(p)
        assert len(h) == 64
    finally:
        p.unlink()
