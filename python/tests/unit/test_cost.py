"""Unit tests for the GATK-SV Cost_Optimizer component (Design §Components.h).

These example-based tests complement the Hypothesis property tests in
``tests/gatk_sv_aws/properties/`` by exercising specific boundary
cases of :func:`recommend`:

* Insufficient observations (< 3) → no recommendation emitted (Req 9.2).
* Boundary case: exactly 3 observations → recommendation emitted
  with default 20% headroom (Req 9.2).
* Custom headroom is honored.
* Multiple tasks with mixed observation counts: only tasks with ≥3
  observations produce recommendations, and the order is preserved.
"""

from __future__ import annotations

from gatk_sv_aws.cost import (
    Recommendation,
    TaskStats,
    recommend,
)


def _stats(name: str, cpu: float, mem: float, obs: int) -> TaskStats:
    return TaskStats(
        task_name=name,
        observed_peak_cpu=cpu,
        observed_peak_memory_gib=mem,
        observation_count=obs,
    )


def test_recommend_one_observation_returns_empty() -> None:
    """With a single observation, the Cost_Optimizer must not recommend (Req 9.2)."""
    recs = recommend([_stats("t", cpu=4.0, mem=16.0, obs=1)])

    assert recs == []


def test_recommend_two_observations_returns_empty() -> None:
    """With two observations, still below the ≥3 threshold — no recommendation (Req 9.2)."""
    recs = recommend([_stats("t", cpu=4.0, mem=16.0, obs=2)])

    assert recs == []


def test_recommend_three_observations_emits_default_headroom() -> None:
    """At the ≥3 boundary, recommendation is observed peak × (1 + 0.20) (Req 9.2)."""
    recs = recommend([_stats("GatherSampleEvidence.Manta", cpu=4.0, mem=16.0, obs=3)])

    assert len(recs) == 1
    rec = recs[0]
    assert isinstance(rec, Recommendation)
    assert rec.task_name == "GatherSampleEvidence.Manta"
    assert rec.recommended_cpu == 4.0 * 1.20
    assert rec.recommended_memory_gib == 16.0 * 1.20
    # Without a declared baseline (see Phase 5 TODO in recommend), reduction
    # percentages remain 0.0. Design §Cost Model → Req 9.3.
    assert rec.cpu_reduction_pct == 0.0
    assert rec.memory_reduction_pct == 0.0


def test_recommend_honors_custom_headroom() -> None:
    """Custom headroom replaces the default 20% factor."""
    recs = recommend(
        [_stats("t", cpu=8.0, mem=32.0, obs=5)],
        headroom=0.50,
    )

    assert len(recs) == 1
    rec = recs[0]
    assert rec.recommended_cpu == 8.0 * 1.50
    assert rec.recommended_memory_gib == 32.0 * 1.50


def test_recommend_mixed_observation_counts_only_includes_ge_three() -> None:
    """Tasks with <3 observations are skipped; ≥3 tasks are emitted in input order."""
    stats = [
        _stats("below", cpu=2.0, mem=4.0, obs=1),
        _stats("threshold", cpu=4.0, mem=8.0, obs=3),
        _stats("also-below", cpu=3.0, mem=6.0, obs=2),
        _stats("above", cpu=16.0, mem=64.0, obs=7),
    ]

    recs = recommend(stats)

    assert [r.task_name for r in recs] == ["threshold", "above"]
    threshold_rec, above_rec = recs
    assert threshold_rec.recommended_cpu == 4.0 * 1.20
    assert threshold_rec.recommended_memory_gib == 8.0 * 1.20
    assert above_rec.recommended_cpu == 16.0 * 1.20
    assert above_rec.recommended_memory_gib == 64.0 * 1.20


def test_recommend_empty_input_returns_empty() -> None:
    """No stats in → no recommendations out."""
    assert recommend([]) == []
