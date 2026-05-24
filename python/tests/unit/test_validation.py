"""Unit tests for the GATK-SV Validation Harness (Design §Components.j).

Covers :func:`compare_cohort_vcf`, :func:`assert_concordance_gates`, and
:func:`validation_cost_report`. Uses tiny synthetic VCFs written to
``tmp_path`` (Req 13.2, 13.3, 13.4, 13.5).
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from gatk_sv_aws.validation import (
    ConcordanceError,
    assert_concordance_gates,
    compare_cohort_vcf,
    iter_sites,
    validation_cost_report,
)


def _write_vcf(path: Path, records: list[tuple[str, int, str]]) -> None:
    """Write a minimal VCF with one line per ``(chrom, pos, svtype)`` tuple."""
    lines = ["##fileformat=VCFv4.2", "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO"]
    for chrom, pos, svtype in records:
        lines.append(
            f"{chrom}\t{pos}\tid\tN\t<{svtype}>\t.\tPASS\tSVTYPE={svtype};END={pos+100}"
        )
    path.write_text("\n".join(lines) + "\n")


def test_iter_sites_reads_plain_vcf(tmp_path: Path) -> None:
    vcf = tmp_path / "a.vcf"
    _write_vcf(vcf, [("chr1", 100, "DEL"), ("chr1", 500, "DUP")])

    sites = list(iter_sites(vcf))
    assert len(sites) == 2
    assert sites[0].chrom == "chr1"
    assert sites[0].pos == 100
    assert sites[0].svtype == "DEL"


def test_iter_sites_reads_gzipped_vcf(tmp_path: Path) -> None:
    vcf_gz = tmp_path / "a.vcf.gz"
    contents = (
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr1\t100\tid\tN\t<DEL>\t.\tPASS\tSVTYPE=DEL;END=200\n"
    )
    with gzip.open(vcf_gz, "wt") as handle:
        handle.write(contents)

    sites = list(iter_sites(vcf_gz))
    assert len(sites) == 1
    assert sites[0].svtype == "DEL"


def test_perfect_concordance_passes_gates(tmp_path: Path) -> None:
    records = [("chr1", 100, "DEL"), ("chr1", 500, "DUP"), ("chr2", 200, "INS")]
    produced = tmp_path / "produced.vcf"
    expected = tmp_path / "expected.vcf"
    _write_vcf(produced, records)
    _write_vcf(expected, records)

    report = compare_cohort_vcf(produced, expected)

    assert report.overall_pass is True
    for row in report.per_type:
        if row.expected_count > 0:
            assert row.concordance == 1.0


def test_partial_concordance_fails_gate(tmp_path: Path) -> None:
    expected_records = [("chr1", i * 100, "DEL") for i in range(1, 101)]  # 100 DELs
    produced_records = expected_records[:50]  # miss half — concordance 0.50
    produced = tmp_path / "produced.vcf"
    expected = tmp_path / "expected.vcf"
    _write_vcf(produced, produced_records)
    _write_vcf(expected, expected_records)

    report = compare_cohort_vcf(produced, expected)

    del_row = report.by_type("DEL")
    assert del_row is not None
    assert del_row.concordance == 0.5
    assert del_row.pass_gate is False

    with pytest.raises(ConcordanceError, match="DEL"):
        assert_concordance_gates(report)


def test_ins_gate_95_percent(tmp_path: Path) -> None:
    # 100 expected INS, 96 produced → 0.96, above 0.95 gate.
    expected_records = [("chr1", i * 100, "INS") for i in range(1, 101)]
    produced_records = expected_records[:96]
    produced = tmp_path / "produced.vcf"
    expected = tmp_path / "expected.vcf"
    _write_vcf(produced, produced_records)
    _write_vcf(expected, expected_records)

    report = compare_cohort_vcf(produced, expected)
    ins_row = report.by_type("INS")
    assert ins_row is not None
    assert ins_row.pass_gate is True
    assert report.overall_pass is True


def test_ins_gate_94_percent_fails(tmp_path: Path) -> None:
    # 100 expected INS, 94 produced → 0.94, below 0.95 gate.
    expected_records = [("chr1", i * 100, "INS") for i in range(1, 101)]
    produced_records = expected_records[:94]
    produced = tmp_path / "produced.vcf"
    expected = tmp_path / "expected.vcf"
    _write_vcf(produced, produced_records)
    _write_vcf(expected, expected_records)

    report = compare_cohort_vcf(produced, expected)
    ins_row = report.by_type("INS")
    assert ins_row is not None
    assert ins_row.pass_gate is False
    with pytest.raises(ConcordanceError):
        assert_concordance_gates(report)


def test_validation_cost_report_under_target() -> None:
    report = validation_cost_report(
        cohort_id="validation-cohort", total_cost_usd=60.0, sample_count=10
    )
    assert report.per_sample_cost_usd == 6.0
    assert report.over_target is False
    assert report.recommendations == ()


def test_validation_cost_report_over_target_surfaces_recommendations() -> None:
    report = validation_cost_report(
        cohort_id="validation-cohort",
        total_cost_usd=100.0,
        sample_count=10,
        recommendations=("right-size ClusterBatch.cpu from 16 to 8",),
    )
    assert report.per_sample_cost_usd == 10.0
    assert report.over_target is True
    assert len(report.recommendations) == 1


def test_validation_cost_report_rejects_zero_samples() -> None:
    with pytest.raises(ValueError, match="sample_count"):
        validation_cost_report("c", total_cost_usd=10.0, sample_count=0)
