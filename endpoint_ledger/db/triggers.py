"""SQL for the probe-version immutability triggers.

Defined here once so the Alembic migration and the test fixture (which uses
``create_all``) recreate identical guarantees. Probe versions must be
immutable after observations reference them.
"""

from __future__ import annotations

GUARD_UPDATE_FUNCTION = """
CREATE OR REPLACE FUNCTION endpoint_ledger_guard_probe_version_update()
RETURNS trigger AS $$
BEGIN
    IF NEW.definition_hash IS DISTINCT FROM OLD.definition_hash
       OR NEW.version_number IS DISTINCT FROM OLD.version_number
       OR NEW.prompt_messages::jsonb IS DISTINCT FROM OLD.prompt_messages::jsonb
       OR NEW.expected_behavior::jsonb IS DISTINCT FROM OLD.expected_behavior::jsonb
       OR NEW.scorers::jsonb IS DISTINCT FROM OLD.scorers::jsonb
    THEN
        IF EXISTS (
            SELECT 1 FROM observations WHERE probe_version_id = OLD.id
        ) THEN
            RAISE EXCEPTION
                'probe_version % is immutable once observations exist', OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

GUARD_DELETE_FUNCTION = """
CREATE OR REPLACE FUNCTION endpoint_ledger_guard_probe_version_delete()
RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM observations WHERE probe_version_id = OLD.id
    ) THEN
        RAISE EXCEPTION
            'probe_version % is immutable once observations exist', OLD.id;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""

TRIGGERS_SQL = (
    GUARD_UPDATE_FUNCTION
    + "\n"
    + GUARD_DELETE_FUNCTION
    + "\n"
    + """
CREATE TRIGGER trg_probe_version_immutable
BEFORE UPDATE ON probe_versions
FOR EACH ROW EXECUTE FUNCTION endpoint_ledger_guard_probe_version_update();
"""
    + "\n"
    + """
CREATE TRIGGER trg_probe_version_no_delete
BEFORE DELETE ON probe_versions
FOR EACH ROW EXECUTE FUNCTION endpoint_ledger_guard_probe_version_delete();
"""
)

DROP_TRIGGERS_SQL = """
DROP TRIGGER IF EXISTS trg_probe_version_no_delete ON probe_versions;
DROP TRIGGER IF EXISTS trg_probe_version_immutable ON probe_versions;
DROP FUNCTION IF EXISTS endpoint_ledger_guard_probe_version_delete();
DROP FUNCTION IF EXISTS endpoint_ledger_guard_probe_version_update();
"""
