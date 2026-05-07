# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "blake3>=1.0",
#   "click>=8.0",
#   "httpx>=0.27",
# ]
# ///
"""
Flashpoint CLI — verify local files are safely on your NAS before deleting.

Usage:
    uv run python flashpoint.py check ./photos/
    uv run python flashpoint.py report ./sd-card/
    uv run python flashpoint.py hash ./photo.jpg
    uv run python flashpoint.py ping
"""

import sqlite3
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import blake3
import click
import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = Path.home() / ".flashpoint" / "config.toml"
DEFAULT_CACHE_DB = Path.home() / ".flashpoint" / "cache.db"

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB


def load_config(config_path: Path) -> dict:
    if config_path.exists():
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    return {}


def get_server_url(cfg: dict) -> str:
    return cfg.get("server", {}).get("url", "")


def get_token(cfg: dict) -> str:
    return cfg.get("server", {}).get("token", "")


def cache_enabled(cfg: dict) -> bool:
    return cfg.get("cache", {}).get("enabled", True)


def cache_db_path(cfg: dict) -> Path:
    p = cfg.get("cache", {}).get("db_path", str(DEFAULT_CACHE_DB))
    return Path(p).expanduser()


# ---------------------------------------------------------------------------
# Local hash cache
# ---------------------------------------------------------------------------

