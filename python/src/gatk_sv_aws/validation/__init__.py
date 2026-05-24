"""Component (j): Validation Harness for the GATK-SV migration.

Implements design §Components and interfaces → (j) Validation Harness.
Drives a documented ≤10-sample cohort end-to-end, compares the produced
Cohort_VCF against the expected Cohort_VCF for SV site concordance
(≥99% for DEL/DUP, ≥95% for INS/INV), and reports the measured dollar
cost and per-sample cost. When the per-sample cost exceeds
Per_Sample_Cost_Target, the harness includes the Cost_Optimizer
recommendations that would close the gap.

Advances Requirement 13 (Validation Run at Small Scale).

The concordance gate is deliberately schema-independent: we parse VCF
records as plain text (one per line starting with ``#``) and consider two
records "the same site" when they share ``(CHROM, POS, SVTYPE)``. This is
the same site-level join ``bcftools isec`` performs and it survives
caller-specific INFO-field differences (e.g. ``END`` may be 0 for
symbolic BND alleles). ``SVTYPE`` is read from the ``INFO`` column.
"""

from __future__ import annotations

import gzip
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

# Per-SV-type concordance gates (Req 13.3).
MIN_CONCORDANCE = {
    "DEL": 0.99,
    "DUP": 0.99,
    "INS": 0.95,
    "INV": 0.95,
}


# ---------------------------------------------------------------------------
# VCF parsing
# ---------------------------------------------------------------------------


SvType = Literal["DEL", "DUP", "INS", "INV", "BND", "CNV", "OTHER"]


@dataclass(frozen=True)
class VcfSite:
    """A minimal SV site record used for concordance joins."""

    chrom: str
    pos: int
    svtype: SvType

    @property
    def key(self) -> tuple[str, int, SvType]:
        return (self.chrom, self.pos, self.svtype)


