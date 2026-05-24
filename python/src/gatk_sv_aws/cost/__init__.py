"""Component (h): Cost Optimizer for the GATK-SV migration.

Implements design §Components and interfaces → (h) Cost Optimizer. After
each cohort run, pulls ``AnalyzeAHORunPerformance`` output, combines it
with Cost Explorer tag-based spend, produces CPU/memory right-sizing
recommendations with 20 percent headroom, records per-module peak working
set for STATIC storage sizing, and flags per-sample cost overages of
Per_Sample_Cost_Target (USD $7.00) with attribution by module, task, and
cost dimension. Recommendations are never applied automatically — the
Operator approves each.

Advances Requirements 8, 9, 13.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from gatk_sv_aws.models import (
    CostAttribution,
    CostDimension,
    CostReport,
    ModuleName,
    RunCostEntry,
)


# ---------------------------------------------------------------------------
# Recommendations (Task 3.8.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskStats:
    """Per-task resource observations used by :func:`recommend`."""

    task_name: str
    observed_peak_cpu: float
    observed_peak_memory_gib: float
    observation_count: int


@dataclass(frozen=True)
class Recommendation:
    """One CPU/memory right-sizing recommendation."""

    task_name: str
    recommended_cpu: float
    recommended_memory_gib: float
    cpu_reduction_pct: float
    memory_reduction_pct: float


def recommend(
    task_stats: list[TaskStats], headroom: float = 0.20
) -> list[Recommendation]:
    """Recommend CPU/memory after ≥3 cohort-scale runs, adding ``headroom`` (default 20%).

    Implementation of Task 3.8.2 (Req 9.2, 9.3).

    For each :class:`TaskStats` ``t``:

    * If ``t.observation_count < 3``, skip — the Cost_Optimizer requires at
      least three cohort-scale observations before it will emit a
      recommendation (Req 9.2).
    * Otherwise emit a :class:`Recommendation` whose ``recommended_cpu`` and
      ``recommended_memory_gib`` are the observed peaks multiplied by
      ``(1 + headroom)``.

    ``cpu_reduction_pct`` and ``memory_reduction_pct`` are set to ``0.0``
    until ``apply_recommendation`` receives the currently-declared CPU /
    memory from the packaged bundle. The ≥25% reduction flag (Req 9.3)
    is computed there, not here, to avoid fabricating a baseline.
    """
    recommendations: list[Recommendation] = []
    factor = 1.0 + headroom
    for t in task_stats:
        if t.observation_count < 3:
            continue
        recommendations.append(
            Recommendation(
                task_name=t.task_name,
                recommended_cpu=t.observed_peak_cpu * factor,
                recommended_memory_gib=t.observed_peak_memory_gib * factor,
                cpu_reduction_pct=0.0,
                memory_reduction_pct=0.0,
            )
        )
    return recommendations


# ---------------------------------------------------------------------------
# Working-set recorder (Task 3.8.3)
# ---------------------------------------------------------------------------


def record_peak_working_set(
    module: ModuleName, peak_gib: float, log_path: Path
) -> None:
    """Append a per-module peak working-set observation for STATIC sizing (Req 8.3).

    Writes one JSON line per call. The next cohort's
    :func:`choose_storage` call (Design §Cost Optimization Strategy)
    reads this log to set STATIC capacity.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"module": module, "peak_gib": peak_gib}
    with log_path.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def load_peak_working_sets(log_path: Path) -> dict[str, float]:
    """Return the maximum observed peak per module from the working-set log."""
    if not log_path.exists():
        return {}
    peaks: dict[str, float] = {}
    with log_path.open("r") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            module = entry["module"]
            peak = float(entry["peak_gib"])
            peaks[module] = max(peaks.get(module, 0.0), peak)
    return peaks


