"""Unit tests for the cross-engine divergence comparator."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from gatk_sv_aws.validation.divergence import (
    ArtifactPair,
    diff_artifact,
    diff_pairs,
)


VCF_HEADER = """##fileformat=VCFv4.2
##fileDate=DIFFERS_BETWEEN_RUNS
##source=gatk-sv-RUN-ID-DIFFERS
##contig=<ID=chr1,length=248956422>
##INFO=<ID=SVTYPE,Number=1,Type=String,Description="SV type">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
"""


def _write_vcf(path: Path, records: list[tuple[str, int, str, str]]) -> Path:
    body = "".join(
        f"{c}\t{p}\t{i}\tN\t<{t}>\t.\tPASS\tSVTYPE={t}\n"
        for c, p, i, t in records
    )
    path.write_text(VCF_HEADER + body)
    return path


def test_vcf_normalisation_skips_metadata_lines(tmp_path: Path) -> None:
    """Two VCFs with different ## headers but same body should hash the same."""
    a = _write_vcf(
        tmp_path / "a.vcf",
        [("chr1", 100, "v1", "DEL"), ("chr1", 200, "v2", "DUP")],
    )
    # Identical body, but a different ##source line at the top.
    b = tmp_path / "b.vcf"
    b.write_text(
        VCF_HEADER.replace("gatk-sv-RUN-ID-DIFFERS", "another-run-id")
        + "chr1\t100\tv1\tN\t<DEL>\t.\tPASS\tSVTYPE=DEL\n"
        + "chr1\t200\tv2\tN\t<DUP>\t.\tPASS\tSVTYPE=DUP\n"
    )
    diff = diff_artifact("S1", "manta.vcf", "vcf", a, b)
    assert not diff.diverged
    assert diff.a_hash == diff.b_hash


def test_vcf_normalisation_sorts_records(tmp_path: Path) -> None:
    """Same records emitted in different order should still hash equal."""
    a = _write_vcf(
        tmp_path / "a.vcf",
        [("chr1", 100, "v1", "DEL"), ("chr1", 200, "v2", "DUP")],
    )
    b = _write_vcf(
        tmp_path / "b.vcf",
        [("chr1", 200, "v2", "DUP"), ("chr1", 100, "v1", "DEL")],
    )
    diff = diff_artifact("S1", "manta.vcf", "vcf", a, b)
    assert not diff.diverged


def test_vcf_diverges_on_record_difference(tmp_path: Path) -> None:
    a = _write_vcf(
        tmp_path / "a.vcf", [("chr1", 100, "v1", "DEL")],
    )
    b = _write_vcf(
        tmp_path / "b.vcf", [("chr1", 101, "v1", "DEL")],  # POS differs
    )
    diff = diff_artifact("S1", "manta.vcf", "vcf", a, b)
    assert diff.diverged


def test_tsv_gz_byte_compare(tmp_path: Path) -> None:
    a = tmp_path / "a.txt.gz"
    b = tmp_path / "b.txt.gz"
    payload = b"chr1\t100\tA\nchr1\t200\tB\n"
    with gzip.open(a, "wb") as h:
        h.write(payload)
    with gzip.open(b, "wb") as h:
        h.write(payload)
    diff = diff_artifact("S1", "pe.txt.gz", "tsv_gz", a, b)
    assert not diff.diverged


def test_tsv_gz_diverges(tmp_path: Path) -> None:
    a = tmp_path / "a.txt.gz"
    b = tmp_path / "b.txt.gz"
    with gzip.open(a, "wb") as h:
        h.write(b"chr1\t100\n")
    with gzip.open(b, "wb") as h:
        h.write(b"chr1\t101\n")  # different
    diff = diff_artifact("S1", "pe.txt.gz", "tsv_gz", a, b)
    assert diff.diverged


def test_diff_pairs_aggregates(tmp_path: Path) -> None:
    a = _write_vcf(tmp_path / "a.vcf", [("chr1", 100, "v1", "DEL")])
    b = _write_vcf(tmp_path / "b.vcf", [("chr1", 100, "v1", "DEL")])
    c = tmp_path / "c.txt.gz"
    d = tmp_path / "d.txt.gz"
    with gzip.open(c, "wb") as h:
        h.write(b"x\n")
    with gzip.open(d, "wb") as h:
        h.write(b"y\n")  # diverges
    report = diff_pairs(
        [
            ArtifactPair("S1", "manta", "vcf", a, b),
            ArtifactPair("S1", "pe", "tsv_gz", c, d),
        ]
    )
    assert not report.all_match
    assert len(report.diverged_artifacts) == 1
    assert report.diverged_artifacts[0].artifact == "pe"


def test_unknown_kind_rejected(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.write_text("hi")
    with pytest.raises(ValueError, match="unknown artifact kind"):
        diff_artifact("S1", "x", "bogus", a, a)  # type: ignore[arg-type]