def _open_vcf(path: Path) -> io.TextIOBase:
    """Open a ``.vcf`` or ``.vcf.gz`` in text mode."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt")
    return path.open("r")


def _parse_info_svtype(info: str) -> SvType:
    for field_ in info.split(";"):
        if field_.startswith("SVTYPE="):
            raw = field_.split("=", 1)[1].upper()
            if raw in {"DEL", "DUP", "INS", "INV", "BND", "CNV"}:
                return raw  # type: ignore[return-value]
            return "OTHER"
    return "OTHER"


def iter_sites(path: Path) -> Iterable[VcfSite]:
    """Yield one :class:`VcfSite` per non-header line in ``path``.

    Skips header (``#...``) and blank lines. Fails cleanly on truncated
    files (too few columns) by skipping the offending line — callers
    should validate VCF integrity separately.
    """
    with _open_vcf(path) as handle:
        for line in handle:
            if not line or line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 8:
                continue
            chrom = cols[0]
            try:
                pos = int(cols[1])
            except ValueError:
                continue
            svtype = _parse_info_svtype(cols[7])
            yield VcfSite(chrom=chrom, pos=pos, svtype=svtype)


# ---------------------------------------------------------------------------
# Concordance report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SvTypeConcordance:
    """Concordance statistics for one SV type."""

    svtype: SvType
    produced_count: int
    expected_count: int
    intersection_count: int
    concordance: float

    @property
    def pass_gate(self) -> bool:
        threshold = MIN_CONCORDANCE.get(str(self.svtype))
        if threshold is None:
            return True  # no declared gate → pass by default
        return self.concordance >= threshold


@dataclass(frozen=True)
class ConcordanceReport:
    """Aggregate concordance report across SV types."""

    per_type: tuple[SvTypeConcordance, ...]
    discordant_sites: tuple[VcfSite, ...] = field(default_factory=tuple)

    @property
    def overall_pass(self) -> bool:
        return all(row.pass_gate for row in self.per_type)

    def by_type(self, svtype: str) -> SvTypeConcordance | None:
        for row in self.per_type:
            if row.svtype == svtype:
                return row
        return None


def compare_cohort_vcf(produced: Path, expected: Path) -> ConcordanceReport:
    """Compute per-SV-type concordance between two cohort VCFs.

    Implementation target of Task 3.10.1 (Req 13.2).

    Concordance is computed as ``|produced ∩ expected| / |expected|`` at
    the granularity of ``(CHROM, POS, SVTYPE)``. When ``|expected|`` is
    zero for a type, concordance is reported as ``1.0`` iff
    ``|produced|`` is also zero, else ``0.0``.
    """
    expected_sites = set(iter_sites(expected))
    produced_sites = set(iter_sites(produced))

    intersection = expected_sites & produced_sites
    discordant = (expected_sites ^ produced_sites)

    per_type: list[SvTypeConcordance] = []
    for svtype in ("DEL", "DUP", "INS", "INV"):
        e_count = sum(1 for s in expected_sites if s.svtype == svtype)
        p_count = sum(1 for s in produced_sites if s.svtype == svtype)
        i_count = sum(1 for s in intersection if s.svtype == svtype)
        if e_count == 0:
            concordance = 1.0 if p_count == 0 else 0.0
        else:
            concordance = i_count / e_count
        per_type.append(
            SvTypeConcordance(
                svtype=svtype,  # type: ignore[arg-type]
                produced_count=p_count,
                expected_count=e_count,
                intersection_count=i_count,
                concordance=concordance,
            )
        )

    return ConcordanceReport(
        per_type=tuple(per_type),
        discordant_sites=tuple(sorted(discordant, key=lambda s: s.key)),
    )


class ConcordanceError(AssertionError):
    """Raised when a concordance gate is not met."""


def assert_concordance_gates(report: ConcordanceReport) -> None:
    """Fail with the list of discordant sites when any gate is below threshold.

    Implementation target of Task 3.10.2 (Req 13.3).
    """
    failing = [row for row in report.per_type if not row.pass_gate]
    if not failing:
        return

    lines = ["Concordance gate failure:"]
    for row in failing:
        threshold = MIN_CONCORDANCE[str(row.svtype)]
        lines.append(
            f"  {row.svtype}: {row.concordance:.4f} < {threshold:.4f} "
            f"(produced {row.produced_count}, expected {row.expected_count}, "
            f"intersection {row.intersection_count})"
        )
    if report.discordant_sites:
        lines.append("Discordant sites:")
        for site in report.discordant_sites[:20]:
            lines.append(f"  {site.chrom}:{site.pos} {site.svtype}")
        if len(report.discordant_sites) > 20:
            lines.append(f"  ... and {len(report.discordant_sites) - 20} more")
    raise ConcordanceError("\n".join(lines))


# ---------------------------------------------------------------------------
# Cost aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationCostReport:
    """Result of :func:`validation_cost_report`.

    ``recommendations`` is populated only when the cohort is over target.
    """

    cohort_id: str
    total_cost_usd: float
    sample_count: int
    per_sample_cost_usd: float
    target_usd: float
    over_target: bool
    recommendations: tuple[str, ...] = field(default_factory=tuple)


def validation_cost_report(
    cohort_id: str,
    total_cost_usd: float,
    sample_count: int,
    *,
    target_usd: float = 7.00,
    recommendations: tuple[str, ...] = (),
) -> ValidationCostReport:
    """Build a :class:`ValidationCostReport` from measured totals (Req 13.4, 13.5)."""
    if sample_count <= 0:
        raise ValueError("sample_count must be ≥ 1")
    per_sample = total_cost_usd / sample_count
    over = per_sample > target_usd
    return ValidationCostReport(
        cohort_id=cohort_id,
        total_cost_usd=total_cost_usd,
        sample_count=sample_count,
        per_sample_cost_usd=per_sample,
        target_usd=target_usd,
        over_target=over,
        recommendations=recommendations if over else (),
    )


__all__ = [
    "MIN_CONCORDANCE",
    "SvType",
    "VcfSite",
    "iter_sites",
    "SvTypeConcordance",
    "ConcordanceReport",
    "ConcordanceError",
    "compare_cohort_vcf",
    "assert_concordance_gates",
    "ValidationCostReport",
    "validation_cost_report",
]
