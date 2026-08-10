"""Endpoint Ledger command-line interface.

Commands
--------
db upgrade          Apply Alembic migrations.
seed                Seed endpoint configs and probe definitions (idempotent).
list-endpoints      List enabled endpoints.
list-probes         List probes and their immutable versions.
run-suite           Run a suite against mock and/or live providers.
detect-changes      Run conservative change detection and persist events.
generate-demo-history  Generate baseline + persistent-regression history and a
                      change event (mock only, no live calls).
serve               Start the private dashboard.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import typer
from sqlalchemy import select

from endpoint_ledger import __version__
from endpoint_ledger.config import PROJECT_ROOT, get_settings
from endpoint_ledger.db.models import (
    Probe,
    ProviderEndpoint,
    RunAnnotation,
    Score,
    ScoreAdjudication,
    SuiteRun,
)
from endpoint_ledger.db.session import make_engine, make_sessionmaker
from endpoint_ledger.runner.service import CostCeilingExceededError, SuiteOptions, SuiteRunner
from endpoint_ledger.seed import seed_all

app = typer.Typer(help="Endpoint Ledger — test live AI endpoints and detect material changes.")
db_app = typer.Typer(help="Database commands (Alembic).")
app.add_typer(db_app, name="db")


def _session_factory():
    settings = get_settings()
    engine = make_engine(settings.database_url)
    return make_sessionmaker(engine)


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply Alembic migrations to the configured database."""
    import alembic.command
    import alembic.config

    cfg = alembic.config.Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    alembic.command.upgrade(cfg, "head")
    typer.echo("database migrated")


@app.command()
def seed() -> None:
    """Seed endpoint configurations and probe definitions (idempotent)."""
    session_factory = _session_factory()
    with session_factory() as session:
        result = seed_all(session)
        typer.echo(f"seeded {len(result['endpoints'])} endpoints, {len(result['probe_versions'])} probe versions")


@app.command("list-endpoints")
def list_endpoints() -> None:
    """List configured endpoints."""
    session_factory = _session_factory()
    with session_factory() as session:
        endpoints = session.scalars(select(ProviderEndpoint).order_by(ProviderEndpoint.id)).all()
        for ep in endpoints:
            pricing = "priced" if ep.input_cost_per_million_tokens is not None else "pricing unknown"
            typer.echo(
                f"[{ep.id}] {ep.display_name}  provider={ep.provider} model={ep.model_requested} "
                f"enabled={ep.enabled} {pricing}"
            )
        if not endpoints:
            typer.echo("no endpoints; run `python -m endpoint_ledger.cli seed`")


@app.command("list-probes")
def list_probes() -> None:
    """List probes and their immutable versions."""
    session_factory = _session_factory()
    with session_factory() as session:
        probes = session.scalars(select(Probe).order_by(Probe.stable_key)).all()
        for probe in probes:
            for version in probe.versions:
                obs_count = len(version.observations)
                typer.echo(
                    f"[{probe.id}] {probe.stable_key} v{version.version_number} "
                    f"category={probe.category} hash={version.definition_hash[:10]} "
                    f"observations={obs_count}"
                )
        if not probes:
            typer.echo("no probes; run `python -m endpoint_ledger.cli seed`")