def _open_cache(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hash_cache (
            file_path   TEXT PRIMARY KEY,
            mtime       REAL NOT NULL,
            file_size   INTEGER NOT NULL,
            blake3_hash TEXT NOT NULL,
            computed_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def cache_get(conn: sqlite3.Connection, path: Path) -> str | None:
    stat = path.stat()
    row = conn.execute(
        "SELECT blake3_hash, mtime, file_size FROM hash_cache WHERE file_path = ?",
        (str(path),),
    ).fetchone()
    if row and abs(row[1] - stat.st_mtime) < 1 and row[2] == stat.st_size:
        return row[0]
    return None


def cache_set(conn: sqlite3.Connection, path: Path, digest: str) -> None:
    stat = path.stat()
    conn.execute(
        """INSERT OR REPLACE INTO hash_cache (file_path, mtime, file_size, blake3_hash, computed_at)
           VALUES (?, ?, ?, ?, ?)""",
        (str(path), stat.st_mtime, stat.st_size, digest, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def hash_file(path: Path, cache_conn: sqlite3.Connection | None = None) -> str:
    if cache_conn:
        cached = cache_get(cache_conn, path)
        if cached:
            return cached
    h = blake3.blake3()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            h.update(chunk)
    digest = h.hexdigest()
    if cache_conn:
        cache_set(cache_conn, path, digest)
    return digest


def collect_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(p for p in target.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

def make_client(url: str, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=url,
        headers={"X-API-Token": token},
        timeout=30,
    )


def api_verify(client: httpx.Client, blake3_hash: str, size: int) -> dict:
    resp = client.post("/v1/verify", json={"blake3": blake3_hash, "size": size})
    resp.raise_for_status()
    return resp.json()


def api_verify_batch(client: httpx.Client, files: list[dict]) -> list[dict]:
    resp = client.post("/v1/verify/batch", json={"files": files})
    resp.raise_for_status()
    return resp.json()["results"]


def api_ping(client: httpx.Client) -> dict:
    resp = client.get("/health")
    resp.raise_for_status()
    return resp.json()


def api_verify_path(client: httpx.Client, nas_path: str) -> dict:
    resp = client.post("/v1/verify/path", json={"path": nas_path})
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

RESULT_SYMBOLS = {
    "safe": click.style("✓ safe", fg="green"),
    "not_safe": click.style("✗ not_safe", fg="red", bold=True),
    "exists_unverified": click.style("~ unverified", fg="yellow"),
    "unknown": click.style("? unknown", fg="cyan"),
}


def fmt_result(result: str) -> str:
    return RESULT_SYMBOLS.get(result, result)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.option("--config", default=str(DEFAULT_CONFIG), show_default=True, help="Config file path")
@click.pass_context
def cli(ctx: click.Context, config: str) -> None:
    ctx.ensure_object(dict)
    cfg = load_config(Path(config).expanduser())
    ctx.obj["cfg"] = cfg
    ctx.obj["config_path"] = config


@cli.command()
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def hash(ctx: click.Context, file: Path) -> None:
    """Print BLAKE3 hash of a file."""
    digest = hash_file(file)
    click.echo(f"{digest}  {file}")


@cli.command()
@click.argument("target", type=click.Path(exists=True, path_type=Path))
@click.option("--no-cache", is_flag=True, help="Skip local hash cache")
@click.pass_context
def check(ctx: click.Context, target: Path, no_cache: bool) -> None:
    """Hash local files and check against the NAS."""
    cfg = ctx.obj["cfg"]
    url, token = get_server_url(cfg), get_token(cfg)
    if not url or not token:
        click.echo("Error: server.url and server.token must be set in config.toml", err=True)
        sys.exit(2)

    cache_conn = None
    if not no_cache and cache_enabled(cfg):
        cache_conn = _open_cache(cache_db_path(cfg))

    files = collect_files(target)
    if not files:
        click.echo("No files found.")
        return

    any_unsafe = False
    try:
        with make_client(url, token) as client:
            batch = []
            path_map: list[Path] = []
            for path in files:
                digest = hash_file(path, cache_conn)
                batch.append({"blake3": digest, "size": path.stat().st_size})
                path_map.append(path)

            results = api_verify_batch(client, batch)
            for path, res in zip(path_map, results):
                symbol = fmt_result(res["result"])
                click.echo(f"  {symbol}  {path}")
                if res["result"] != "safe":
                    any_unsafe = True
    except httpx.HTTPError as e:
        click.echo(f"Server error: {e}", err=True)
        sys.exit(2)

    sys.exit(1 if any_unsafe else 0)


@cli.command()
@click.argument("target", type=click.Path(exists=True, path_type=Path))
@click.option("--no-cache", is_flag=True)
@click.pass_context
def report(ctx: click.Context, target: Path, no_cache: bool) -> None:
    """Summarise safe / not-safe / unknown / unverified counts for a folder."""
    cfg = ctx.obj["cfg"]
    url, token = get_server_url(cfg), get_token(cfg)
    if not url or not token:
        click.echo("Error: server.url and server.token must be set in config.toml", err=True)
        sys.exit(2)

    cache_conn = None
    if not no_cache and cache_enabled(cfg):
        cache_conn = _open_cache(cache_db_path(cfg))

    files = collect_files(target)
    if not files:
        click.echo("No files found.")
        return

    counts: dict[str, int] = {"safe": 0, "not_safe": 0, "exists_unverified": 0, "unknown": 0}

    try:
        with make_client(url, token) as client:
            BATCH_SIZE = 500
            for i in range(0, len(files), BATCH_SIZE):
                chunk = files[i : i + BATCH_SIZE]
                batch = [
                    {"blake3": hash_file(p, cache_conn), "size": p.stat().st_size}
                    for p in chunk
                ]
                results = api_verify_batch(client, batch)
                for res in results:
                    counts[res["result"]] = counts.get(res["result"], 0) + 1
    except httpx.HTTPError as e:
        click.echo(f"Server error: {e}", err=True)
        sys.exit(2)

    total = sum(counts.values())
    click.echo(f"\nFlashpoint report — {target}")
    click.echo(f"  Total files      : {total}")
    click.echo(f"  {fmt_result('safe'):<30}: {counts['safe']}")
    click.echo(f"  {fmt_result('not_safe'):<30}: {counts['not_safe']}")
    click.echo(f"  {fmt_result('exists_unverified'):<30}: {counts['exists_unverified']}")
    click.echo(f"  {fmt_result('unknown'):<30}: {counts['unknown']}")

    sys.exit(1 if counts["not_safe"] > 0 or counts["unknown"] > 0 else 0)


@cli.command("verify-remote")
@click.argument("nas_path")
@click.pass_context
def verify_remote(ctx: click.Context, nas_path: str) -> None:
    """Ask the NAS to immediately hash and verify a path."""
    cfg = ctx.obj["cfg"]
    url, token = get_server_url(cfg), get_token(cfg)
    if not url or not token:
        click.echo("Error: server.url and server.token must be set in config.toml", err=True)
        sys.exit(2)

    try:
        with make_client(url, token) as client:
            res = api_verify_path(client, nas_path)
        symbol = fmt_result(res["result"])
        click.echo(f"  {symbol}  {nas_path}")
        click.echo(f"  message: {res['message']}")
        sys.exit(0 if res["result"] == "safe" else 1)
    except httpx.HTTPError as e:
        click.echo(f"Server error: {e}", err=True)
        sys.exit(2)


@cli.command()
@click.pass_context
def ping(ctx: click.Context) -> None:
    """Check server reachability and auth."""
    cfg = ctx.obj["cfg"]
    url, token = get_server_url(cfg), get_token(cfg)
    if not url:
        click.echo("Error: server.url not set in config.toml", err=True)
        sys.exit(2)

    try:
        with make_client(url, token) as client:
            health = api_ping(client)
        click.echo(f"Server OK — {url}  status={health.get('status')}")
        # Validate auth with a known-bad hash
        try:
            client2 = make_client(url, token)
            client2.post("/v1/verify", json={"blake3": "0" * 64, "size": 0})
            click.echo("Auth OK")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                click.echo("Auth FAILED — check your token", err=True)
                sys.exit(1)
    except httpx.ConnectError:
        click.echo(f"Cannot reach server at {url}", err=True)
        sys.exit(2)
    except httpx.HTTPError as e:
        click.echo(f"Server error: {e}", err=True)
        sys.exit(2)


@cli.group()
def cache() -> None:
    """Manage the local hash cache."""


@cache.command("stats")
@click.pass_context
def cache_stats(ctx: click.Context) -> None:
    """Show local cache size."""
    cfg = ctx.obj["cfg"]
    db_path = cache_db_path(cfg)
    if not db_path.exists():
        click.echo("Cache not initialised yet.")
        return
    conn = _open_cache(db_path)
    row = conn.execute("SELECT COUNT(*), SUM(file_size) FROM hash_cache").fetchone()
    count, total_bytes = row
    click.echo(f"Cached entries : {count or 0}")
    click.echo(f"Total file size: {(total_bytes or 0) / 1024 / 1024:.1f} MB")
    click.echo(f"DB path        : {db_path}")


@cache.command("clear")
@click.confirmation_option(prompt="Clear the entire local hash cache?")
@click.pass_context
def cache_clear(ctx: click.Context) -> None:
    """Wipe the local hash cache."""
    cfg = ctx.obj["cfg"]
    db_path = cache_db_path(cfg)
    if db_path.exists():
        conn = _open_cache(db_path)
        conn.execute("DELETE FROM hash_cache")
        conn.commit()
        click.echo("Cache cleared.")
    else:
        click.echo("No cache found.")


if __name__ == "__main__":
    cli()
