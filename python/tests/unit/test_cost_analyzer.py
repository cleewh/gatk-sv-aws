"""Unit tests for the richer Cost Optimizer analyzer + helpers.

Covers:

* :func:`analyze_cohort` — Cost Explorer response → :class:`CostReport` mapping.
* :func:`record_peak_working_set` / :func:`load_peak_working_sets` — log round-trip.
* :func:`surface_overage` — overage vs under-target behavior.
* :func:`apply_recommendation` — approval gating (Req 9.4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gatk_sv_aws.cost import (
    ApprovalRequiredError,
    CohortRunInput,
    Recommendation,
    analyze_cohort,
    apply_recommendation,
    load_peak_working_sets,
    record_peak_working_set,
    surface_overage,
)
from gatk_sv_aws.models import (
    CostAttribution,
    CostDimension,
    CostReport,
)


class FakeCostExplorerClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get_cost_and_usage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def test_analyze_cohort_under_target() -> None:
    response = {
        "ResultsByTime": [
            {
                "Groups": [
                    {
                        "Keys": [
                            "gatk-sv:module$GatherSampleEvidence",
                            "AWS HealthOmics",
                        ],
                        "Metrics": {"UnblendedCost": {"Amount": "25.50"}},
                    },
                    {
                        "Keys": [
                            "gatk-sv:module$AnnotateVcf",
                            "Amazon Simple Storage Service",
                        ],
                        "Metrics": {"UnblendedCost": {"Amount": "4.50"}},
                    },
                ]
            }
        ]
    }
    client = FakeCostExplorerClient(response)

    report = analyze_cohort(
        runs=[
            CohortRunInput(module="GatherSampleEvidence", run_id="r1"),
            CohortRunInput(module="AnnotateVcf", run_id="r2"),
        ],
        cohort_id="validation-cohort",
        sample_count=10,
        ce_client=client,
    )

    assert isinstance(report, CostReport)
    assert report.total_cost_usd == 30.0
    assert report.per_sample_cost_usd == 3.0
    assert report.over_target is False
    # Attribution should carry both modules and the expected dimensions.
    dim_by_module = {(a.module, a.dimension) for a in report.attribution}
    assert ("GatherSampleEvidence", CostDimension.COMPUTE) in dim_by_module
    assert ("AnnotateVcf", CostDimension.STORAGE) in dim_by_module


def test_analyze_cohort_over_target_flags_report() -> None:
    response = {
        "ResultsByTime": [
            {
                "Groups": [
                    {
                        "Keys": ["gatk-sv:module$ClusterBatch", "AWS HealthOmics"],
                        "Metrics": {"UnblendedCost": {"Amount": "100.00"}},
                    },
                ]
            }
        ]
    }
    client = FakeCostExplorerClient(response)

    report = analyze_cohort(
        runs=[CohortRunInput(module="ClusterBatch", run_id="r1")],
        cohort_id="validation-cohort",
        sample_count=10,
        ce_client=client,
    )

    assert report.per_sample_cost_usd == 10.0
    assert report.over_target is True


def test_analyze_cohort_rejects_zero_samples() -> None:
    client = FakeCostExplorerClient({"ResultsByTime": []})
    with pytest.raises(ValueError, match="sample_count"):
        analyze_cohort(
            runs=[],
            cohort_id="c",
            sample_count=0,
            ce_client=client,
        )


def test_record_and_load_peak_working_sets(tmp_path: Path) -> None:
    log = tmp_path / "opt.jsonl"
    record_peak_working_set("GatherSampleEvidence", 150.0, log)
    record_peak_working_set("GatherSampleEvidence", 200.0, log)  # higher peak
    record_peak_working_set("ClusterBatch", 80.0, log)

    peaks = load_peak_working_sets(log)
    assert peaks == {"GatherSampleEvidence": 200.0, "ClusterBatch": 80.0}


def test_load_peak_working_sets_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_peak_working_sets(tmp_path / "nope.jsonl") == {}


def test_surface_overage_returns_none_when_under_target() -> None:
    report = CostReport(
        cohort_id="c",
        sample_count=10,
        runs=[],
        total_cost_usd=60.0,
        per_sample_cost_usd=6.0,
        target_usd=7.00,
        over_target=False,
        attribution=[],
    )
    assert surface_overage(report) is None


def test_surface_overage_surfaces_attribution_on_overage() -> None:
    report = CostReport(
        cohort_id="c",
        sample_count=10,
        runs=[],
        total_cost_usd=100.0,
        per_sample_cost_usd=10.0,
        target_usd=7.00,
        over_target=True,
        attribution=[
            CostAttribution(
                module="ClusterBatch",
                dimension=CostDimension.COMPUTE,
                cost_usd=40.0,
            )
        ],
    )
    overage = surface_overage(report)
    assert overage is not None
    assert overage.over_by_usd == 3.0
    assert overage.attribution[0].module == "ClusterBatch"


def test_apply_recommendation_requires_approval() -> None:
    rec = Recommendation(
        task_name="ClusterBatch.cluster",
        recommended_cpu=4.8,
        recommended_memory_gib=19.2,
        cpu_reduction_pct=0.0,
        memory_reduction_pct=0.0,
    )
    with pytest.raises(ApprovalRequiredError):
        apply_recommendation(rec, approved_by=None)
    with pytest.raises(ApprovalRequiredError):
        apply_recommendation(rec, approved_by="")

    applied = apply_recommendation(rec, approved_by="ops-user@example")
    assert applied == rec
