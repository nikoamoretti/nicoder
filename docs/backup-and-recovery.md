# Database Backup and Recovery

## Normal backup

```bash
make db-backup
# or: python -m endpoint_ledger.backup create
```

Creates a timestamped, compressed PostgreSQL dump
(`endpoint_ledger_<UTC timestamp>.dump`) in the configured backup directory
(`ENDPOINT_LEDGER_BACKUP_DIR`, default `.backups/`), outside version control.
Files are chmod 600; the directory is chmod 700. Passwords are never printed.

```bash
make db-list-backups          # list available dumps
make db-verify-backup BACKUP=.backups/endpoint_ledger_20260810T120000Z.dump
```

## Backup verification

Verification restores the dump into a **temporary** database
(`endpoint_ledger_verify_<timestamp>`), runs integrity checks (table presence
and row counts for the evidence tables, alembic version), writes a sidecar
`<dump>.verified.json`, and then drops the temporary database. It never touches
the active database.

A backup counts as "recent and verified" for maintenance gating only when it
has a verification sidecar newer than `ENDPOINT_LEDGER_BACKUP_MAX_AGE_HOURS`
(default 24 hours). Destructive maintenance commands (e.g.
`generate-demo-history --execute`) refuse to run when live evidence exists and
no recent verified backup is available.

## Full restoration

```bash
make db-restore BACKUP=.backups/endpoint_ledger_20260810T120000Z.dump
```

Restores the dump into the configured database (`pg_restore --clean
--if-exists --no-owner`), preserving migrations, tables, constraints, indexes,
triggers, and data. Run `python -m endpoint_ledger.cli db upgrade` afterwards
to confirm the alembic version matches head.

## Restoration into a new database

```bash
createdb endpoint_ledger_new
ENDPOINT_LEDGER_DATABASE_URL=postgresql+psycopg://localhost:5432/endpoint_ledger_new \
  python -m endpoint_ledger.backup restore .backups/<dump>
```

## What is and is not included

Included: all tables, data, constraints, indexes, triggers (including the
sealed-evidence and probe-immutability triggers), sequences, and the alembic
version.

Not included: anything outside PostgreSQL — the portable run export bundles in
`evidence_exports/` (archive those separately), `.env` secrets, and the
`local_endpoints/` configuration files.

## How often backups should run during baseline collection

Backup and verify **immediately before** any destructive maintenance, and at
least **once per day** during active baseline collection. The conservative
default gate (`backup_max_age_hours=24`) enforces this: if you begin a
collection day without a verified backup from the previous day, destructive
commands will refuse until you create and verify one.

Recommended rhythm during collection:

1. `make db-backup && make db-verify-backup BACKUP=<latest>` — start of day.
2. Run baseline collection (`run-baseline`).
3. `make db-backup` — end of day.
4. Keep the daily dump until the cohort is qualified.