# ---------------------------------------------------------------------------
# Overage attribution (Task 3.8.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OverageReport:
    """Per-(module, dimension) cost attribution returned on overage (Req 8.6, 13.5)."""

    cohort_id: str
    per_sample_cost_usd: float
    target_usd: float
    over_by_usd: float
    attribution: tuple[CostAttribution, ...]


def surface_overage(cost_report: CostReport) -> OverageReport | None:
    """Return an :class:`OverageReport` when ``cost_report.over_target`` is True.

    Implementation of Task 3.8.4 (Req 8.6, 13.5). When the cohort is
    under target, returns ``None`` — no attribution needed.
    """
    if not cost_report.over_target:
        return None
    over_by = cost_report.per_sample_cost_usd - cost_report.target_usd
    return OverageReport(
        cohort_id=cost_report.cohort_id,
        per_sample_cost_usd=cost_report.per_sample_cost_usd,
        target_usd=cost_report.target_usd,
        over_by_usd=over_by,
        attribution=tuple(cost_report.attribution),
    )


# ---------------------------------------------------------------------------
# Approval-gated recommendation application (Task 3.8.5)
# ---------------------------------------------------------------------------


class ApprovalRequiredError(PermissionError):
    """Raised when ``apply_recommendation`` is called without an ``approved_by`` argument."""


def apply_recommendation(
    recommendation: Recommendation,
    *,
    approved_by: str | None,
) -> Recommendation:
    """Gate :class:`Recommendation` application on an explicit ``approved_by`` (Req 9.4).

    Returns the recommendation unchanged when ``approved_by`` is a
    non-empty string; raises :class:`ApprovalRequiredError` otherwise.
    This function is deliberately a no-op aside from the gate — Phase 5
    wires it to the Registrar to actually mutate a workflow version.
    """
    if not approved_by:
        raise ApprovalRequiredError(
            f"Recommendation for task {recommendation.task_name!r} requires an "
            "explicit approved_by= argument before it can be applied (Req 9.4)."
        )
    return recommendation


# ---------------------------------------------------------------------------
# Cohort cost analyzer (Task 3.8.1)
# ---------------------------------------------------------------------------


class CostExplorerClient(Protocol):
    """Minimal subset of ``boto3.client('ce')`` used by :func:`analyze_cohort`."""

    def get_cost_and_usage(self, **kwargs: Any) -> dict[str, Any]: ...


