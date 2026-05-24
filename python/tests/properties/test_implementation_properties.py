# Feature: gatk-sv-healthomics-migration, Task 2.11: Supplementary implementation-layer properties
"""Supplementary implementation-layer property tests.

One Hypothesis test per row of Design §Testing Strategy → Property-Based
Tests. These cover behaviors that are subsumed by the ten top-level
correctness properties or are narrow implementation details, but still
benefit from property-based coverage:

* WDL version acceptance (Req 2.1)
* gs:// URI rejection at packaging (Req 2.6)
* STATIC storage sizing (Req 8.1, 8.2, 8.3)
* Right-sizing recommender (Req 9.2, 9.3)
* Retry classifier & backoff (Req 15.1, 15.3)
* Static task declaration check (Req 9.1, 9.5)
* Event schema completeness (Req 14.1, 14.2)
* Output presence verifier (Req 7.4, 7.5)

All tests are RED until the corresponding implementation tasks (3.1.*,
3.7.*, 3.8.*, 3.9.*) land.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given, strategies as st

from gatk_sv_aws.cost import (
    Recommendation,
    TaskStats,
    recommend,
)
from gatk_sv_aws.monitoring import (
    emit_run_finished,
    emit_run_started,
)
from gatk_sv_aws.orchestrator import (
    RETRYABLE_ERROR_CODES,
    choose_storage,
    classify_retry,
    verify_outputs,
)
from gatk_sv_aws.packager import (
    UnsupportedWdlVersionError,
    WdlTree,
    check_wdl_version,
    reject_gcs_uris,
)


# ---------------------------------------------------------------------------
# WDL version acceptance (Req 2.1)
# ---------------------------------------------------------------------------


@given(version=st.sampled_from(["draft-2", "1.0", "1.1", "1.2", "2.0"]))
def test_impl_prop_wdl_version_acceptance(version: str) -> None:
    """check_wdl_version accepts iff version in {1.0, 1.1}."""

    accepted_versions = {"1.0", "1.1"}
    if version in accepted_versions:
        check_wdl_version(version)  # must not raise
    else:
        with pytest.raises(UnsupportedWdlVersionError):
            check_wdl_version(version)


# ---------------------------------------------------------------------------
# gs:// URI rejection (Req 2.6)
# ---------------------------------------------------------------------------


_uri_scheme = st.sampled_from(["gs", "s3", "https", "file"])
_uri_path = st.from_regex(r"\A[A-Za-z0-9][A-Za-z0-9/_.-]{1,30}\Z", fullmatch=True)


@st.composite
def wdl_tree_with_uris(draw: st.DrawFn) -> tuple[WdlTree, int]:
    """Generate a WdlTree with a known number of gs:// URIs embedded."""
    n_gs = draw(st.integers(min_value=0, max_value=3))
    n_other = draw(st.integers(min_value=0, max_value=3))
    paths = []
    for _ in range(n_gs):
        paths.append(f"gs://bucket/{draw(_uri_path)}")
    for _ in range(n_other):
        scheme = draw(st.sampled_from(["s3", "https"]))
        paths.append(f"{scheme}://bucket/{draw(_uri_path)}")
    tree = WdlTree(source="(synthetic)", input_paths=paths)
    return tree, n_gs


@given(data=wdl_tree_with_uris())
def test_impl_prop_gs_uri_rejection(data) -> None:  # type: ignore[no-untyped-def]
    tree, expected_gs = data
    violations = reject_gcs_uris(tree)
    assert len(violations) == expected_gs
    for v in violations:
        assert v.offending_uri.startswith("gs://")


# ---------------------------------------------------------------------------
# STATIC storage sizing (Req 8.1, 8.2, 8.3)
# ---------------------------------------------------------------------------

ONE_TIB = 1024**4


