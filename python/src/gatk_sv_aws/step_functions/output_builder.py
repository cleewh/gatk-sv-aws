"""Output builder helpers for Step Functions terminal states.

Provides helper functions to construct PipelineSuccessOutput and
PipelineFailureOutput from the execution context. These helpers are
called by the state machine's terminal states (via Lambda or inline).

When the pipeline fails at module N, modules 1 through N-1 have already
written outputs to S3 (immutable). Re-running with the same inputs
leverages CACHE_ALWAYS to skip completed modules — no cleanup or
rollback of prior module outputs occurs.

Requirements: 9.2, 9.3, 11.3.
"""

from __future__ import annotations

from typing import Any

from gatk_sv_aws.step_functions.models import (
    CompletedModuleSummary,
    ModuleRunSummary,
    PipelineFailureOutput,
    PipelineSuccessOutput,
)


def build_success_output(
    cohort_id: str,
    module_runs: list[dict[str, Any]],
    total_cost_usd: float,
    per_sample_cost_usd: float,
    output_uri: str,
    duration_seconds: int,
    cost_report_uri: str,
) -> dict[str, Any]:
    """Build a PipelineSuccessOutput from the full execution context.

    Parameters
    ----------
    cohort_id : str
        Cohort identifier.
    module_runs : list[dict]
        List of all 10 module run records. Each dict must contain:
        module, run_id, duration_seconds, is_cache_hit.
    total_cost_usd : float
        Total pipeline cost in USD.
    per_sample_cost_usd : float
        Cost per sample in USD.
    output_uri : str
        S3 output prefix for the cohort run.
    duration_seconds : int
        Total pipeline duration in seconds.
    cost_report_uri : str
        S3 URI of the cost-report.json file.

    Returns
    -------
    dict
        Serialized PipelineSuccessOutput.
    """
    run_summaries = [
        ModuleRunSummary(
            module=run["module"],
            run_id=run["run_id"],
            duration_seconds=run.get("duration_seconds", 0),
            is_cache_hit=run.get("is_cache_hit", False),
        )
        for run in module_runs
    ]

    output = PipelineSuccessOutput(
        cohort_id=cohort_id,
        status="COMPLETED",
        module_runs=run_summaries,
        total_cost_usd=total_cost_usd,
        per_sample_cost_usd=per_sample_cost_usd,
        output_uri=output_uri,
        duration_seconds=duration_seconds,
        cost_report_uri=cost_report_uri,
    )
    return output.model_dump()


def build_failure_output(
    cohort_id: str,
    failed_module: str,
    failed_run_id: str,
    error_message: str,
    error_code: str | None,
    retry_attempts: int,
    completed_module_runs: list[dict[str, Any]],
    partial_cost_usd: float | None = None,
    sample_count: int | None = None,
) -> dict[str, Any]:
    """Build a PipelineFailureOutput from the current execution context.

    When the pipeline fails at module N, this function builds the failure
    output including all modules that completed successfully (1 through N-1).
    Their outputs remain in S3 (immutable) and a re-run with the same inputs
    will leverage CACHE_ALWAYS to skip them.

    Parameters
    ----------
    cohort_id : str
        Cohort identifier.
    failed_module : str
        Module that caused the pipeline failure.
    failed_run_id : str
        HealthOmics run ID of the failed run.
    error_message : str
        Human-readable error message from the failed run.
    error_code : str | None
        Error code for classification (e.g. InternalServerError).
    retry_attempts : int
        Number of retry attempts made before failure.
    completed_module_runs : list[dict]
        List of module runs that completed successfully before the failure.
        Each dict must contain: module, run_id.
    partial_cost_usd : float | None
        Partial cost for completed modules (None if unavailable).
    sample_count : int | None
        Number of samples (used for per-sample cost in partial report).

    Returns
    -------
    dict
        Serialized PipelineFailureOutput.
    """
    completed_modules = [
        CompletedModuleSummary(
            module=run["module"],
            run_id=run["run_id"],
        )
        for run in completed_module_runs
    ]

    # Build partial cost report if cost data is available
    partial_cost_report: dict[str, Any] | None = None
    if partial_cost_usd is not None:
        partial_cost_report = {
            "total_cost_usd": partial_cost_usd,
            "modules_completed": len(completed_module_runs),
            "note": "Partial cost report — pipeline did not complete all modules.",
        }
        if sample_count and sample_count > 0:
            partial_cost_report["per_sample_cost_usd"] = (
                partial_cost_usd / sample_count
            )

    output = PipelineFailureOutput(
        cohort_id=cohort_id,
        status="FAILED",
        failed_module=failed_module,
        failed_run_id=failed_run_id,
        error_message=error_message,
        error_code=error_code,
        retry_attempts=retry_attempts,
        completed_modules=completed_modules,
        partial_cost_report=partial_cost_report,
    )
    return output.model_dump()