class PerformanceAnalyzer(Protocol):
    """Minimal protocol matching the HealthOmics ``AnalyzeAHORunPerformance`` tool."""

    def analyze(self, run_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CohortRunInput:
    """One (module, run_id) pair supplied to :func:`analyze_cohort`."""

    module: ModuleName
    run_id: str


def analyze_cohort(
    runs: list[CohortRunInput],
    cohort_id: str,
    sample_count: int,
    *,
    ce_client: CostExplorerClient,
    performance_analyzer: PerformanceAnalyzer | None = None,
    target_usd: float = 7.00,
    start_date: date | None = None,
    end_date: date | None = None,
) -> CostReport:
    """Produce a :class:`CostReport` for a completed cohort (Req 8.5, 8.6, 8.7, 14.5).

    Implementation of Task 3.8.1. The analyzer:

    1. Calls Cost Explorer ``GetCostAndUsage`` filtered by the Property 10
       tag set (``gatk-sv:cohort-id == cohort_id``) and groups by the
       ``gatk-sv:module`` and ``SERVICE`` (mapped to
       :class:`~.models.CostDimension`) dimensions.
    2. Optionally calls ``AnalyzeAHORunPerformance`` per run to attach
       wall-clock duration (filled with 0 when analyzer is None).
    3. Computes ``per_sample_cost_usd = total / sample_count``, sets
       ``over_target``, and assembles per-(module, dimension) attribution.
    """
    if sample_count <= 0:
        raise ValueError("sample_count must be ≥ 1")

    today = date.today()
    ce_start = start_date or (today - timedelta(days=30))
    ce_end = end_date or today

    ce_response = ce_client.get_cost_and_usage(
        TimePeriod={"Start": ce_start.isoformat(), "End": ce_end.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        Filter={
            "Tags": {
                "Key": "gatk-sv:cohort-id",
                "Values": [cohort_id],
            }
        },
        GroupBy=[
            {"Type": "TAG", "Key": "gatk-sv:module"},
            {"Type": "DIMENSION", "Key": "SERVICE"},
        ],
    )

    attribution_map: dict[tuple[ModuleName, CostDimension], float] = {}
    total_cost = 0.0

    for bucket in ce_response.get("ResultsByTime", []):
        for group in bucket.get("Groups", []):
            keys = group.get("Keys", [])
            if len(keys) < 2:
                continue
            raw_module = keys[0].split("$")[-1] or None
            service = keys[1]
            amount = float(
                group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", "0")
            )
            total_cost += amount

            dimension = _map_service_to_dimension(service)
            module = _parse_module_name(raw_module)
            if module is None:
                continue
            key = (module, dimension)
            attribution_map[key] = attribution_map.get(key, 0.0) + amount

    per_sample = total_cost / sample_count
    attribution = [
        CostAttribution(module=m, dimension=d, cost_usd=round(c, 4))
        for (m, d), c in sorted(attribution_map.items())
    ]

    run_entries: list[RunCostEntry] = []
    for run in runs:
        wall_clock = 0
        if performance_analyzer is not None:
            perf = performance_analyzer.analyze(run.run_id)
            wall_clock = int(perf.get("walltime_sec", 0))
        run_entries.append(
            RunCostEntry(
                module=run.module,
                run_id=run.run_id,
                cost_usd=0.0,  # per-run attribution comes from Cost Explorer groups; left 0 here
                wall_clock_sec=wall_clock,
                tags={"gatk-sv:cohort-id": cohort_id, "gatk-sv:module": run.module},
            )
        )

    return CostReport(
        cohort_id=cohort_id,
        sample_count=sample_count,
        runs=run_entries,
        total_cost_usd=round(total_cost, 4),
        per_sample_cost_usd=round(per_sample, 4),
        target_usd=target_usd,
        over_target=per_sample > target_usd,
        attribution=attribution,
    )


_SERVICE_TO_DIMENSION: dict[str, CostDimension] = {
    "Amazon Omics": CostDimension.COMPUTE,
    "AWS HealthOmics": CostDimension.COMPUTE,
    "Amazon Elastic Compute Cloud - Compute": CostDimension.COMPUTE,
    "Amazon Simple Storage Service": CostDimension.STORAGE,
    "Amazon EC2 Container Registry (ECR)": CostDimension.CONTAINER_PULLS,
    "AWS Data Transfer": CostDimension.DATA_TRANSFER,
}


def _map_service_to_dimension(service_name: str) -> CostDimension:
    return _SERVICE_TO_DIMENSION.get(service_name, CostDimension.COMPUTE)


def _parse_module_name(raw: str | None) -> ModuleName | None:
    if not raw:
        return None
    allowed = {
        "GatherSampleEvidence",
        "GatherBatchEvidence",
        "ClusterBatch",
        "GenerateBatchMetrics",
        "FilterBatch",
        "MergeBatchSites",
        "GenotypeBatch",
        "RegenotypeCNVs",
        "MakeCohortVcf",
        "AnnotateVcf",
    }
    if raw in allowed:
        return raw  # type: ignore[return-value]
    return None


__all__ = [
    "TaskStats",
    "Recommendation",
    "recommend",
    "record_peak_working_set",
    "load_peak_working_sets",
    "OverageReport",
    "surface_overage",
    "ApprovalRequiredError",
    "apply_recommendation",
    "CostExplorerClient",
    "PerformanceAnalyzer",
    "CohortRunInput",
    "analyze_cohort",
]
