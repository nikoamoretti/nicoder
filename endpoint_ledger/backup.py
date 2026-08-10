"""PostgreSQL backup and restore workflow.

Uses PostgreSQL-native tooling (pg_dump / pg_restore / createdb / dropdb).
Backups are timestamped, compressed, stored outside version control, and never
echo database passwords.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine.url import make_url

from endpoint_ledger.config import Settings, get_settings

BACKUP_SUFFIX = ".dump"
VERIFY_SUFFIX = ".verified.json"


def _pg_env(database_url: str) -> dict[str, str]:
    """Environment for pg tools derived from the SQLAlchemy URL. No passwords
    are printed anywhere."""
    url = make_url(database_url)
    env = dict(os.environ)
    if url.host:
        env["PGHOST"] = url.host
    if url.port:
        env["PGPORT"] = str(url.port)
    if url.username:
        env["PGUSER"] = url.username
    env["PGDATABASE"] = url.database or "postgres"
    if url.password:
        env["PGPASSWORD"] = url.password
    return env


def backup_dir(settings: Settings) -> Path:
    path = settings.backup_path
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover - filesystem without chmod support
        pass
    return path


def create_backup(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    directory = backup_dir(settings)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"endpoint_ledger_{stamp}{BACKUP_SUFFIX}"
    env = _pg_env(settings.database_url)
    result = subprocess.run(
        ["pg_dump", "--format=custom", "--compress=9", "--file", str(target)],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.strip()}")
    try:
        os.chmod(target, 0o600)
    except OSError:  # pragma: no cover
        pass
    return target


def list_backups(settings: Settings | None = None) -> list[Path]:
    settings = settings or get_settings()
    directory = backup_dir(settings)
    return sorted(directory.glob(f"*{BACKUP_SUFFIX}"), key=lambda p: p.name, reverse=True)


def restore_backup(path: Path, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not path.exists():
        raise FileNotFoundError(f"backup file not found: {path}")
    env = _pg_env(settings.database_url)
    result = subprocess.run(
        ["pg_restore", "--clean", "--if-exists", "--no-owner", "--dbname", env["PGDATABASE"], str(path)],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_restore failed: {result.stderr.strip()}")


def _integrity_checks(temp_db: str, env: dict[str, str]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    from sqlalchemy import create_engine, text

    engine = create_engine(
        f"postgresql+psycopg://{env.get('PGUSER', '')}:{env.get('PGPASSWORD', '')}"
        f"@{env.get('PGHOST', 'localhost')}:{env.get('PGPORT', '5432')}/{temp_db}"
    )
    with engine.connect() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).all()
        }
        checks["tables"] = sorted(existing)
        for table in ("suite_runs", "observations", "scores", "probe_versions", "provider_endpoints"):
            if table in existing:
                checks[table] = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
        if "alembic_version" in existing:
            checks["alembic_version"] = list(
                conn.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
    engine.dispose()
    return checks


def verify_backup(path: Path, settings: Settings | None = None) -> dict[str, Any]:
    """Restore a backup into a temporary verification database, run integrity
    checks, and remove the temporary database afterwards."""
    settings = settings or get_settings()
    if not path.exists():
        raise FileNotFoundError(f"backup file not found: {path}")
    env = _pg_env(settings.database_url)
    temp_db = f"endpoint_ledger_verify_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"

    created = subprocess.run(["createdb", "-T", "template0", temp_db], env=env, capture_output=True, text=True)
    if created.returncode != 0:
        raise RuntimeError(f"createdb failed: {created.stderr.strip()}")

    temp_env = dict(env)
    temp_env["PGDATABASE"] = temp_db
    try:
        restored = subprocess.run(
            ["pg_restore", "--no-owner", "--dbname", temp_db, str(path)],
            env=temp_env,
            capture_output=True,
            text=True,
        )
        if restored.returncode != 0:
            raise RuntimeError(f"verification restore failed: {restored.stderr.strip()}")
        checks = _integrity_checks(temp_db, temp_env)
    finally:
        subprocess.run(["dropdb", temp_db], env=env, capture_output=True, text=True)

    verified_at = datetime.now(UTC).isoformat()
    sidecar = path.with_suffix(path.suffix + VERIFY_SUFFIX)
    sidecar.write_text(json.dumps({"verified_at": verified_at, "checks": checks}, indent=2))
    os.chmod(sidecar, 0o600)
    return {"path": str(path), "verified_at": verified_at, "checks": checks}


def last_verified_backup(settings: Settings | None = None) -> Path | None:
    """The newest backup with a verification sidecar within the max age."""
    settings = settings or get_settings()
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(hours=settings.backup_max_age_hours)
    for backup in list_backups(settings):
        sidecar = backup.with_suffix(backup.suffix + VERIFY_SUFFIX)
        if not sidecar.exists():
            continue
        try:
            verified_at = datetime.fromisoformat(json.loads(sidecar.read_text()).get("verified_at", ""))
        except (ValueError, json.JSONDecodeError):
            continue
        if verified_at >= cutoff:
            return backup
    return None


def _main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"
    settings = get_settings()
    if command == "create":
        target = create_backup(settings)
        print(f"backup created: {target}")
        print("verify with: python -m endpoint_ledger.backup verify <path>")
    elif command == "list":
        for path in list_backups(settings):
            size = path.stat().st_size
            print(f"{path.name}  ({size / 1024:.1f} KiB)")
        if not list_backups(settings):
            print("no backups found")
    elif command == "restore":
        if len(sys.argv) < 3:
            print("usage: python -m endpoint_ledger.backup restore <path>", file=sys.stderr)
            sys.exit(2)
        restore_backup(Path(sys.argv[2]), settings)
        print(f"restored {sys.argv[2]}")
    elif command == "verify":
        if len(sys.argv) < 3:
            print("usage: python -m endpoint_ledger.backup verify <path>", file=sys.stderr)
            sys.exit(2)
        result = verify_backup(Path(sys.argv[2]), settings)
        print(f"verified: {result['path']}")
        print(f"  verified at: {result['verified_at']}")
        print(f"  checks: {result['checks']}")
    else:
        print(__doc__)
        sys.exit(0 if command == "help" else 2)


if __name__ == "__main__":
    _main()
