"""Acceptance test: produced VCF concordance with expected (Req 13.2, 13.3).

Skipped unless ``RUN_ACCEPTANCE_TESTS=1`` and a produced VCF + expected
VCF exist at the conventional paths.

The produced cohort VCF for the validation-2026q2 cohort lives in S3 at:
    s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/batch/
        mcv-remaining-steps-ec2/cleaned_vcf/
        gatk-sv-validation-2026q2.cleaned.vcf.gz

The expected (Broad-reference) cohort VCF must be produced by running
the upstream GATK-SV pipeline on Terra against the same 10 samples; see
``docs/validation-runbook.md`` Step 1. Once produced
and uploaded, the test driver expects it at:
    validation-cohort/expected/expected.vcf.gz
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gatk_sv_aws.validation import (
    assert_concordance_gates,
    compare_cohort_vcf,
)
from gatk_sv_aws.validation.fuzzy import (
    compare_cohort_vcf_fuzzy,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PRODUCED = (
    PROJECT_ROOT
    
    / "validation-cohort"
    / "produced"
    / "cohort.vcf.gz"
)
EXPECTED = (
    PROJECT_ROOT
    
    / "validation-cohort"
    / "expected"
    / "expected.vcf.gz"
)


def _require_inputs() -> None:
    if not PRODUCED.exists():
        pytest.skip(
            f"produced VCF not found at {PRODUCED}; download with:\n"
            f"  aws s3 cp s3://healthomics-outputs-__ACCOUNT_ID__-apse1/"
            f"runs/gatk-sv-e2e/batch/mcv-remaining-steps-ec2/cleaned_vcf/"
            f"gatk-sv-validation-2026q2.cleaned.vcf.gz {PRODUCED}"
        )
    if not EXPECTED.exists():
        pytest.skip(
            f"expected VCF not found at {EXPECTED}; produce the Broad "
            f"reference per docs/validation-runbook.md Step 1."
        )


def test_cohort_vcf_meets_concordance_gates_strict() -> None:
    """Strict (CHROM, POS, SVTYPE) join."""
    _require_inputs()
    report = compare_cohort_vcf(PRODUCED, EXPECTED)
    assert_concordance_gates(report)


def test_cohort_vcf_meets_concordance_gates_fuzzy_50bp() -> None:
    """Fuzz-tolerant join with ±50 bp window — closer to Broad's standard."""
    _require_inputs()
    report = compare_cohort_vcf_fuzzy(PRODUCED, EXPECTED, pos_fuzz_bp=50)
    assert_concordance_gates(report)
