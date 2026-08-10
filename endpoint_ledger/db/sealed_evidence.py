"""SQL for the sealed-live-evidence database protections.

Defined here once so the Alembic migration and the test fixture (which uses
``create_all``) recreate identical guarantees. Once a live run is sealed, its
SuiteRun, Observations, and Scores are append-only: UPDATE and DELETE are
blocked at the database level, including for direct SQL executed with the
normal application role.
"""

from __future__ import annotations

FUNCTIONS_SQL = """
CREATE OR REPLACE FUNCTION el_is_sealed_live_run(run_id integer)
RETURNS boolean AS $$
DECLARE
    origin text;
    state text;
BEGIN
    SELECT data_origin, evidence_state INTO origin, state
    FROM suite_runs WHERE id = run_id;
    RETURN COALESCE(origin, '') = 'live' AND COALESCE(state, 'open') <> 'open';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION el_guard_sealed_suite_run_delete()
RETURNS trigger AS $$
BEGIN
    IF OLD.data_origin = 'live' AND OLD.evidence_state <> 'open' THEN
        RAISE EXCEPTION 'sealed live evidence: suite run % cannot be deleted', OLD.id;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION el_guard_sealed_suite_run_update()
RETURNS trigger AS $$
DECLARE
    changed jsonb;
BEGIN
    IF OLD.data_origin = 'live' AND NEW.data_origin IS DISTINCT FROM OLD.data_origin THEN
        RAISE EXCEPTION 'live evidence: suite run % cannot change data origin', OLD.id;
    END IF;
    IF OLD.data_origin = 'live' AND OLD.evidence_state <> 'open' THEN
        IF OLD.evidence_state IN ('sealed', 'exported', 'export_failed')
           AND NEW.evidence_state = 'open' THEN
            RAISE EXCEPTION 'sealed live evidence: suite run % cannot be re-opened', OLD.id;
        END IF;
        SELECT COALESCE(jsonb_agg(key), '[]'::jsonb) INTO changed FROM (
            SELECT key, value FROM jsonb_each(to_jsonb(OLD))
            EXCEPT ALL
            SELECT key, value FROM jsonb_each(to_jsonb(NEW))
        ) t;
        IF NOT (changed <@ '["evidence_state","sealed_at","evidence_manifest_hash",'
                '"evidence_exported_at","updated_at"]'::jsonb) THEN
            RAISE EXCEPTION 'sealed live evidence: suite run % fields immutable (changed: %)', OLD.id, changed;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION el_guard_sealed_observation_delete()
RETURNS trigger AS $$
BEGIN
    IF el_is_sealed_live_run(OLD.suite_run_id) THEN
        RAISE EXCEPTION 'sealed live evidence: observation % cannot be deleted', OLD.id;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION el_guard_sealed_observation_update()
RETURNS trigger AS $$
BEGIN
    IF el_is_sealed_live_run(OLD.suite_run_id) THEN
        RAISE EXCEPTION 'sealed live evidence: observation % is immutable', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION el_guard_sealed_score_delete()
RETURNS trigger AS $$
DECLARE
    run_id integer;
BEGIN
    SELECT o.suite_run_id INTO run_id FROM observations o WHERE o.id = OLD.observation_id;
    IF el_is_sealed_live_run(run_id) THEN
        RAISE EXCEPTION 'sealed live evidence: score % cannot be deleted', OLD.id;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION el_guard_sealed_score_update()
RETURNS trigger AS $$
DECLARE
    run_id integer;
BEGIN
    SELECT o.suite_run_id INTO run_id FROM observations o WHERE o.id = OLD.observation_id;
    IF el_is_sealed_live_run(run_id) THEN
        RAISE EXCEPTION 'sealed live evidence: score % is immutable', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

TRIGGERS_SQL = """
CREATE TRIGGER trg_sealed_suite_run_delete
BEFORE DELETE ON suite_runs
FOR EACH ROW EXECUTE FUNCTION el_guard_sealed_suite_run_delete();

CREATE TRIGGER trg_sealed_suite_run_update
BEFORE UPDATE ON suite_runs
FOR EACH ROW EXECUTE FUNCTION el_guard_sealed_suite_run_update();

CREATE TRIGGER trg_sealed_observation_delete
BEFORE DELETE ON observations
FOR EACH ROW EXECUTE FUNCTION el_guard_sealed_observation_delete();

CREATE TRIGGER trg_sealed_observation_update
BEFORE UPDATE ON observations
FOR EACH ROW EXECUTE FUNCTION el_guard_sealed_observation_update();

CREATE TRIGGER trg_sealed_score_delete
BEFORE DELETE ON scores
FOR EACH ROW EXECUTE FUNCTION el_guard_sealed_score_delete();

CREATE TRIGGER trg_sealed_score_update
BEFORE UPDATE ON scores
FOR EACH ROW EXECUTE FUNCTION el_guard_sealed_score_update();
"""

ALL_SQL = FUNCTIONS_SQL + "\n" + TRIGGERS_SQL

DROP_SQL = """
DROP TRIGGER IF EXISTS trg_sealed_score_update ON scores;
DROP TRIGGER IF EXISTS trg_sealed_score_delete ON scores;
DROP TRIGGER IF EXISTS trg_sealed_observation_update ON observations;
DROP TRIGGER IF EXISTS trg_sealed_observation_delete ON observations;
DROP TRIGGER IF EXISTS trg_sealed_suite_run_update ON suite_runs;
DROP TRIGGER IF EXISTS trg_sealed_suite_run_delete ON suite_runs;
DROP FUNCTION IF EXISTS el_guard_sealed_score_update();
DROP FUNCTION IF EXISTS el_guard_sealed_score_delete();
DROP FUNCTION IF EXISTS el_guard_sealed_observation_update();
DROP FUNCTION IF EXISTS el_guard_sealed_observation_delete();
DROP FUNCTION IF EXISTS el_guard_sealed_suite_run_update();
DROP FUNCTION IF EXISTS el_guard_sealed_suite_run_delete();
DROP FUNCTION IF EXISTS el_is_sealed_live_run(integer);
"""
