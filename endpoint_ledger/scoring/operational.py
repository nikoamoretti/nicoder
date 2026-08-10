"""Deterministic operational scorers (HTTP, errors, latency)."""

from __future__ import annotations

from endpoint_ledger.scoring.base import Scorer, ScoreResult


class HttpSuccess(Scorer):
    name = "http_success"
    metric_name = "http_success"

    def score(self, response, expected_behavior) -> ScoreResult:
        status = response.http_status
        passed = status is not None and 200 <= status < 400
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            explanation=(
                f"HTTP {status} (success)"
                if passed
                else (f"HTTP {status} (non-success)" if status is not None else "no HTTP status recorded")
            ),
            details={"http_status": status},
        )


class ErrorOccurrence(Scorer):
    name = "error_occurrence"
    metric_name = "error_occurrence"

    def score(self, response, expected_behavior) -> ScoreResult:
        error = response.error
        passed = error is None
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            explanation=(
                "no provider error occurred"
                if passed
                else f"provider error occurred: {error.error_type}"
            ),
            details=(
                {}
                if error is None
                else {
                    "error_type": error.error_type,
                    "http_status": error.http_status,
                    "message_sanitized": error.message_sanitized,
                }
            ),
        )


class LatencyMeasurement(Scorer):
    name = "latency_measurement"
    metric_name = "latency_measurement"

    def score(self, response, expected_behavior) -> ScoreResult:
        threshold = expected_behavior.get("max_latency_ms")
        latency = response.latency_ms
        passed = None if latency is None or threshold is None else latency <= int(threshold)
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            numeric_value=float(latency) if latency is not None else None,
            explanation=(
                f"measured latency {latency} ms (threshold {threshold} ms)"
                if threshold is not None
                else f"measured latency {latency} ms (no threshold configured)"
            ),
            details={"latency_ms": latency, "max_latency_ms": threshold},
        )
