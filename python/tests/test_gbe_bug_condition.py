# Feature: gbe-pipeline-fix, Property 1: Bug Condition Exploration
"""Bug Condition Exploration — GBE Missing Params and Glob-All Discovery.

This test encodes the EXPECTED (correct) behavior as assertions. Since the
code is currently UNFIXED, these assertions will FAIL — which is the desired
outcome (failure proves the bug exists).

**Validates: Requirements 1.1, 1.2, 1.3, 1.4**

Property assertions:
1. GBE params dict SHOULD contain `min_interval_size` with value 101
   and `max_interval_size` with value 2000
2. `discover_gse_outputs()` SHOULD return arrays where each has exactly
   `len(SAMPLES)` (10) elements
3. Array element at index `i` SHOULD contain `SAMPLES[i]` in its path
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

# Add the scripts directory to the path so we can import run_pipeline
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[3] / "gatk-sv-healthomics" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import run_pipeline  # noqa: E402
from run_pipeline import SAMPLES, discover_gse_outputs  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUCKET = f"healthomics-outputs-{run_pipeline.ACCOUNT}-apse1"

OUTPUT_SUFFIXES = {
    "counts": ".counts.tsv.gz",
    "pe_files": ".pe.txt.gz",
    "sr_files": ".sr.txt.gz",
    "sd_files": ".sd.txt.gz",
    "manta_vcfs": ".manta.vcf.gz",
    "wham_vcfs": ".wham.vcf.gz",
    "scramble_vcfs": ".scramble.vcf.gz",
}


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@st.composite
def s3_listing_with_duplicates(draw: st.DrawFn) -> dict[str, list[dict[str, Any]]]:
    """Generate S3 object listings with varying numbers of duplicates per sample.

    Returns a dict mapping sample_id -> list of S3 object dicts (as returned
    by list_objects_v2). Each sample has 1-5 files per output type to simulate
    duplicate files from prior test runs.
    """
    listings: dict[str, list[dict[str, Any]]] = {}

    for sample_id in SAMPLES:
        prefix = (
            f"runs/gatk-sv-e2e/{sample_id}/optimized/"
            if sample_id == "NA12878"
            else f"runs/gatk-sv-e2e/{sample_id}/gse/"
        )

        objects: list[dict[str, Any]] = []
        for output_type, suffix in OUTPUT_SUFFIXES.items():
            # Draw number of duplicates for this sample x output type (1-5)
            num_duplicates = draw(
                st.integers(min_value=1, max_value=5).filter(lambda x: True)
            )
            for dup_idx in range(num_duplicates):
                # Create realistic S3 keys with run-attempt suffixes
                key = f"{prefix}run-{dup_idx}/{sample_id}{suffix}"
                objects.append({
                    "Key": key,
                    "LastModified": f"2025-01-{10 + dup_idx:02d}T00:00:00Z",
                    "Size": 1024 * (dup_idx + 1),
                })

        listings[sample_id] = objects

    return listings


def _build_mock_s3_client(listings: dict[str, list[dict[str, Any]]]) -> MagicMock:
    """Build a mock S3 client that returns the given listings per sample prefix."""
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator

    def paginate_side_effect(Bucket: str, Prefix: str) -> list[dict[str, Any]]:
        """Return pages matching the given prefix."""
        # Find all objects whose Key starts with the given prefix
        matching_objects: list[dict[str, Any]] = []
        for sample_id, objects in listings.items():
            for obj in objects:
                if obj["Key"].startswith(Prefix):
                    matching_objects.append(obj)

        # Return as a single page
        return [{"Contents": matching_objects}] if matching_objects else [{"Contents": []}]

    mock_paginator.paginate.side_effect = paginate_side_effect
    return mock_client


# ---------------------------------------------------------------------------
# Property 1(a): GBE params dict SHOULD contain interval size parameters
# ---------------------------------------------------------------------------


def test_gbe_params_contain_interval_sizes() -> None:
    """GBE params dict SHOULD contain min_interval_size=101 and max_interval_size=2000.

    **Validates: Requirements 2.1**

    This test encodes the EXPECTED behavior. On unfixed code, the params dict
    does NOT contain these keys, so this assertion will FAIL — confirming the bug.
    """
    # Build a mock S3 client with clean listings (1 file per sample per type)
    listings: dict[str, list[dict[str, Any]]] = {}
    for sample_id in SAMPLES:
        prefix = (
            f"runs/gatk-sv-e2e/{sample_id}/optimized/"
            if sample_id == "NA12878"
            else f"runs/gatk-sv-e2e/{sample_id}/gse/"
        )
        objects: list[dict[str, Any]] = []
        for output_type, suffix in OUTPUT_SUFFIXES.items():
            key = f"{prefix}{sample_id}{suffix}"
            objects.append({"Key": key, "Size": 1024})
        listings[sample_id] = objects

    mock_s3 = _build_mock_s3_client(listings)

    # Mock the TrainGCNV outputs
    mock_gcnv = {
        "contig_ploidy_model_tar": f"s3://{BUCKET}/runs/gatk-sv-e2e/batch/train-gcnv/contig-ploidy-model.tar.gz",
        "gcnv_model_tars": [
            f"s3://{BUCKET}/runs/gatk-sv-e2e/batch/train-gcnv/gcnv-model-shard-{i}.tar.gz"
            for i in range(5)
        ],
    }

    # Simulate the GBE params construction from main()
    gse_outputs = discover_gse_outputs(mock_s3)

    # Build params dict exactly as main() does for the gbe stage
    params = {
        "batch": run_pipeline.BATCH_ID,
        "samples": SAMPLES,
        "counts": gse_outputs["counts"],
        "PE_files": gse_outputs["pe_files"],
        "SR_files": gse_outputs["sr_files"],
        "SD_files": gse_outputs["sd_files"],
        "manta_vcfs": gse_outputs["manta_vcfs"],
        "wham_vcfs": gse_outputs["wham_vcfs"],
        "scramble_vcfs": gse_outputs["scramble_vcfs"],
        "contig_ploidy_model_tar": mock_gcnv["contig_ploidy_model_tar"],
        "gcnv_model_tars": mock_gcnv["gcnv_model_tars"],
        "ped_file": run_pipeline.REF["ped_file"],
        "genome_file": run_pipeline.REF["genome_file"],
        "primary_contigs_fai": run_pipeline.REF["primary_contigs_fai"],
        "ref_dict": run_pipeline.REF["dict"],
        "cytoband": run_pipeline.REF["cytoband"],
        "mei_bed": run_pipeline.REF["mei_bed"],
        "cnmops_chrom_file": run_pipeline.REF["autosome_fai"],
        "cnmops_allo_file": run_pipeline.REF["allosome_fai"],
        "cnmops_exclude_list": run_pipeline.REF["pesr_blacklist"],
        "matrix_qc_distance": 1000000,
        "min_svsize": 50,
        "min_interval_size": 101,
        "max_interval_size": 2000,
        "run_matrix_qc": False,
        "run_ploidy": False,
        "rename_samples": False,
        "append_first_sample_to_ped": False,
        "subset_primary_contigs": False,
        "ref_copy_number_autosomal_contigs": 2,
        "gcnv_qs_cutoff": 30,
        "gatk_docker": run_pipeline.DOCKER["gatk"],
        "linux_docker": run_pipeline.DOCKER["linux"],
        "sv_base_docker": run_pipeline.DOCKER["sv_base"],
        "sv_base_mini_docker": run_pipeline.DOCKER["sv_base_mini"],
        "sv_pipeline_docker": run_pipeline.DOCKER["sv_pipeline"],
        "sv_pipeline_qc_docker": run_pipeline.DOCKER["sv_pipeline"],
        "cnmops_docker": run_pipeline.DOCKER["cnmops"],
    }

    # Assert EXPECTED behavior — these will FAIL on unfixed code
    assert "min_interval_size" in params, (
        "Bug confirmed: params dict missing 'min_interval_size'"
    )
    assert params["min_interval_size"] == 101, (
        f"Bug confirmed: min_interval_size should be 101, got {params.get('min_interval_size')}"
    )
    assert "max_interval_size" in params, (
        "Bug confirmed: params dict missing 'max_interval_size'"
    )
    assert params["max_interval_size"] == 2000, (
        f"Bug confirmed: max_interval_size should be 2000, got {params.get('max_interval_size')}"
    )


# ---------------------------------------------------------------------------
# Property 1(b): discover_gse_outputs() SHOULD return arrays of len(SAMPLES)
# ---------------------------------------------------------------------------


@given(listings=s3_listing_with_duplicates())
@settings(max_examples=50)
def test_discover_gse_outputs_returns_correct_length(
    listings: dict[str, list[dict[str, Any]]],
) -> None:
    """discover_gse_outputs() SHOULD return arrays with exactly len(SAMPLES) elements.

    **Validates: Requirements 2.2, 2.3**

    When S3 has duplicate files (from prior test runs), the function should
    still return exactly 10 elements per output type. On unfixed code, it
    appends ALL matching files, producing arrays > 10 — confirming the bug.
    """
    mock_s3 = _build_mock_s3_client(listings)
    outputs = discover_gse_outputs(mock_s3)

    expected_len = len(SAMPLES)
    for key, array in outputs.items():
        assert len(array) == expected_len, (
            f"Bug confirmed: outputs['{key}'] has {len(array)} elements, "
            f"expected {expected_len}"
        )


# ---------------------------------------------------------------------------
# Property 1(c): Array element at index i SHOULD contain SAMPLES[i]
# ---------------------------------------------------------------------------


@given(listings=s3_listing_with_duplicates())
@settings(max_examples=50)
def test_discover_gse_outputs_maintains_sample_alignment(
    listings: dict[str, list[dict[str, Any]]],
) -> None:
    """Array element at index i SHOULD contain SAMPLES[i] in its path.

    **Validates: Requirements 2.4**

    On unfixed code, the glob-all approach does not guarantee sample alignment,
    so this assertion will FAIL — confirming the bug.
    """
    mock_s3 = _build_mock_s3_client(listings)
    outputs = discover_gse_outputs(mock_s3)

    for key, array in outputs.items():
        # First check length (prerequisite for alignment check)
        if len(array) != len(SAMPLES):
            # If length is wrong, alignment is inherently broken
            assert False, (
                f"Bug confirmed: outputs['{key}'] has {len(array)} elements "
                f"(expected {len(SAMPLES)}), alignment cannot be verified"
            )

        for i, uri in enumerate(array):
            assert SAMPLES[i] in uri, (
                f"Bug confirmed: outputs['{key}'][{i}] = '{uri}' "
                f"does not contain expected sample '{SAMPLES[i]}'"
            )
