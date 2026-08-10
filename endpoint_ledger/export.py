"""Portable run evidence bundles.

A run export is a directory of deterministic JSON/JSONL files plus a manifest
listing every file with its SHA-256. The manifest's own hash is stored on the
SuiteRun (evidence_manifest_hash) and in run_artifacts. Exports never contain
API keys, authorization headers, or unrelated application data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import (
    ModelResolutionEvent,
    Observation,
    Probe,
    ProbeVersion,
    ProviderEndpoint,
    RunAnnotation,
    RunArtifact,
    Score,
    SuiteRun,
)

EXPORT_SCHEMA_VERSION = "1.0.0"

_SK_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
_FORBIDDEN_KEYS = ("authorization", "api-key", "api_key", "x-api-key")
_MANIFEST_FIELDS = ("schema_version", "run_id", "generated_at", "origin", "files")


class ExportInvalidError(RuntimeError):
    """Raised when an export contains secret-shaped content or fails checks."""


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _scan_for_secrets(text: str) -> list[str]:
    issues: list[str] = []
    if _SK_PATTERN.search(text):
        issues.append("secret-shaped token (sk-...) found")
    lowered = text.lower()
    for key in _FORBIDDEN_KEYS:
        if f'"{key}"' in lowered or f"'{key}'" in lowered:
            issues.append(f"forbidden key {key!r} found")
    for env_key in ("OPENAI_API_KEY", "API_KEY", "AUTH_TOKEN"):
        if f"{env_key}=" in text:
            issues.append(f"environment variable assignment {env_key} found")
    return issues


def _scan_file(path: Path) -> list[str]:
    return _scan_for_secrets(path.read_text(encoding="utf-8"))


def _run_dict(run: SuiteRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "suite_name": run.suite_name,
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "provider_filter": run.provider_filter,
        "endpoint_filter": run.endpoint_filter,
        "total_expected_observations": run.total_expected_observations,
        "completed_observations": run.completed_observations,
        "failed_observations": run.failed_observations,
        "estimated_cost_usd": run.estimated_cost_usd,
        "actual_cost_usd": run.actual_cost_usd,
        "runner_version": run.runner_version,
        "error_summary": run.error_summary,
        "data_origin": run.data_origin,
        "evidence_state": run.evidence_state,
        "sealed_at": run.sealed_at.isoformat() if run.sealed_at else None,
        "evidence_manifest_hash": run.evidence_manifest_hash,
        "evidence_exported_at": run.evidence_exported_at.isoformat() if run.evidence_exported_at else None,
    }


def _observation_dict(observation: Observation) -> dict[str, Any]:
    return {
        "id": observation.id,
        "suite_run_id": observation.suite_run_id,
        "endpoint_id": observation.endpoint_id,
        "probe_version_id": observation.probe_version_id,
        "repeat_index": observation.repeat_index,
        "requested_at": observation.requested_at.isoformat(),
        "completed_at": observation.completed_at.isoformat() if observation.completed_at else None,
        "latency_ms": observation.latency_ms,
        "http_status": observation.http_status,
        "model_reported": observation.model_reported,
        "finish_reason": observation.finish_reason,
        "raw_request": observation.raw_request,
        "raw_response": observation.raw_response,
        "raw_request_hash": observation.raw_request_hash,
        "raw_response_hash": observation.raw_response_hash,
        "normalized_text": observation.normalized_text,
        "normalized_tool_calls": observation.normalized_tool_calls,
        "input_tokens": observation.input_tokens,
        "output_tokens": observation.output_tokens,
        "estimated_cost_usd": observation.estimated_cost_usd,
        "error_type": observation.error_type,
        "error_message_sanitized": observation.error_message_sanitized,
        "provider_request_id": observation.provider_request_id,
        "error_code": observation.error_code,
        "error_category": observation.error_category,
        "pricing_input_price_per_million": observation.pricing_input_price_per_million,
        "pricing_output_price_per_million": observation.pricing_output_price_per_million,
        "pricing_effective_at": observation.pricing_effective_at.isoformat()
        if observation.pricing_effective_at
        else None,
        "cost_calculation_version": observation.cost_calculation_version,
        "cost_is_estimated": observation.cost_is_estimated,
    }


def _score_dict(score: Score) -> dict[str, Any]:
    return {
        "id": score.id,
        "observation_id": score.observation_id,
        "scorer_name": score.scorer_name,
        "scorer_version": score.scorer_version,
        "metric_name": score.metric_name,
        "passed": score.passed,
        "numeric_value": score.numeric_value,
        "explanation": score.explanation,
        "details": score.details,
        "role": score.role,
        "created_at": score.created_at.isoformat(),
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(_canonical_json(row) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def export_run(
    session: Session,
    run: SuiteRun,
    settings: Settings | None = None,
) -> RunArtifact:
    """Export a run into a portable bundle and register a RunArtifact.

    The bundle is written before any DB state is updated; the caller decides
    when to mark the run exported.
    """
    settings = settings or get_settings()
    directory = settings.evidence_export_path
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:  # pragma: no cover
        pass
    bundle_dir = directory / f"run-{run.id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')}"
    bundle_dir.mkdir(parents=True, exist_ok=False)
    try:
        os.chmod(bundle_dir, 0o700)
    except OSError:  # pragma: no cover
        pass

    observations = list(
        session.scalars(select(Observation).where(Observation.suite_run_id == run.id).order_by(Observation.id)).all()
    )
    score_rows = session.execute(
        select(Score).where(Score.observation_id.in_([o.id for o in observations]))
    ).scalars().all()
    endpoint = session.get(ProviderEndpoint, observations[0].endpoint_id) if observations else None

    probe_rows: dict[int, tuple[Probe, ProbeVersion]] = {}
    for observation in observations:
        version = observation.probe_version
        probe_rows[version.id] = (version.probe, version)

    resolution = (
        list(
            session.scalars(
                select(ModelResolutionEvent).where(
                    ModelResolutionEvent.requested_model == endpoint.model_requested
                )
            ).all()
        )
        if endpoint is not None
        else []
    )

    files: dict[str, Any] = {}
    files["suite_run.json"] = _canonical_json(_run_dict(run)) + "\n"
    files["endpoint.json"] = _canonical_json(
        {
            "id": endpoint.id,
            "provider": endpoint.provider,
            "display_name": endpoint.display_name,
            "model_requested": endpoint.model_requested,
            "request_defaults": endpoint.request_defaults,
            "input_cost_per_million_tokens": endpoint.input_cost_per_million_tokens,
            "output_cost_per_million_tokens": endpoint.output_cost_per_million_tokens,
        }
    ) + "\n" if endpoint else ""
    files["probes.json"] = _canonical_json(
        [
            {
                "probe": {"id": probe.id, "stable_key": probe.stable_key, "category": probe.category,
                          "title": probe.title, "description": probe.description},
                "version": {
                    "id": version.id,
                    "version_number": version.version_number,
                    "definition_hash": version.definition_hash,
                    "prompt_messages": version.prompt_messages,
                    "tools": version.tools,
                    "expected_behavior": version.expected_behavior,
                    "request_parameters": version.request_parameters,
                    "scorers": version.scorers,
                    "baseline_eligible": version.baseline_eligible,
                    "ineligibility_reason": version.ineligibility_reason,
                    "primary_metric": version.primary_metric,
                },
            }
            for probe, version in probe_rows.values()
        ]
    ) + "\n"
    files["observations.jsonl"] = "".join(_canonical_json(_observation_dict(o)) + "\n" for o in observations)
    files["scores.jsonl"] = "".join(_canonical_json(_score_dict(s)) + "\n" for s in score_rows)
    files["model_resolution.json"] = _canonical_json(
        [
            {"requested_model": e.requested_model, "old_model_reported": e.old_model_reported,
             "new_model_reported": e.new_model_reported,
             "first_observed_at": e.first_observed_at.isoformat(),
             "event_type": e.event_type, "notes": e.notes}
            for e in resolution
        ]
    ) + "\n"

    for name in files:
        if not files[name]:
            continue
        (bundle_dir / name).write_text(files[name], encoding="utf-8")
        os.chmod(bundle_dir / name, 0o600)

    # Secret-pattern scan before the export is marked valid.
    issues: list[str] = []
    for name in files:
        if files[name]:
            issues.extend(_scan_for_secrets(files[name]))
    if issues:
        raise ExportInvalidError("run export contains secret-shaped content: " + "; ".join(issues))

    manifest_files = []
    for name in sorted(files):
        if not files[name]:
            continue
        manifest_files.append({"path": name, "sha256": _sha256(files[name])})
    manifest = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "run_id": run.id,
        "generated_at": datetime.now(UTC).isoformat(),
        "origin": run.data_origin,
        "files": manifest_files,
    }
    manifest_hash = _sha256(_canonical_json(manifest))
    (bundle_dir / "manifest.json").write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
    os.chmod(bundle_dir / "manifest.json", 0o600)

    artifact = RunArtifact(
        suite_run_id=run.id,
        artifact_type="run_export",
        path=str(bundle_dir),
        sha256=manifest_hash,
        verification_status="pending",
    )
    session.add(artifact)
    session.flush()
    return artifact


def verify_export(path: Path) -> dict[str, Any]:
    """Verify a bundle: manifest hash, per-file hashes, JSON validity, secrets."""
    path = Path(path)
    if not (path / "manifest.json").exists():
        raise ExportInvalidError(f"no manifest.json in {path}")
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    for field in _MANIFEST_FIELDS:
        if field not in manifest:
            raise ExportInvalidError(f"manifest missing field {field!r}")
    recomputed = _sha256(_canonical_json({k: manifest[k] for k in _MANIFEST_FIELDS}))

    issues: list[str] = []
    for entry in manifest["files"]:
        file_path = path / entry["path"]
        if not file_path.exists():
            issues.append(f"missing file: {entry['path']}")
            continue
        actual = _sha256(file_path.read_text(encoding="utf-8"))
        if actual != entry["sha256"]:
            issues.append(f"hash mismatch: {entry['path']}")
        try:
            if entry["path"].endswith(".jsonl"):
                _read_jsonl(file_path)
            elif entry["path"].endswith(".json"):
                json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as exc:
            issues.append(f"invalid JSON: {entry['path']}: {exc}")
        issues.extend(_scan_file(file_path))

    for sibling in path.iterdir():
        if sibling.name not in {e["path"] for e in manifest["files"]} | {"manifest.json"}:
            issues.append(f"unexpected file in bundle: {sibling.name}")

    return {
        "ok": not issues,
        "errors": issues,
        "file_count": len(manifest["files"]),
        "manifest_hash": recomputed,
    }


def import_export(
    path: Path,
    session: Session,
    settings: Settings | None = None,
    require_empty: bool = True,
) -> SuiteRun:
    """Import a verified bundle into a database.

    Requires an empty database (unless ``require_empty=False`` for tests).
    Idempotent: rows whose identifiers already exist are skipped.
    """
    settings = settings or get_settings()
    result = verify_export(path)
    if not result["ok"]:
        raise ExportInvalidError("refusing import of unverified export: " + "; ".join(result["errors"]))
    if require_empty:
        existing_run = session.scalar(select(SuiteRun.id).limit(1))
        existing_obs = session.scalar(select(Observation.id).limit(1))
        if existing_run or existing_obs:
            raise RuntimeError("import refused: target database is not empty")

    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    run_data = json.loads((path / "suite_run.json").read_text(encoding="utf-8"))
    probes = json.loads((path / "probes.json").read_text(encoding="utf-8"))
    observations = _read_jsonl(path / "observations.jsonl")
    scores = _read_jsonl(path / "scores.jsonl")

    for entry in probes:
        probe_id = entry["probe"]["id"]
        if session.get(Probe, probe_id) is None:
            session.add(Probe(id=probe_id, **{k: entry["probe"][k] for k in
                                              ("stable_key", "category", "title", "description")}))
        version = entry["version"]
        if session.get(ProbeVersion, version["id"]) is None:
            session.add(
                ProbeVersion(
                    id=version["id"],
                    probe_id=probe_id,
                    version_number=version["version_number"],
                    definition_hash=version["definition_hash"],
                    prompt_messages=version["prompt_messages"],
                    tools=version["tools"],
                    expected_behavior=version["expected_behavior"],
                    request_parameters=version["request_parameters"],
                    scorers=version["scorers"],
                    baseline_eligible=version["baseline_eligible"],
                    ineligibility_reason=version["ineligibility_reason"],
                    primary_metric=version["primary_metric"],
                )
            )
    session.flush()

    endpoint_file = path / "endpoint.json"
    endpoint_id = None
    if endpoint_file.exists():
        endpoint = json.loads(endpoint_file.read_text(encoding="utf-8"))
        endpoint_id = endpoint["id"]
        if session.get(ProviderEndpoint, endpoint_id) is None:
            session.add(
                ProviderEndpoint(
                    id=endpoint_id,
                    provider=endpoint["provider"],
                    display_name=endpoint["display_name"],
                    model_requested=endpoint["model_requested"],
                    request_defaults=endpoint["request_defaults"],
                    input_cost_per_million_tokens=endpoint["input_cost_per_million_tokens"],
                    output_cost_per_million_tokens=endpoint["output_cost_per_million_tokens"],
                )
            )
    session.flush()

    run = session.get(SuiteRun, run_data["id"])
    if run is None:
        run = SuiteRun(id=run_data["id"])
        for field in (
            "suite_name", "status", "started_at", "completed_at", "provider_filter",
            "endpoint_filter", "total_expected_observations", "completed_observations",
            "failed_observations", "estimated_cost_usd", "actual_cost_usd", "runner_version",
            "error_summary", "data_origin", "evidence_state", "sealed_at",
            "evidence_manifest_hash", "evidence_exported_at",
        ):
            setattr(run, field, run_data[field])
        session.add(run)
    session.flush()

    for row in observations:
        if session.get(Observation, row["id"]) is not None:
            continue
        session.add(Observation(id=row["id"], **{k: v for k, v in row.items() if k != "id"}))
    session.flush()
    for row in scores:
        if session.get(Score, row["id"]) is not None:
            continue
        session.add(Score(id=row["id"], **{k: v for k, v in row.items() if k != "id"}))
    session.flush()

    session.add(
        RunAnnotation(
            suite_run_id=run.id,
            annotation_type="evidence_import",
            notes=f"imported from {path} (manifest {manifest['schema_version']})",
            reviewer_label="import-run-export",
        )
    )
    session.commit()
    return run