@app.command("run-suite")
def run_suite(
    suite: str = typer.Option("smoke", help="Suite name (probe directory)."),
    provider: str | None = typer.Option(None, help="Restrict to a provider (mock, openai)."),
    endpoint: str | None = typer.Option(None, help="Restrict by display name or model."),
    repeats: int = typer.Option(3, min=1, help="Repeats per probe x endpoint."),
    delay: float = typer.Option(0.0, min=0.0, help="Delay in seconds between calls."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the planned matrix, call nothing."),
    live: bool = typer.Option(False, "--live", help="Allow live provider calls (required for openai)."),
    cost_ceiling: float | None = typer.Option(
        None, "--cost-ceiling", help="Refuse to start when estimated cost exceeds this USD value."
    ),
    probes_dir: str | None = typer.Option(None, "--probes-dir", help="Override probe directory."),
    data_origin: str | None = typer.Option(
        None, "--data-origin",
        help="Override data origin: live, mock, synthetic, demo, test (default: derived).",
    ),
) -> None:
    """Run a suite (endpoint x probe x repeat matrix)."""
    session_factory = _session_factory()
    with session_factory() as session:
        runner = SuiteRunner(session)
        options = SuiteOptions(
            suite_name=suite,
            provider=provider,
            endpoint_filter=endpoint,
            repeats=repeats,
            delay_seconds=delay,
            dry_run=dry_run,
            live=live,
            cost_ceiling_usd=cost_ceiling,
            probes_dir=probes_dir,
            data_origin=data_origin,  # type: ignore[arg-type]
        )
        try:
            summary = runner.run(options)
        except CostCeilingExceededError as exc:
            typer.echo(f"REFUSED: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        if dry_run:
            return
        run = summary.suite_run
        if run is None:  # pragma: no cover
            typer.echo("run produced no SuiteRun")
            raise typer.Exit(code=1)
        typer.echo(
            f"suite {run.suite_name!r}: status={run.status} origin={run.data_origin} "
            f"completed={run.completed_observations}/{run.total_expected_observations} "
            f"failed={run.failed_observations} run_id={run.id}"
        )
        if run.error_summary:
            typer.echo(f"  note: {run.error_summary}")
        if run.status != "completed":
            raise typer.Exit(code=1)


@app.command("detect-changes")
def detect_changes(
    endpoint_id: int | None = typer.Option(None, "--endpoint", help="Restrict to an endpoint id."),
    split_after: str | None = typer.Option(
        None, "--split-after", help="ISO-8601 boundary between baseline and current windows."
    ),
    origin: str = typer.Option(
        "live", "--origin", help="Data origin to detect on (live, mock, demo, test)."
    ),
    allow_unqualified: bool = typer.Option(
        False, "--allow-unqualified",
        help="Allow behavioral claims from unqualified cohorts (non-live use only).",
    ),
) -> None:
    """Run conservative change detection and persist provisional events."""
    from endpoint_ledger.change_detection.service import detect_changes as run_detection

    session_factory = _session_factory()
    split_dt = None
    if split_after:
        split_dt = datetime.fromisoformat(split_after)
    with session_factory() as session:
        result = run_detection(
            session,
            get_settings(),
            endpoint_id=endpoint_id,
            split_after=split_dt,
            origin=origin,  # type: ignore[arg-type]
            require_qualified_cohort=not allow_unqualified,
        )
        for refusal in result.refusals:
            typer.echo(f"REFUSED endpoint={refusal.endpoint_id} metric={refusal.metric_name}: {refusal.reason}")
        if not result.events:
            if not result.refusals:
                typer.echo("no change events detected")
            return
        for event in result.events:
            typer.echo(
                f"event [{event.id}] endpoint={event.endpoint_id} metric={event.metric_name} "
                f"origin={event.data_origin} direction={event.direction} "
                f"{event.baseline_value} -> {event.current_value} "
                f"abs={event.absolute_change:+.3f} p={event.p_value} status={event.status}"
            )


@app.command("generate-demo-history")
def generate_demo_history(
    execute: bool = typer.Option(False, "--execute", help="Apply the deletion; dry-run by default."),
) -> None:
    """Generate baseline + persistent-regression mock history and a change event.

    Dry-run by default: prints the exact demo records that would be replaced.
    Only data_origin=demo records are ever touched; live evidence is never
    deleted. Uses only the MockProvider (no live calls).
    """
    from endpoint_ledger.change_detection.service import detect_changes as run_detection
    from endpoint_ledger.operations import delete_demo_runs

    settings = get_settings()
    session_factory = _session_factory()
    with session_factory() as session:
        delete_demo_runs(session, settings, execute=execute)
        if not execute:
            typer.echo("dry-run: nothing was changed; re-run with --execute to regenerate the demo")
            return

        seed_all(session, settings)
        mock_endpoint = session.scalar(
            select(ProviderEndpoint).where(ProviderEndpoint.provider == "mock")
        )
        if mock_endpoint is None:  # pragma: no cover
            raise RuntimeError("mock endpoint missing after seed")

        runner = SuiteRunner(session, settings)

        mock_endpoint.request_defaults = {"mock": {"mode": "stable_success", "seed": 1}}
        session.commit()
        baseline = runner.run(
            SuiteOptions(suite_name="demo-baseline", provider="mock", repeats=5, live=False)
        ).suite_run
        if baseline is None:  # pragma: no cover
            raise RuntimeError("baseline run failed")
        offset = timedelta(days=30)
        baseline.started_at = baseline.started_at - offset
        baseline.completed_at = (baseline.completed_at or baseline.started_at) - offset
        for observation in baseline.observations:
            observation.requested_at = observation.requested_at - offset
            if observation.completed_at is not None:
                observation.completed_at = observation.completed_at - offset
        session.commit()
        boundary = max(
            o.completed_at or o.requested_at for o in baseline.observations
        )

        mock_endpoint.request_defaults = {
            "mock": {"mode": "structured_output_regression", "seed": 1}
        }
        session.commit()
        regression = runner.run(
            SuiteOptions(suite_name="demo-regression", provider="mock", repeats=5, live=False)
        ).suite_run
        if regression is None:  # pragma: no cover
            raise RuntimeError("regression run failed")

        result = run_detection(
            session, settings, split_after=boundary,
            origin="demo",  # type: ignore[arg-type]
            require_qualified_cohort=False,
        )
        typer.echo(f"baseline run id={baseline.id} ({baseline.completed_observations} observations)")
        typer.echo(f"regression run id={regression.id} ({regression.completed_observations} observations)")
        typer.echo(f"split boundary: {boundary.isoformat()}")
        for refusal in result.refusals:
            typer.echo(f"REFUSED endpoint={refusal.endpoint_id} metric={refusal.metric_name}: {refusal.reason}")
        if not result.events:
            typer.echo("no change events detected")
            return
        for event in result.events:
            typer.echo(
                f"CHANGE EVENT [{event.id}] endpoint={event.endpoint_id} "
                f"metric={event.metric_name} origin={event.data_origin} direction={event.direction} "
                f"{event.baseline_value:.3f} -> {event.current_value:.3f} "
                f"abs={event.absolute_change:+.3f} p={event.p_value:.4f} status={event.status}"
            )


@app.command("audit-run")
def audit_run(
    run_id: int = typer.Argument(..., help="Suite run ID to audit."),
    output: str | None = typer.Option(
        None, "--output", help="Path to write the markdown report (default: docs/audits/run-N-audit.md)."
    ),
    reviewer_label: str = typer.Option(
        "audit-cli", "--reviewer-label", help="Label recorded on adjudications."
    ),
) -> None:
    """Audit every failed scored check of a run and persist adjudications."""
    from endpoint_ledger.audit_report import (
        REPORT_PATH_TEMPLATE,
        render_report,
    )
    from endpoint_ledger.audit_report import (
        audit_run as perform_audit,
    )

    session_factory = _session_factory()
    with session_factory() as session:
        settings = get_settings()
        run = session.get(SuiteRun, run_id)
        if run is None:
            typer.echo(f"suite run {run_id} does not exist", err=True)
            raise typer.Exit(code=1)
        checks = perform_audit(session, run_id, settings, reviewer_label=reviewer_label)
        if not checks:
            typer.echo(f"run {run_id} has no failed scored checks; nothing to audit")
            return
        report_path = output or REPORT_PATH_TEMPLATE.format(run_id=run_id)
        from pathlib import Path

        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_report(run_id, checks, run), encoding="utf-8")
        typer.echo(f"audited {len(checks)} failed scored checks; report written to {path}")
        for check in checks:
            typer.echo(
                f"  obs={check.observation_id} {check.probe_stable_key} v{check.probe_version_number} "
                f"repeat={check.repeat_index} {check.metric_name} -> {check.classification} "
                f"(eligible={check.baseline_eligible})"
            )


@app.command("baseline-status")
def baseline_status(
    provider: str | None = typer.Option(None, "--provider", help="Filter by provider."),
    origin: str = typer.Option("live", "--origin", help="Data origin to report on."),
) -> None:
    """Show baseline cohort status and qualification progress."""
    from endpoint_ledger.measurement.cohorts import CohortService

    session_factory = _session_factory()
    with session_factory() as session:
        service = CohortService(session, get_settings())
        reports = service.status_report(provider=provider, origin=origin)  # type: ignore[arg-type]
        if not reports:
            typer.echo("no baseline cohorts yet")
            return
        for report in reports:
            cohort = report["cohort"]
            progress = report["progress"]
            typer.echo(
                f"cohort [{cohort.id}] {cohort.probe_stable_key} v"
                f"{cohort.probe_version.version_number} model={cohort.model_requested}"
                f"/{cohort.model_reported} status={progress['status']}"
            )
            typer.echo(
                f"  date coverage: {progress['calendar_days']} of {progress['min_calendar_days']} days"
            )
            typer.echo(
                f"  eligible runs: {progress['distinct_runs']} of {progress['min_distinct_runs']}"
            )
            typer.echo(
                f"  eligible observations: {progress['eligible_primary_observations']} of "
                f"{progress['min_primary_observations']}"
            )
            typer.echo(
                f"  distinct dates: {progress['distinct_dates']} of {progress['min_distinct_dates']}"
            )
            typer.echo(
                f"  unresolved measurement issues: {progress['unresolved_measurement_issues']}"
            )
            if not progress["qualified"]:
                typer.echo(f"  unmet: {progress['unmet_requirements']}")


@app.command("run-baseline")
def run_baseline(
    provider: str = typer.Option("openai", help="Provider to run (openai)."),
    endpoint: str = typer.Option("gpt-4o-mini", help="Endpoint model/display name filter."),
    suite: str = typer.Option("smoke", help="Suite (probe directory)."),
    repeats: int | None = typer.Option(
        None, min=1, help="Repeats per probe (default 5; 1 with --qualification-smoke)."
    ),
    cost_ceiling: float = typer.Option(1.0, "--cost-ceiling", help="Hard cost ceiling (USD)."),
    live: bool = typer.Option(False, "--live", help="Required for live providers."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run preflight only; no provider, no DB writes."),
    qualification_smoke: bool = typer.Option(
        False, "--qualification-smoke",
        help="First post-change verification run: 1 repeat/probe, clearly labeled, requires manual approval.",
    ),
) -> None:
    """Collect baseline evidence with the full safe lifecycle.

    Preflight -> create open run -> execute -> finish -> seal -> export ->
    verify -> mark exported -> refresh cohorts. Secrets come only from the
    process environment; never from files.
    """
    from datetime import date as _date
    from pathlib import Path

    from endpoint_ledger.evidence import mark_export_failed, mark_exported, seal_run
    from endpoint_ledger.export import ExportInvalidError, export_run, verify_export
    from endpoint_ledger.preflight import run_preflight

    settings = get_settings()
    resolved_repeats = 1 if qualification_smoke else (repeats or 5)
    suite_name = (
        f"qualification-smoke-{_date.today().isoformat()}"
        if qualification_smoke
        else f"baseline-{provider}-{suite}"
    )

    session_factory = _session_factory()
    with session_factory() as session:
        report = run_preflight(
            session, settings,
            provider=provider, endpoint_filter=endpoint,
            suite=suite, repeats=resolved_repeats, cost_ceiling=cost_ceiling,
        )
        if dry_run:
            typer.echo(_render_preflight(report, fmt="text"))
            typer.echo("DRY RUN: preflight only — no provider call, no database writes")
            return
        if not report["ready"]:
            typer.echo("PREFLIGHT FAILED — baseline collection refused:", err=True)
            for refusal in report["refusals"]:
                typer.echo(f"  - {refusal}", err=True)
            raise typer.Exit(code=1)

        if not live:
            typer.echo("REFUSED: --live is required for baseline collection", err=True)
            raise typer.Exit(code=1)

        runner = SuiteRunner(session, settings)
        options = SuiteOptions(
            suite_name=suite_name,
            provider=provider,
            endpoint_filter=endpoint,
            repeats=resolved_repeats,
            live=True,
            cost_ceiling_usd=cost_ceiling,
            eligible_only=True,
        )
        try:
            summary = runner.run(options)
        except CostCeilingExceededError as exc:
            typer.echo(f"REFUSED: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        run = summary.suite_run
        if run is None:  # pragma: no cover
            typer.echo("run produced no SuiteRun")
            raise typer.Exit(code=1)
        typer.echo(
            f"baseline run id={run.id} status={run.status} origin={run.data_origin} "
            f"completed={run.completed_observations}/{run.total_expected_observations} "
            f"failed={run.failed_observations}"
        )
        if run.error_summary:
            typer.echo(f"  note: {run.error_summary}")

        seal_run(session, run)
        session.commit()
        typer.echo(f"evidence sealed: state={run.evidence_state}")

        try:
            artifact = export_run(session, run, settings)
            verification = verify_export(Path(artifact.path))
            if not verification["ok"]:
                raise ExportInvalidError("; ".join(verification["errors"]))
            artifact.verification_status = "verified"
            artifact.verified_at = datetime.now(UTC)
            mark_exported(session, run, artifact.sha256)
            session.commit()
            typer.echo(f"evidence exported: {artifact.path}")
            typer.echo(f"manifest hash: {artifact.sha256}")
            typer.echo(f"export verified: {verification['file_count']} files, hashes match")
        except Exception as exc:  # export failure must never destroy evidence
            session.rollback()
            session.expire_all()
            run = session.get(SuiteRun, run.id)
            mark_export_failed(session, run)
            session.commit()
            typer.echo(
                f"ERROR: provider run {run.id} succeeded but durable archival FAILED: {exc}",
                err=True,
            )
            typer.echo(
                "The completed run and its observations remain in the database, sealed.",
                err=True,
            )
            raise typer.Exit(code=1) from exc

        from endpoint_ledger.measurement.cohorts import CohortService

        cohort_service = CohortService(session, settings)
        cohort_reports = cohort_service.status_report(provider=provider, origin="live")
        typer.echo("Qualification progress:")
        if not cohort_reports:
            typer.echo("  no live cohorts yet")
        for cohort_report in cohort_reports:
            progress = cohort_report["progress"]
            typer.echo(
                f"  {cohort_report['cohort'].probe_stable_key}: {progress['status']} — "
                f"observations {progress['eligible_primary_observations']} of "
                f"{progress['min_primary_observations']}, runs {progress['distinct_runs']} of "
                f"{progress['min_distinct_runs']}"
            )


@app.command("export-run")
def export_run_command(
    run_id: int = typer.Argument(..., help="Suite run ID to export."),
) -> None:
    """Seal (if needed) and export a run into a portable evidence bundle."""
    from pathlib import Path

    from endpoint_ledger.evidence import mark_exported, seal_run
    from endpoint_ledger.export import export_run, verify_export

    session_factory = _session_factory()
    with session_factory() as session:
        run = session.get(SuiteRun, run_id)
        if run is None:
            typer.echo(f"suite run {run_id} does not exist", err=True)
            raise typer.Exit(code=1)
        if run.status not in ("completed", "completed_with_errors", "failed", "cancelled"):
            typer.echo(f"run {run_id} is not terminal; cannot export", err=True)
            raise typer.Exit(code=1)
        seal_run(session, run)
        artifact = export_run(session, run, get_settings())
        verification = verify_export(Path(artifact.path))
        if verification["ok"]:
            artifact.verification_status = "verified"
            artifact.verified_at = datetime.now(UTC)
            if run.data_origin == "live":
                mark_exported(session, run, artifact.sha256)
            session.commit()
            typer.echo(f"export: {artifact.path}")
            typer.echo(f"manifest hash: {artifact.sha256}")
            typer.echo(f"verified: {verification['file_count']} files")
        else:
            typer.echo(f"export verification FAILED: {verification['errors']}", err=True)
            raise typer.Exit(code=1)


@app.command("verify-run-export")
def verify_run_export(path: str = typer.Argument(..., help="Path to an export bundle.")) -> None:
    """Verify a run export bundle: hashes, JSON validity, secret scan."""
    from pathlib import Path

    from endpoint_ledger.export import verify_export

    result = verify_export(Path(path))
    if result["ok"]:
        typer.echo(f"export verified: {result['file_count']} files, manifest hash {result['manifest_hash']}")
    else:
        typer.echo("export verification FAILED:", err=True)
        for error in result["errors"]:
            typer.echo(f"  - {error}", err=True)
        raise typer.Exit(code=1)


@app.command("import-run-export")
def import_run_export(
    path: str = typer.Argument(..., help="Path to an export bundle."),
    target_empty_db: bool = typer.Option(True, "--target-empty-db", help="Require an empty database."),
) -> None:
    """Import a verified export. Requires an empty database by default."""
    from pathlib import Path

    from endpoint_ledger.export import import_export

    session_factory = _session_factory()
    with session_factory() as session:
        run = import_export(Path(path), session, get_settings(), require_empty=target_empty_db)
        typer.echo(f"imported suite run {run.id} ({run.suite_name}) with {len(run.observations)} observations")


@app.command("baseline-preflight")
def baseline_preflight(
    provider: str = typer.Option("openai"),
    endpoint: str = typer.Option("gpt-4o-mini"),
    suite: str = typer.Option("smoke"),
    repeats: int = typer.Option(5, min=1),
    cost_ceiling: float = typer.Option(1.0, "--cost-ceiling"),
    fmt: str = typer.Option("text", "--format", help="text or json"),
) -> None:
    """Readiness check for baseline collection. Never calls the provider."""
    from endpoint_ledger.preflight import run_preflight

    session_factory = _session_factory()
    with session_factory() as session:
        report = run_preflight(
            session, get_settings(),
            provider=provider, endpoint_filter=endpoint,
            suite=suite, repeats=repeats, cost_ceiling=cost_ceiling,
        )
        typer.echo(_render_preflight(report, fmt=fmt))
        if not report["ready"]:
            raise typer.Exit(code=1)


@app.command("approve-qualification-run")
def approve_qualification_run(
    run_id: int = typer.Argument(..., help="Qualification-smoke run ID to approve."),
) -> None:
    """Approve a live qualification-smoke run after manual audit.

    Approval adds a separate annotation; original observations and scores are
    never mutated.
    """
    from endpoint_ledger.db.models import Observation
    from endpoint_ledger.measurement.adjudication import MEASUREMENT_DEFECT_CLASSES
    from endpoint_ledger.measurement.cohorts import primary_outcome_for_observation

    session_factory = _session_factory()
    with session_factory() as session:
        run = session.get(SuiteRun, run_id)
        if run is None:
            typer.echo(f"suite run {run_id} does not exist", err=True)
            raise typer.Exit(code=1)
        observations = list(run.observations)
        errors = []

        if not observations:
            errors.append("run has no observations")
        if run.completed_observations < run.total_expected_observations:
            errors.append(
                f"missing observations ({run.completed_observations} of {run.total_expected_observations})"
            )
        if run.failed_observations > 0:
            failure_analysis = session.scalars(
                select(RunAnnotation).where(
                    RunAnnotation.suite_run_id == run_id,
                    RunAnnotation.annotation_type == "failure_analysis",
                )
            ).all()
            if not failure_analysis:
                errors.append(
                    "provider failures are present but not analyzed "
                    "(add a failure_analysis annotation first)"
                )

        probe_keys = {o.probe_version.probe.stable_key for o in observations}
        for key in sorted(probe_keys):
            probe_obs = [o for o in observations if o.probe_version.probe.stable_key == key]
            if not any(primary_outcome_for_observation(o)[0] is not None for o in probe_obs):
                errors.append(f"probe {key} has no primary score")

        defect_scores = session.scalars(
            select(ScoreAdjudication.id)
            .join(Score, Score.id == ScoreAdjudication.score_id)
            .join(Observation, Observation.id == Score.observation_id)
            .where(
                Observation.suite_run_id == run_id,
                ScoreAdjudication.classification.in_(sorted(MEASUREMENT_DEFECT_CLASSES)),
            )
        ).all()
        if defect_scores:
            errors.append(f"{len(defect_scores)} probe/scorer defect(s) recorded on this run")

        from pathlib import Path

        from endpoint_ledger.db.models import RunArtifact
        from endpoint_ledger.export import verify_export

        artifact = session.scalar(
            select(RunArtifact).where(
                RunArtifact.suite_run_id == run_id,
                RunArtifact.artifact_type == "run_export",
            ).order_by(RunArtifact.id.desc())
        )
        if artifact is None:
            errors.append("no evidence export exists for this run")
        else:
            verification = verify_export(Path(artifact.path))
            if not verification["ok"]:
                errors.append("evidence export does not verify")
            elif artifact.verification_status != "verified":
                errors.append("evidence export is not marked verified")

        if run.data_origin == "live":
            missing_reported = [
                o.id for o in observations
                if o.error_type is None and o.model_reported is None
            ]
            if missing_reported:
                errors.append(f"{len(missing_reported)} observation(s) lack provider-reported model")

        if errors:
            typer.echo("APPROVAL REFUSED:", err=True)
            for error in errors:
                typer.echo(f"  - {error}", err=True)
            raise typer.Exit(code=1)

        session.add(
            RunAnnotation(
                suite_run_id=run_id,
                annotation_type="qualification_approval",
                notes=(
                    f"qualification-smoke run approved after manual audit; "
                    f"{run.completed_observations} observations, 0 unresolved defects"
                ),
                reviewer_label="approve-qualification-run",
            )
        )
        session.commit()
        typer.echo(f"qualification-smoke run {run_id} approved (annotation added)")


def _render_preflight(report: dict, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(report, indent=2, default=str)
    lines = []
    lines.append("BASELINE PREFLIGHT")
    lines.append(f"  provider: {report['provider']}")
    if "endpoint" in report:
        endpoint = report["endpoint"]
        lines.append(
            f"  endpoint: {endpoint['display_name']} (model {endpoint['model_requested']})"
        )
    lines.append(
        f"  expected requests: {report.get('expected_request_count', 'n/a')} "
        f"(repeats {report.get('repeats', '?')})"
    )
    lines.append(f"  conservative max cost: ${report.get('conservative_max_cost_usd', 'unknown')}")
    for probe in report.get("probes", []):
        lines.append(
            f"  probe {probe['probe']} v{probe['version_number']}: "
            f"{'eligible' if probe['baseline_eligible'] else 'INELIGIBLE'}"
            f" primary={probe.get('primary_metric') or '-'}"
        )
    lines.append(f"  api credential present: {report['checks'].get('api_credential_present', False)}")
    lines.append(f"  most recent verified backup: {report.get('most_recent_verified_backup') or 'none'}")
    lines.append(f"  evidence export destination: {report.get('evidence_export_destination') or 'unset'}")
    lines.append(f"  unresolved measurement issues: {report.get('unresolved_measurement_issues', 0)}")
    lines.append(f"  ready: {report['ready']}")
    for refusal in report["refusals"]:
        lines.append(f"  REFUSAL: {refusal}")
    return "\n".join(lines)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Start the private server-rendered dashboard."""
    import uvicorn

    from endpoint_ledger.web.app import create_app

    typer.echo(f"Endpoint Ledger dashboard on http://{host}:{port}")
    uvicorn.run(create_app(start_scheduler=True), host=host, port=port, log_level="info")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"endpoint-ledger {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", callback=_version_callback, is_eager=True),
) -> None:
    """Endpoint Ledger CLI."""


if __name__ == "__main__":
    app()
