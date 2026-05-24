"""Cross-engine divergence comparator (HealthOmics vs EC2/Cromwell/etc.).

Cromwell-on-Terra and HealthOmics both interpret the same WDL but their
file localisation, working-directory layout and bind-mount semantics
differ. The Migration_System claims results match upstream "for a fixed
seed and fixed inputs" (Req 2.4). This module operationalises the claim
for the GatherSampleEvidence outputs (the per-sample evidence that
downstream cohort-scope modules consume).

For each per-sample artifact category (PE, SR, RD, BAF, Manta VCF, Wham
VCF, Scramble VCF) the comparator picks a normalisation function
appropriate to the file format:

* ``vcf`` — body-only md5: skip every line beginning with ``##`` (header
  metadata that includes timestamps, command lines, run-IDs that vary
  by engine), keep ``#CHROM`` line + records, sort records by
  ``(CHROM, POS, ID)``, hash.
* ``tsv_gz`` — full md5 of decompressed body. PE/SR/RD evidence files
  are deterministic at the byte level once decompressed.
* ``txt`` — full md5.

Two artifacts are considered "divergent" when their normalised hashes
differ. The report enumerates every divergent artifact with the two
hashes and the artifact category.
"""

from __future__ import annotations

import gzip
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal

ArtifactKind = Literal["vcf", "tsv_gz", "txt"]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _normalise_vcf(path: Path) -> bytes:
    """Return canonical body of a VCF: header line + sorted records, no metadata."""
    opener = gzip.open if path.suffix == ".gz" else open
    keep_meta_prefix = b"#CHROM"
    records: list[bytes] = []
    chrom_line: bytes | None = None
    with opener(path, "rb") as handle:  # type: ignore[operator]
        for raw in handle:
            line = raw.rstrip(b"\r\n")
            if line.startswith(b"##"):
                continue
            if line.startswith(keep_meta_prefix):
                chrom_line = line
                continue
            if not line:
                continue
            records.append(line)

    def _sort_key(rec: bytes) -> tuple[bytes, int, bytes]:
        cols = rec.split(b"\t", 4)
        chrom = cols[0]
        try:
            pos = int(cols[1])
        except (IndexError, ValueError):
            pos = 0
        rec_id = cols[2] if len(cols) > 2 else b""
        return chrom, pos, rec_id

    records.sort(key=_sort_key)
    out = (chrom_line + b"\n" if chrom_line else b"") + b"\n".join(records) + b"\n"
    return out


def _normalise_tsv_gz(path: Path) -> bytes:
    """Stream-decompress a .gz file and return its full body bytes."""
    with gzip.open(path, "rb") as handle:
        return handle.read()


def _normalise_txt(path: Path) -> bytes:
    return path.read_bytes()


_NORMALISERS: dict[ArtifactKind, Callable[[Path], bytes]] = {
    "vcf": _normalise_vcf,
    "tsv_gz": _normalise_tsv_gz,
    "txt": _normalise_txt,
}


# ---------------------------------------------------------------------------
# Per-artifact and per-sample diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactDivergence:
    """Per-artifact divergence record."""

    sample: str
    artifact: str
    kind: ArtifactKind
    a_hash: str
    b_hash: str
    a_size: int
    b_size: int

    @property
    def diverged(self) -> bool:
        return self.a_hash != self.b_hash


def _hash(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324


def diff_artifact(
    sample: str,
    artifact: str,
    kind: ArtifactKind,
    path_a: Path,
    path_b: Path,
) -> ArtifactDivergence:
    """Hash both files using the kind's normaliser and return the comparison."""
    if kind not in _NORMALISERS:
        raise ValueError(f"unknown artifact kind: {kind}")
    norm = _NORMALISERS[kind]
    body_a = norm(path_a)
    body_b = norm(path_b)
    return ArtifactDivergence(
        sample=sample,
        artifact=artifact,
        kind=kind,
        a_hash=_hash(body_a),
        b_hash=_hash(body_b),
        a_size=len(body_a),
        b_size=len(body_b),
    )


@dataclass(frozen=True)
class DivergenceReport:
    """Aggregate divergence report across one or more samples and artifacts."""

    artifacts: tuple[ArtifactDivergence, ...]

    @property
    def diverged_artifacts(self) -> tuple[ArtifactDivergence, ...]:
        return tuple(a for a in self.artifacts if a.diverged)

    @property
    def all_match(self) -> bool:
        return not self.diverged_artifacts

    def by_sample(self, sample: str) -> tuple[ArtifactDivergence, ...]:
        return tuple(a for a in self.artifacts if a.sample == sample)


# ---------------------------------------------------------------------------
# Plan helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactPair:
    """A single artifact pair to compare across the two engines."""

    sample: str
    artifact: str
    kind: ArtifactKind
    path_a: Path
    path_b: Path


def diff_pairs(pairs: Iterable[ArtifactPair]) -> DivergenceReport:
    """Run :func:`diff_artifact` over every pair and aggregate."""
    rows: list[ArtifactDivergence] = []
    for p in pairs:
        rows.append(
            diff_artifact(
                sample=p.sample,
                artifact=p.artifact,
                kind=p.kind,
                path_a=p.path_a,
                path_b=p.path_b,
            )
        )
    return DivergenceReport(artifacts=tuple(rows))


# Canonical GatherSampleEvidence per-sample artifact set. Used by the
# acceptance test in tests/gatk_sv_aws/acceptance/test_engine_divergence.py
# to drive a deterministic comparison.
GSE_ARTIFACT_KINDS: dict[str, ArtifactKind] = {
    "pe.txt.gz": "tsv_gz",
    "sr.txt.gz": "tsv_gz",
    "rd.txt.gz": "tsv_gz",
    "manta.vcf.gz": "vcf",
    "wham.vcf.gz": "vcf",
    "scramble.vcf.gz": "vcf",
}


__all__ = [
    "ArtifactKind",
    "ArtifactDivergence",
    "ArtifactPair",
    "DivergenceReport",
    "diff_artifact",
    "diff_pairs",
    "GSE_ARTIFACT_KINDS",
]