@given(
    total_input_bytes=st.integers(min_value=0, max_value=16 * ONE_TIB),
    peak_working_set_gib=st.floats(min_value=0.1, max_value=50000.0, allow_nan=False),
)
def test_impl_prop_storage_sizing(
    total_input_bytes: int, peak_working_set_gib: float
) -> None:
    choice = choose_storage(total_input_bytes, peak_working_set_gib)
    if total_input_bytes <= ONE_TIB:
        assert choice.storage_type == "DYNAMIC"
        assert choice.storage_capacity_gib is None
    else:
        assert choice.storage_type == "STATIC"
        capacity = choice.storage_capacity_gib
        assert capacity is not None
        # Must be at least 1200 and a multiple of 1200.
        assert capacity >= 1200
        assert capacity % 1200 == 0
        # Must satisfy ``capacity >= ceil(peak * 1.20 / 1200) * 1200``.
        required = max(1200, math.ceil(peak_working_set_gib * 1.20 / 1200) * 1200)
        assert capacity >= required


# ---------------------------------------------------------------------------
# Right-sizing recommender (Req 9.2, 9.3)
# ---------------------------------------------------------------------------


@given(
    cpu=st.floats(min_value=0.5, max_value=64, allow_nan=False),
    mem=st.floats(min_value=0.5, max_value=256, allow_nan=False),
    obs_count=st.integers(min_value=1, max_value=10),
)
def test_impl_prop_recommender(cpu: float, mem: float, obs_count: int) -> None:
    stats = [
        TaskStats(
            task_name="t",
            observed_peak_cpu=cpu,
            observed_peak_memory_gib=mem,
            observation_count=obs_count,
        )
    ]
    recs = recommend(stats)
    if obs_count < 3:
        assert recs == []
    else:
        assert len(recs) == 1
        rec: Recommendation = recs[0]
        assert rec.recommended_cpu >= cpu * 1.20 - 1e-9
        assert rec.recommended_memory_gib >= mem * 1.20 - 1e-9


# ---------------------------------------------------------------------------
# Retry classifier & backoff (Req 15.1, 15.3)
# ---------------------------------------------------------------------------


@given(
    error_code=st.sampled_from(
        sorted(RETRYABLE_ERROR_CODES)
        + ["ValidationException", "AccessDenied", "ResourceNotFound"]
    ),
    attempt=st.integers(min_value=1, max_value=5),
)
def test_impl_prop_retry_classifier(error_code: str, attempt: int) -> None:
    decision = classify_retry(error_code, attempt)
    if error_code in RETRYABLE_ERROR_CODES and attempt < 3:
        assert decision.should_retry is True
        assert decision.delay_seconds >= 30.0
        assert decision.delay_seconds <= 8 * 60
    else:
        assert decision.should_retry is False


# ---------------------------------------------------------------------------
# Event schema completeness (Req 14.1, 14.2)
# ---------------------------------------------------------------------------


_ident = st.from_regex(r"\A[A-Za-z][A-Za-z0-9_-]{0,15}\Z", fullmatch=True)


@given(run_id=_ident, cohort_id=_ident)
def test_impl_prop_event_started_schema(run_id: str, cohort_id: str) -> None:
    evt = emit_run_started(run_id, cohort_id, {"p": 1})
    for key in ("run_id", "cohort_id", "parameters"):
        assert key in evt


@given(
    run_id=_ident,
    status=st.sampled_from(["COMPLETED", "FAILED"]),
    wall=st.integers(min_value=0, max_value=60 * 60 * 24),
    cost=st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
)
def test_impl_prop_event_finished_schema(
    run_id: str, status: str, wall: int, cost: float
) -> None:
    evt = emit_run_finished(run_id, status, wall, cost)
    for key in ("run_id", "status", "wall_clock_sec", "cost_usd"):
        assert key in evt


# ---------------------------------------------------------------------------
# Output presence verifier (Req 7.4, 7.5)
# ---------------------------------------------------------------------------


_filename = st.from_regex(r"\A[A-Za-z0-9_-]{1,10}\.vcf(\.gz)?\Z", fullmatch=True)


@given(
    declared=st.lists(_filename, min_size=1, max_size=5, unique=True),
    missing_count=st.integers(min_value=0, max_value=5),
)
def test_impl_prop_output_verifier(declared: list[str], missing_count: int) -> None:
    missing_count = min(missing_count, len(declared))
    present = declared[missing_count:]
    report = verify_outputs(declared, present)
    if missing_count == 0:
        assert report.status == "COMPLETED"
        assert report.missing == ()
    else:
        assert report.status == "FAILED"
        assert set(report.missing) == set(declared[:missing_count])
