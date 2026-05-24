"""Fuzz-tolerant SV site comparator.

The exact ``(CHROM, POS, SVTYPE)`` join used by
:func:`gatk_sv_aws.validation.compare_cohort_vcf`
is strict — calls that drift by even 1 bp between two engine runs are
counted as discordant. In practice GATK-SV breakpoint clustering
recomputes representative breakpoints across batches and shards, so
true-equivalent calls can land within tens of bp of each other.

This module provides a pos-fuzz join that is closer to ``bcftools isec``
behaviour: two sites match when they share CHROM and SVTYPE and their
``POS`` values are within ``pos_fuzz_bp`` of each other. The default
``pos_fuzz_bp=50`` follows Broad's published validation tolerances for
short-read SV pipelines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import (
    MIN_CONCORDANCE,
    ConcordanceReport,
    SvType,
    SvTypeConcordance,
    VcfSite,
    iter_sites,
)


def _sites_by_chrom_type(
    sites: Iterable[VcfSite],
) -> dict[tuple[str, SvType], list[int]]:
    """Group sites by (CHROM, SVTYPE), with sorted POS lists for binary search."""
    out: dict[tuple[str, SvType], list[int]] = {}
    for s in sites:
        out.setdefault((s.chrom, s.svtype), []).append(s.pos)
    for key in out:
        out[key].sort()
    return out


def _has_match_within(
    sorted_positions: list[int], pos: int, fuzz: int
) -> bool:
    """Return True if ``sorted_positions`` contains any value within ``fuzz`` of ``pos``."""
    if not sorted_positions:
        return False
    # binary search for closest element
    lo, hi = 0, len(sorted_positions) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_positions[mid] < pos:
            lo = mid + 1
        else:
            hi = mid
    candidates: list[int] = []
    if lo < len(sorted_positions):
        candidates.append(sorted_positions[lo])
    if lo - 1 >= 0:
        candidates.append(sorted_positions[lo - 1])
    return any(abs(c - pos) <= fuzz for c in candidates)


def compare_cohort_vcf_fuzzy(
    produced: Path,
    expected: Path,
    *,
    pos_fuzz_bp: int = 50,
) -> ConcordanceReport:
    """Concordance with ``±pos_fuzz_bp`` tolerance on POS.

    For each ``(CHROM, SVTYPE)`` group, an expected site is "matched"
    when the produced VCF has any site of the same type within
    ``pos_fuzz_bp`` bp. Concordance per type is then
    ``|matched expected| / |expected|``.
    """
    expected_sites = list(iter_sites(expected))
    produced_sites = list(iter_sites(produced))
    produced_index = _sites_by_chrom_type(produced_sites)

    matched: list[VcfSite] = []
    unmatched_expected: list[VcfSite] = []
    for s in expected_sites:
        positions = produced_index.get((s.chrom, s.svtype), [])
        if _has_match_within(positions, s.pos, pos_fuzz_bp):
            matched.append(s)
        else:
            unmatched_expected.append(s)

    # Symmetric: produced sites with no expected match within fuzz
    expected_index = _sites_by_chrom_type(expected_sites)
    unmatched_produced: list[VcfSite] = []
    for s in produced_sites:
        positions = expected_index.get((s.chrom, s.svtype), [])
        if not _has_match_within(positions, s.pos, pos_fuzz_bp):
            unmatched_produced.append(s)

    per_type: list[SvTypeConcordance] = []
    for svtype in ("DEL", "DUP", "INS", "INV"):
        e_count = sum(1 for s in expected_sites if s.svtype == svtype)
        p_count = sum(1 for s in produced_sites if s.svtype == svtype)
        i_count = sum(1 for s in matched if s.svtype == svtype)
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

    discordant = tuple(
        sorted(
            unmatched_expected + unmatched_produced,
            key=lambda s: s.key,
        )
    )
    return ConcordanceReport(per_type=tuple(per_type), discordant_sites=discordant)


__all__ = ["compare_cohort_vcf_fuzzy"]
