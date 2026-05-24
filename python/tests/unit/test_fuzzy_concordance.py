"""Unit tests for the fuzz-tolerant SV site comparator."""

from __future__ import annotations

from pathlib import Path

import pytest

from gatk_sv_aws.validation.fuzzy import (
    compare_cohort_vcf_fuzzy,
)


VCF_HEADER = """##fileformat=VCFv4.2
##INFO=<ID=SVTYPE,Number=1,Type=String,Description="SV type">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
"""


def _write_vcf(path: Path, rows: list[tuple[str, int, str]]) -> Path:
    body = "".join(
        f"{chrom}\t{pos}\tx\tN\t<{svtype}>\t.\tPASS\tSVTYPE={svtype}\n"
        for chrom, pos, svtype in rows
    )
    path.write_text(VCF_HEADER + body)
    return path


def test_exact_match_within_default_fuzz(tmp_path: Path) -> None:
    expected = _write_vcf(
        tmp_path / "expected.vcf",
        [("chr1", 1000, "DEL"), ("chr1", 5000, "DUP")],
    )
    produced = _write_vcf(
        tmp_path / "produced.vcf",
        [("chr1", 1000, "DEL"), ("chr1", 5000, "DUP")],
    )
    report = compare_cohort_vcf_fuzzy(produced, expected)
    for row in report.per_type:
        if row.expected_count > 0:
            assert row.concordance == 1.0


def test_match_within_fuzz_window(tmp_path: Path) -> None:
    expected = _write_vcf(
        tmp_path / "expected.vcf",
        [("chr1", 1000, "DEL")],
    )
    produced = _write_vcf(
        tmp_path / "produced.vcf",
        [("chr1", 1042, "DEL")],  # 42 bp drift
    )
    report = compare_cohort_vcf_fuzzy(produced, expected, pos_fuzz_bp=50)
    del_row = report.by_type("DEL")
    assert del_row is not None
    assert del_row.concordance == 1.0
    assert report.discordant_sites == ()


def test_no_match_outside_fuzz_window(tmp_path: Path) -> None:
    expected = _write_vcf(
        tmp_path / "expected.vcf",
        [("chr1", 1000, "DEL")],
    )
    produced = _write_vcf(
        tmp_path / "produced.vcf",
        [("chr1", 1200, "DEL")],  # 200 bp drift > fuzz=50
    )
    report = compare_cohort_vcf_fuzzy(produced, expected, pos_fuzz_bp=50)
    del_row = report.by_type("DEL")
    assert del_row is not None
    assert del_row.concordance == 0.0
    # Both sites are reported as discordant (one missing, one extra).
    chroms = {s.chrom for s in report.discordant_sites}
    assert chroms == {"chr1"}


def test_svtype_must_match(tmp_path: Path) -> None:
    expected = _write_vcf(
        tmp_path / "expected.vcf",
        [("chr1", 1000, "DEL")],
    )
    produced = _write_vcf(
        tmp_path / "produced.vcf",
        [("chr1", 1010, "DUP")],  # within fuzz on POS but wrong svtype
    )
    report = compare_cohort_vcf_fuzzy(produced, expected, pos_fuzz_bp=50)
    del_row = report.by_type("DEL")
    assert del_row is not None
    assert del_row.concordance == 0.0


def test_zero_expected_yields_one(tmp_path: Path) -> None:
    expected = _write_vcf(tmp_path / "expected.vcf", [])
    produced = _write_vcf(tmp_path / "produced.vcf", [])
    report = compare_cohort_vcf_fuzzy(produced, expected)
    for row in report.per_type:
        assert row.concordance == 1.0


def test_zero_expected_with_extras_yields_zero(tmp_path: Path) -> None:
    expected = _write_vcf(tmp_path / "expected.vcf", [])
    produced = _write_vcf(
        tmp_path / "produced.vcf", [("chr1", 100, "DEL")]
    )
    report = compare_cohort_vcf_fuzzy(produced, expected)
    del_row = report.by_type("DEL")
    assert del_row is not None
    assert del_row.concordance == 0.0


@pytest.mark.parametrize("svtype,gate", [
    ("DEL", 0.99), ("DUP", 0.99), ("INS", 0.95), ("INV", 0.95),
])
def test_concordance_gates_drive_pass_flag(
    tmp_path: Path, svtype: str, gate: float
) -> None:
    """Exactly hitting the gate threshold is a pass."""
    n = 100
    n_match = int(n * gate)
    expected_rows = [("chr1", 100 + i * 1000, svtype) for i in range(n)]
    produced_rows = [
        ("chr1", 100 + i * 1000, svtype) for i in range(n_match)
    ] + [
        # Extras far away — won't match anything in expected.
        ("chr2", 100_000_000 + i * 1_000_000, svtype)
        for i in range(n - n_match)
    ]
    expected = _write_vcf(tmp_path / "expected.vcf", expected_rows)
    produced = _write_vcf(tmp_path / "produced.vcf", produced_rows)
    report = compare_cohort_vcf_fuzzy(produced, expected, pos_fuzz_bp=50)
    row = report.by_type(svtype)
    assert row is not None
    assert row.pass_gate is True
