"""Unit tests for the Module_Phase boundary plumbing in
``scripts/run_cohort_e2e.py`` (Req 19.1–19.6, Req 14.1–14.2).

These tests cover Task 8.8: the four ``--skip-phase-{a,b,c,d}`` flags,
the phase classification helper, and the prerequisite-enforcement
helper that blocks downstream phases when an upstream phase fails.

The orchestrator module is imported via ``importlib.util.spec_from_file_location``
because ``scripts/`` is not part of the installed Python package and the
script reads ``AWS_ACCOUNT_ID`` at import time.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ORCHESTRATOR_PATH = _REPO_ROOT / "scripts" / "run_cohort_e2e.py"


@pytest.fixture(scope="module")
def orch():
    """Import scripts/run_cohort_e2e.py as a module.

    The script requires AWS_ACCOUNT_ID at import time (it builds the run
    role ARN). We set a dummy account id so import succeeds without an
    AWS profile.
    """
    os.environ.setdefault("AWS_ACCOUNT_ID", "000000000000")
    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
    spec = importlib.util.spec_from_file_location(
        "run_cohort_e2e", str(_ORCHESTRATOR_PATH)
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_cohort_e2e"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# CLI parser: every Module_Phase boundary has a --skip-phase-* flag (Req 19)
# ---------------------------------------------------------------------------


def test_cli_exposes_four_phase_skip_flags(orch) -> None:
    parser = orch._build_parser()
    args = parser.parse_args([
        "--cohort-id", "cohort-x",
        "--skip-phase-a", "--skip-phase-b",
        "--skip-phase-c", "--skip-phase-d",
    ])
    assert args.skip_phase_a is True
    assert args.skip_phase_b is True
    assert args.skip_phase_c is True
    assert args.skip_phase_d is True


def test_cli_phase_skip_flags_default_off(orch) -> None:
    parser = orch._build_parser()
    args = parser.parse_args(["--cohort-id", "cohort-x"])
    assert args.skip_phase_a is False
    assert args.skip_phase_b is False
    assert args.skip_phase_c is False
    assert args.skip_phase_d is False


def test_cli_legacy_skip_flags_remain(orch) -> None:
    """Backward-compat: pre-existing fine-grained flags must still parse."""
    parser = orch._build_parser()
    args = parser.parse_args([
        "--cohort-id", "cohort-x",
        "--skip-evidence-qc",
        "--skip-post-processing",
        "--skip-main-vcf-qc",
        "--include-visualize-cnvs",
    ])
    assert args.skip_evidence_qc is True
    assert args.skip_post_processing is True
    assert args.skip_main_vcf_qc is True
    assert args.include_visualize_cnvs is True


# ---------------------------------------------------------------------------
# _classify_phase routes every known stage label into the right boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("GSE:manta:S1", "A"),
        ("scramble_ec2:S2", "A"),
        ("evidence_qc", "A"),
        ("gather_batch_evidence", "B"),
        ("genotype_batch", "B"),
        ("regenotype_cnvs", "B"),
        ("combinebatches_ec2", "C"),
        ("remaining_steps_ec2", "C"),
        ("refine_complex_variants", "C"),
        ("filter_genotypes", "C"),
        ("annotate_vcf", "D"),
        ("main_vcf_qc", "D"),
        ("visualize_cnvs", "D"),
        ("not_a_real_stage", ""),
    ],
)
def test_classify_phase(orch, stage, expected) -> None:
    assert orch._classify_phase(stage) == expected


# ---------------------------------------------------------------------------
# _summarize_phase rolls a list of StageRecord into {status, records_count}
# ---------------------------------------------------------------------------


def _record(orch, stage: str, status: str):
    return orch.StageRecord(
        stage=stage,
        kind="healthomics",
        id="run-id",
        name=f"{stage}-1",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T01:00:00Z",
        status=status,
        duration_sec=3600.0,
    )


def test_summarize_phase_no_records_is_not_started(orch) -> None:
    summary = orch._summarize_phase([], "B")
    assert summary["status"] == "NOT_STARTED"
    assert summary["records_count"] == 0


def test_summarize_phase_all_completed(orch) -> None:
    records = [
        _record(orch, "gather_batch_evidence", "COMPLETED"),
        _record(orch, "cluster_batch", "COMPLETED"),
    ]
    summary = orch._summarize_phase(records, "B")
    assert summary["status"] == "COMPLETED"
    assert summary["records_count"] == 2


def test_summarize_phase_all_skipped(orch) -> None:
    records = [_record(orch, "evidence_qc", "SKIPPED")]
    summary = orch._summarize_phase(records, "A")
    assert summary["status"] == "SKIPPED"


def test_summarize_phase_one_failed_marks_phase_failed(orch) -> None:
    records = [
        _record(orch, "gather_batch_evidence", "COMPLETED"),
        _record(orch, "cluster_batch", "FAILED"),
    ]
    summary = orch._summarize_phase(records, "B")
    assert summary["status"] == "FAILED"
    assert summary["error_status"] == "FAILED"


# ---------------------------------------------------------------------------
# _check_phase_prereq blocks B when A failed and was not explicitly skipped
# ---------------------------------------------------------------------------


def test_phase_b_blocked_when_phase_a_failed(orch) -> None:
    phase_outcomes = {"A": {"status": "FAILED", "records_count": 1}}
    with pytest.raises(RuntimeError, match="Phase A=FAILED"):
        orch._check_phase_prereq(phase_outcomes, current="B", required=["A"])


def test_phase_b_proceeds_when_phase_a_completed(orch) -> None:
    phase_outcomes = {"A": {"status": "COMPLETED", "records_count": 3}}
    # Should NOT raise.
    orch._check_phase_prereq(phase_outcomes, current="B", required=["A"])


def test_phase_b_proceeds_when_phase_a_skipped(orch) -> None:
    """Operator-acknowledged skip (--skip-phase-a) must satisfy the prereq."""
    phase_outcomes = {"A": {"status": "SKIPPED", "reason": "--skip-phase-a"}}
    orch._check_phase_prereq(phase_outcomes, current="B", required=["A"])


def test_phase_c_blocked_when_phase_b_not_started(orch) -> None:
    """A phase that never started (no records) is treated as a blocker."""
    phase_outcomes: dict = {}
    with pytest.raises(RuntimeError, match="Phase B=NOT_STARTED"):
        orch._check_phase_prereq(phase_outcomes, current="C", required=["B"])


def test_prereq_error_mentions_phase_skip_flag(orch) -> None:
    """The error message must guide the operator to --skip-phase-c (etc.)."""
    phase_outcomes = {"C": {"status": "FAILED"}}
    with pytest.raises(RuntimeError, match="--skip-phase-c"):
        orch._check_phase_prereq(phase_outcomes, current="D", required=["C"])
