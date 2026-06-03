"""Unit tests for the Phase B.1 (GatherBatchEvidence) dispatch script.

The dispatch script lives at the repo root as ``.dispatch-phase-b-gbe.py``
(leading dot keeps it grouped with other one-off dispatch scripts and
matches the pattern set by ``.dispatch-evidence-qc.py`` /
``.dispatch-scramble-serial.py``). Its filename starts with a dot, so a
plain ``import`` won't work — we load it via ``importlib.util``.

These tests pin the post-mortem fix for run 8799664 (Phase B.1 GBE that
failed in cn.MOPS CNSampleNormal because the staged ped file
``s3://omics-ref-<region>-<account>/gatk-sv/reference/GRCh38/cohort.ped``
listed only the validation cohort, not the smoke cohort, leaving the male
sample list empty after the awk filter on the sex column).

The fix overrides ``params["ped_file"]`` to a smoke-cohort-specific staged
copy. This file verifies:

* ``SAMPLES`` has exactly 10 entries (the smoke cohort).
* ``_build_gbe_parameters({}, fake_paths)`` overrides ``ped_file`` to the
  smoke-cohort URI even though the template already had a value.
* ``params["batch"] == "gatk-sv-156-smoke-test"``.
* All seven per-sample arrays have length 10.
* ``params["samples"]`` is a list equal to ``SAMPLES`` (and not the same
  object — defensive copy).

Together these prevent regression of the post-mortem fix.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DISPATCH_PATH = _REPO_ROOT / ".dispatch-phase-b-gbe.py"


@pytest.fixture(scope="module")
def dispatch():
    """Import ``.dispatch-phase-b-gbe.py`` as a module via importlib.

    The file's leading-dot name prevents normal ``import`` resolution.
    The module imports ``boto3`` at top-level, but no client is created
    at import time (only inside ``main()``), so the import is side-effect
    free.
    """
    os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
    spec = importlib.util.spec_from_file_location(
        "dispatch_phase_b_gbe", str(_DISPATCH_PATH)
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["dispatch_phase_b_gbe"] = module
    spec.loader.exec_module(module)
    return module


def _fake_paths(samples: list[str]) -> dict[str, dict[str, str]]:
    """Build a placeholder per-sample paths dict matching the contract of
    ``_build_gse_paths_per_sample``: ``{sample: {field: s3_uri}}``.
    The actual URIs don't matter — ``_build_gbe_parameters`` only iterates
    them in SAMPLES order to build the per-sample arrays.
    """
    return {
        sid: {
            "counts":   f"s3://bucket/{sid}/counts.tsv.gz",
            "manta":    f"s3://bucket/{sid}/manta.vcf.gz",
            "wham":     f"s3://bucket/{sid}/wham.vcf.gz",
            "scramble": f"s3://bucket/{sid}/scramble.vcf.gz",
            "pe":       f"s3://bucket/{sid}/pe.txt.gz",
            "sr":       f"s3://bucket/{sid}/sr.txt.gz",
            "sd":       f"s3://bucket/{sid}/sd.txt.gz",
        }
        for sid in samples
    }


def test_samples_has_exactly_ten_entries(dispatch) -> None:
    """The smoke cohort is fixed at 10 samples; size drift would silently
    break the per-sample array assertions in ``main()``.
    """
    assert isinstance(dispatch.SAMPLES, list)
    assert len(dispatch.SAMPLES) == 10
    assert dispatch.SAMPLES == [
        "HG00096", "HG00129", "HG00140", "HG00150", "HG00187",
        "HG00239", "HG00277", "HG00288", "HG00337", "HG00349",
    ]


def test_smoke_ped_uri_is_cohort_specific(dispatch) -> None:
    """The constant must point at the smoke-cohort ped, NOT the original
    validation cohort ped (which only listed validation samples and caused
    run 8799664 to fail in cn.MOPS CNSampleNormal).
    """
    assert dispatch.SMOKE_PED_URI.endswith(
        "/gatk-sv/reference/GRCh38/cohort-gatk-sv-156-smoke-test.ped"
    )
    assert dispatch.SMOKE_PED_URI.startswith("s3://omics-ref-")
    # Defensive: the old ped basename must NOT appear in the override URI.
    assert "/cohort.ped" not in dispatch.SMOKE_PED_URI


def test_build_gbe_parameters_overrides_ped_file(dispatch) -> None:
    """Even if the template carries a different ped_file value, the
    builder must overwrite it with ``SMOKE_PED_URI``. This is the core
    post-mortem regression guard.
    """
    template: dict[str, Any] = {
        # The validation-cohort ped that broke run 8799664.
        "ped_file": (
            "s3://omics-ref-ap-southeast-1-123456789012/gatk-sv/reference/"
            "GRCh38/cohort.ped"
        ),
        # A few unrelated globals to confirm they survive untouched.
        "ref_dict": "s3://refs/GRCh38.dict",
        "gcnv_qs_cutoff": 30,
        "run_matrix_qc": False,
    }
    params = dispatch._build_gbe_parameters(template, _fake_paths(dispatch.SAMPLES))

    assert params["ped_file"] == dispatch.SMOKE_PED_URI
    # template not mutated in place (defensive copy).
    assert template["ped_file"].endswith("/cohort.ped")
    # globals preserved.
    assert params["ref_dict"] == "s3://refs/GRCh38.dict"
    assert params["gcnv_qs_cutoff"] == 30
    assert params["run_matrix_qc"] is False


def test_build_gbe_parameters_sets_smoke_batch_id(dispatch) -> None:
    """The batch label drives every output prefix; it MUST be the smoke id."""
    params = dispatch._build_gbe_parameters({}, _fake_paths(dispatch.SAMPLES))
    assert params["batch"] == "gatk-sv-156-smoke-test"
    assert params["batch"] == dispatch.COHORT_ID


def test_build_gbe_parameters_per_sample_arrays_are_length_ten(dispatch) -> None:
    """All seven per-sample arrays must align with SAMPLES (length 10)
    and preserve SAMPLES ordering. Misaligned arrays would feed mismatched
    files to the wrong sample inside the WDL.
    """
    params = dispatch._build_gbe_parameters({}, _fake_paths(dispatch.SAMPLES))
    for fld in ("counts", "manta_vcfs", "wham_vcfs", "scramble_vcfs",
                "PE_files", "SR_files", "SD_files"):
        assert len(params[fld]) == 10, f"{fld} length != 10"
    # Per-sample ordering: each array's i-th element must reference the
    # i-th sample id.
    for i, sid in enumerate(dispatch.SAMPLES):
        assert sid in params["counts"][i]
        assert sid in params["manta_vcfs"][i]
        assert sid in params["wham_vcfs"][i]
        assert sid in params["scramble_vcfs"][i]
        assert sid in params["PE_files"][i]
        assert sid in params["SR_files"][i]
        assert sid in params["SD_files"][i]


def test_build_gbe_parameters_samples_equals_constant(dispatch) -> None:
    """``params["samples"]`` must equal SAMPLES (same content, defensive
    copy so callers can't mutate the module-level constant).
    """
    params = dispatch._build_gbe_parameters({}, _fake_paths(dispatch.SAMPLES))
    assert params["samples"] == dispatch.SAMPLES
    # defensive copy: mutating params["samples"] must not touch SAMPLES.
    params["samples"].append("HG-EXTRA")
    assert "HG-EXTRA" not in dispatch.SAMPLES
