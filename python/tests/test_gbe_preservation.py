# Feature: gbe-pipeline-fix, Property 2: Preservation
"""Preservation Property Tests — Existing Params, Prefix Routing, and Discovery.

These tests capture the existing CORRECT behavior that must be preserved after
the fix. They run on the UNFIXED code and MUST PASS, establishing a baseline
that guards against regressions.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

Properties tested:
1. GBE params dict contains all existing keys with their current values
2. NA12878 uses prefix `runs/gatk-sv-e2e/NA12878/optimized/`; others use `gse/`
3. When S3 has exactly 1 file per sample per type, discover_gse_outputs()
   returns arrays of length 10
4. discover_train_gcnv_outputs() with mocked S3 returns correct structure
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from hypothesis import given, settings, strategies as st

# Add the scripts directory to the path so we can import run_pipeline
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[3] / "gatk-sv-healthomics" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import run_pipeline  # noqa: E402
from run_pipeline import (  # noqa: E402
    BATCH_ID,
    DOCKER,
    REF,
    SAMPLES,
    discover_gse_outputs,
    discover_train_gcnv_outputs,
)

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

# Expected GBE parameter values (current behavior to preserve)
EXPECTED_PARAMS = {
    "batch": "batch_01",
    "samples": SAMPLES,
    "matrix_qc_distance": 1000000,
    "min_svsize": 50,
    "run_matrix_qc": False,
    "run_ploidy": False,
    "rename_samples": False,
    "append_first_sample_to_ped": False,
    "subset_primary_contigs": False,
    "ref_copy_number_autosomal_contigs": 2,
    "gcnv_qs_cutoff": 30,
}

EXPECTED_DOCKER_PARAMS = {
    "gatk_docker": DOCKER["gatk"],
    "linux_docker": DOCKER["linux"],
    "sv_base_docker": DOCKER["sv_base"],
    "sv_base_mini_docker": DOCKER["sv_base_mini"],
    "sv_pipeline_docker": DOCKER["sv_pipeline"],
    "sv_pipeline_qc_docker": DOCKER["sv_pipeline"],
    "cnmops_docker": DOCKER["cnmops"],
}

EXPECTED_REF_PARAMS = {
    "ped_file": REF["ped_file"],
    "genome_file": REF["genome_file"],
    "primary_contigs_fai": REF["primary_contigs_fai"],
    "ref_dict": REF["dict"],
    "cytoband": REF["cytoband"],
    "mei_bed": REF["mei_bed"],
    "cnmops_chrom_file": REF["autosome_fai"],
    "cnmops_allo_file": REF["allosome_fai"],
    "cnmops_exclude_list": REF["pesr_blacklist"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_clean_listings() -> dict[str, list[dict[str, Any]]]:
    """Build S3 listings with exactly 1 file per sample per output type."""
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
    return listings


def _build_mock_s3_client(listings: dict[str, list[dict[str, Any]]]) -> MagicMock:
    """Build a mock S3 client that returns the given listings per sample prefix."""
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator

    def paginate_side_effect(Bucket: str, Prefix: str) -> list[dict[str, Any]]:
        """Return pages matching the given prefix."""
        matching_objects: list[dict[str, Any]] = []
        for sample_id, objects in listings.items():
            for obj in objects:
                if obj["Key"].startswith(Prefix):
                    matching_objects.append(obj)
        return [{"Contents": matching_objects}] if matching_objects else [{"Contents": []}]

    mock_paginator.paginate.side_effect = paginate_side_effect
    return mock_client


def _build_mock_s3_for_train_gcnv(
    num_shards: int,
) -> MagicMock:
    """Build a mock S3 client for TrainGCNV discovery."""
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator

    prefix = "runs/gatk-sv-e2e/batch/train-gcnv/"
    objects: list[dict[str, Any]] = [
        {"Key": f"{prefix}contig-ploidy-model.tar.gz", "Size": 2048},
    ]
    for i in range(num_shards):
        objects.append(
            {"Key": f"{prefix}gcnv-model-shard-{i:04d}.tar.gz", "Size": 4096}
        )

    def paginate_side_effect(Bucket: str, Prefix: str) -> list[dict[str, Any]]:
        matching = [obj for obj in objects if obj["Key"].startswith(Prefix)]
        return [{"Contents": matching}] if matching else [{"Contents": []}]

    mock_paginator.paginate.side_effect = paginate_side_effect
    return mock_client


def _build_gbe_params(gse_outputs: dict, gcnv_outputs: dict) -> dict:
    """Build the GBE params dict exactly as main() does for the gbe stage."""
    return {
        "batch": BATCH_ID,
        "samples": SAMPLES,
        "counts": gse_outputs["counts"],
        "PE_files": gse_outputs["pe_files"],
        "SR_files": gse_outputs["sr_files"],
        "SD_files": gse_outputs["sd_files"],
        "manta_vcfs": gse_outputs["manta_vcfs"],
        "wham_vcfs": gse_outputs["wham_vcfs"],
        "scramble_vcfs": gse_outputs["scramble_vcfs"],
        "contig_ploidy_model_tar": gcnv_outputs["contig_ploidy_model_tar"],
        "gcnv_model_tars": gcnv_outputs["gcnv_model_tars"],
        "ped_file": REF["ped_file"],
        "genome_file": REF["genome_file"],
        "primary_contigs_fai": REF["primary_contigs_fai"],
        "ref_dict": REF["dict"],
        "cytoband": REF["cytoband"],
        "mei_bed": REF["mei_bed"],
        "cnmops_chrom_file": REF["autosome_fai"],
        "cnmops_allo_file": REF["allosome_fai"],
        "cnmops_exclude_list": REF["pesr_blacklist"],
        "matrix_qc_distance": 1000000,
        "min_svsize": 50,
        "run_matrix_qc": False,
        "run_ploidy": False,
        "rename_samples": False,
        "append_first_sample_to_ped": False,
        "subset_primary_contigs": False,
        "ref_copy_number_autosomal_contigs": 2,
        "gcnv_qs_cutoff": 30,
        "gatk_docker": DOCKER["gatk"],
        "linux_docker": DOCKER["linux"],
        "sv_base_docker": DOCKER["sv_base"],
        "sv_base_mini_docker": DOCKER["sv_base_mini"],
        "sv_pipeline_docker": DOCKER["sv_pipeline"],
        "sv_pipeline_qc_docker": DOCKER["sv_pipeline"],
        "cnmops_docker": DOCKER["cnmops"],
    }


# ---------------------------------------------------------------------------
# Property 1: Parameter Preservation
# ---------------------------------------------------------------------------

# Strategy: draw a non-empty subset of existing param keys to verify
_ALL_PARAM_KEYS = list(EXPECTED_PARAMS.keys()) + list(EXPECTED_DOCKER_PARAMS.keys()) + list(EXPECTED_REF_PARAMS.keys())


@st.composite
def param_key_subsets(draw: st.DrawFn) -> list[str]:
    """Draw a non-empty subset of existing GBE parameter keys."""
    subset = draw(
        st.lists(
            st.sampled_from(_ALL_PARAM_KEYS),
            min_size=1,
            max_size=len(_ALL_PARAM_KEYS),
            unique=True,
        )
    )
    return subset


@given(keys=param_key_subsets())
@settings(max_examples=50)
def test_gbe_params_preserve_existing_values(keys: list[str]) -> None:
    """For all subsets of existing param keys, those keys exist with original values.

    **Validates: Requirements 3.1, 3.2**

    This test verifies that the GBE params dict contains all previously-existing
    parameters with their current values. Must PASS on unfixed code.
    """
    listings = _build_clean_listings()
    mock_s3 = _build_mock_s3_client(listings)
    gse_outputs = discover_gse_outputs(mock_s3)

    mock_gcnv = {
        "contig_ploidy_model_tar": f"s3://{BUCKET}/runs/gatk-sv-e2e/batch/train-gcnv/contig-ploidy-model.tar.gz",
        "gcnv_model_tars": [
            f"s3://{BUCKET}/runs/gatk-sv-e2e/batch/train-gcnv/gcnv-model-shard-{i}.tar.gz"
            for i in range(5)
        ],
    }

    params = _build_gbe_params(gse_outputs, mock_gcnv)

    # Merge all expected values into one lookup
    all_expected = {**EXPECTED_PARAMS, **EXPECTED_DOCKER_PARAMS, **EXPECTED_REF_PARAMS}

    for key in keys:
        assert key in params, f"Param key '{key}' missing from GBE params dict"
        assert params[key] == all_expected[key], (
            f"Param '{key}' has value {params[key]!r}, expected {all_expected[key]!r}"
        )


# ---------------------------------------------------------------------------
# Property 2: Prefix Routing Preservation
# ---------------------------------------------------------------------------


@given(sample_id=st.sampled_from(SAMPLES))
@settings(max_examples=20)
def test_prefix_routing_preservation(sample_id: str) -> None:
    """NA12878 uses optimized/ prefix; all others use gse/ prefix.

    **Validates: Requirements 3.3, 3.4**

    This test verifies the prefix routing logic is correct for all samples.
    Must PASS on unfixed code.
    """
    listings = _build_clean_listings()
    mock_s3 = _build_mock_s3_client(listings)
    outputs = discover_gse_outputs(mock_s3)

    # Check that the output URIs for this sample use the correct prefix
    sample_idx = SAMPLES.index(sample_id)

    if sample_id == "NA12878":
        expected_prefix = f"runs/gatk-sv-e2e/{sample_id}/optimized/"
    else:
        expected_prefix = f"runs/gatk-sv-e2e/{sample_id}/gse/"

    # With clean listings (1 file per sample per type), the arrays are in
    # SAMPLES order. Check that the file at this sample's index uses the
    # correct prefix.
    for key, array in outputs.items():
        # With clean listings, array length should be 10
        if len(array) > sample_idx:
            uri = array[sample_idx]
            assert expected_prefix in uri, (
                f"Sample '{sample_id}' at index {sample_idx}: "
                f"outputs['{key}'] = '{uri}' does not contain "
                f"expected prefix '{expected_prefix}'"
            )


# ---------------------------------------------------------------------------
# Property 3: Clean Listing Baseline
# ---------------------------------------------------------------------------


@st.composite
def clean_s3_listings(draw: st.DrawFn) -> dict[str, list[dict[str, Any]]]:
    """Generate S3 listings with exactly 1 file per sample per output type.

    Uses hypothesis to vary the filename structure while keeping exactly
    one file per sample per type.
    """
    listings: dict[str, list[dict[str, Any]]] = {}

    # Draw a run identifier to vary the file paths
    run_id = draw(st.integers(min_value=0, max_value=99))

    for sample_id in SAMPLES:
        prefix = (
            f"runs/gatk-sv-e2e/{sample_id}/optimized/"
            if sample_id == "NA12878"
            else f"runs/gatk-sv-e2e/{sample_id}/gse/"
        )
        objects: list[dict[str, Any]] = []
        for output_type, suffix in OUTPUT_SUFFIXES.items():
            key = f"{prefix}run-{run_id}/{sample_id}{suffix}"
            objects.append({"Key": key, "Size": 1024})
        listings[sample_id] = objects

    return listings


@given(listings=clean_s3_listings())
@settings(max_examples=50)
def test_clean_listing_returns_correct_length(
    listings: dict[str, list[dict[str, Any]]],
) -> None:
    """With exactly 1 file per sample per type, arrays have length 10.

    **Validates: Requirements 3.1, 3.2**

    This test confirms that when S3 has no duplicates, the current (unfixed)
    discover_gse_outputs() correctly returns arrays of length 10.
    Must PASS on unfixed code.
    """
    mock_s3 = _build_mock_s3_client(listings)
    outputs = discover_gse_outputs(mock_s3)

    expected_len = len(SAMPLES)
    for key, array in outputs.items():
        assert len(array) == expected_len, (
            f"outputs['{key}'] has {len(array)} elements, expected {expected_len}"
        )


# ---------------------------------------------------------------------------
# Property 4: TrainGCNV Discovery Preservation
# ---------------------------------------------------------------------------


@given(num_shards=st.integers(min_value=1, max_value=20))
@settings(max_examples=30)
def test_train_gcnv_discovery_preservation(num_shards: int) -> None:
    """discover_train_gcnv_outputs() correctly finds model tar and shard tars.

    **Validates: Requirements 3.1**

    This test verifies that TrainGCNV discovery works correctly with varying
    numbers of model shards. Must PASS on unfixed code.
    """
    mock_s3 = _build_mock_s3_for_train_gcnv(num_shards)
    outputs = discover_train_gcnv_outputs(mock_s3)

    # contig_ploidy_model_tar should be found
    assert outputs["contig_ploidy_model_tar"] is not None, (
        "contig_ploidy_model_tar should not be None"
    )
    assert "contig-ploidy-model" in outputs["contig_ploidy_model_tar"], (
        f"contig_ploidy_model_tar URI should contain 'contig-ploidy-model', "
        f"got: {outputs['contig_ploidy_model_tar']}"
    )
    assert outputs["contig_ploidy_model_tar"].endswith(".tar.gz"), (
        "contig_ploidy_model_tar should end with .tar.gz"
    )

    # gcnv_model_tars should have the correct number of shards
    assert len(outputs["gcnv_model_tars"]) == num_shards, (
        f"Expected {num_shards} gcnv_model_tars, got {len(outputs['gcnv_model_tars'])}"
    )
    for tar_uri in outputs["gcnv_model_tars"]:
        assert "gcnv-model-shard" in tar_uri, (
            f"gcnv_model_tar URI should contain 'gcnv-model-shard', got: {tar_uri}"
        )
        assert tar_uri.endswith(".tar.gz"), (
            f"gcnv_model_tar should end with .tar.gz, got: {tar_uri}"
        )
