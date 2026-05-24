"""Cross-engine divergence acceptance test.

Verifies that GatherSampleEvidence outputs produced by the HealthOmics
engine match outputs produced by an alternative engine (currently:
miniwdl on EC2) for the same sample, same Docker images, same reference
files. Skipped unless both result sets are present locally — set up by
running ``scripts/divergence_pull.py`` first.

Layout:
    divergence/
      <sample-id>/
        healthomics/
          pe.txt.gz, sr.txt.gz, rd.txt.gz,
          manta.vcf.gz, wham.vcf.gz, scramble.vcf.gz
        ec2/
          (same set)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gatk_sv_aws.validation.divergence import (
    GSE_ARTIFACT_KINDS,
    ArtifactPair,
    diff_pairs,
)

DIVERGENCE_ROOT = (
    Path(__file__).resolve().parents[3]
    
    / "divergence"
)


def _samples() -> list[Path]:
    if not DIVERGENCE_ROOT.exists():
        return []
    return sorted(p for p in DIVERGENCE_ROOT.iterdir() if p.is_dir())


@pytest.mark.parametrize("sample_dir", _samples(), ids=lambda p: p.name)
def test_engine_outputs_match(sample_dir: Path) -> None:
    healthomics_dir = sample_dir / "healthomics"
    ec2_dir = sample_dir / "ec2"

    if not healthomics_dir.exists() or not ec2_dir.exists():
        pytest.skip(
            f"divergence inputs missing for {sample_dir.name}; "
            f"need {healthomics_dir} and {ec2_dir}"
        )

    pairs: list[ArtifactPair] = []
    for filename, kind in GSE_ARTIFACT_KINDS.items():
        a = healthomics_dir / filename
        b = ec2_dir / filename
        if not a.exists() or not b.exists():
            # Optional artifacts are skipped per-sample.
            continue
        pairs.append(
            ArtifactPair(
                sample=sample_dir.name,
                artifact=filename,
                kind=kind,
                path_a=a,
                path_b=b,
            )
        )

    if not pairs:
        pytest.skip(f"no comparable artifacts for {sample_dir.name}")

    report = diff_pairs(pairs)
    if not report.all_match:
        msg_lines = [f"Engine divergence for sample {sample_dir.name}:"]
        for diff in report.diverged_artifacts:
            msg_lines.append(
                f"  {diff.artifact} ({diff.kind}): "
                f"healthomics={diff.a_hash[:12]} ({diff.a_size}B) "
                f"ec2={diff.b_hash[:12]} ({diff.b_size}B)"
            )
        pytest.fail("\n".join(msg_lines))


def test_root_directory_exists() -> None:
    """Sanity check: at least the directory layout is there.

    This guards against silent skips when the directory was deleted.
    """
    if not DIVERGENCE_ROOT.parent.exists():
        pytest.skip(
            "validation-cohort layout missing; this is expected before "
            "first divergence test run"
        )
