"""Unit tests for the launcher blockers fixed for Task 8.10's smoke test.

Covers three blockers identified during Phase A preflight against the
``cohorts/gatk-sv-156/`` cohort:

* Blocker 1 — ``scripts/run_gse_cohort_tagged.py`` previously redefined
  ``sample_files`` after its parameterised version, masking the
  ``cohort_base`` override and forcing every run at the hardcoded 2026q2
  prefix. The single ``sample_files(sample_id, cohort_base=None)``
  definition plus a ``--cohort-base`` CLI flag let the launcher target
  arbitrary staged cohorts.
* Blocker 2 — covered indirectly by tests on ``_derive_cohorts_prefix``
  and ``_gse_runs_by_sample_module`` (the helpers the
  ``phase_a5_scramble_ec2`` env block uses to build per-sample
  ``COHORTS_PREFIX`` / ``COUNTS_S3`` / ``MANTA_S3`` lines).
* Blocker 3 — ``_swap_uris`` in ``scripts/run_cohort_e2e.py`` needs a
  two-step substitution to handle template-run URIs that embed the long
  ``gatk-sv-validation-2026q2-rerun-2026-05-25`` rerun string. The fix
  is enforced at the orchestrator level (``_start_cohort_module`` calls
  ``_swap_uris`` twice); these tests exercise the underlying behaviour.

The scripts are imported via ``importlib.util`` because ``scripts/`` is
not a Python package and the orchestrator reads ``AWS_ACCOUNT_ID`` at
import time.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TAGGED_PATH = _REPO_ROOT / "scripts" / "run_gse_cohort_tagged.py"
_ORCH_PATH = _REPO_ROOT / "scripts" / "run_cohort_e2e.py"


def _load_script(path: Path, module_name: str):
    os.environ.setdefault("AWS_ACCOUNT_ID", "000000000000")
    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tagged():
    return _load_script(_TAGGED_PATH, "run_gse_cohort_tagged")


@pytest.fixture(scope="module")
def orch():
    return _load_script(_ORCH_PATH, "run_cohort_e2e")


# ---------------------------------------------------------------------------
# Blocker 1 — run_gse_cohort_tagged.py
# ---------------------------------------------------------------------------
def test_tagged_parser_exposes_cohort_base_flag(tagged) -> None:
    parser = tagged._build_parser()
    args = parser.parse_args([
        "--cohort-id", "gatk-sv-156-smoke",
        "--cohort-base", "s3://test-bucket/cohorts/gatk-sv-156",
    ])
    assert args.cohort_base == "s3://test-bucket/cohorts/gatk-sv-156"


def test_tagged_parser_cohort_base_default_is_none(tagged) -> None:
    parser = tagged._build_parser()
    args = parser.parse_args(["--cohort-id", "gatk-sv-156-smoke"])
    assert args.cohort_base is None


def test_tagged_sample_files_uses_override(tagged) -> None:
    """``sample_files`` must respect the ``cohort_base`` override."""
    out = tagged.sample_files("HG00096", cohort_base="s3://test/c1")
    assert out["cram"] == "s3://test/c1/HG00096.final.cram"
    assert out["crai"] == "s3://test/c1/HG00096.final.cram.crai"


def test_tagged_sample_files_falls_back_to_constant(tagged) -> None:
    """Without an override, ``sample_files`` falls back to ``COHORT_BASE``."""
    out = tagged.sample_files("HG00096")
    assert out["cram"].startswith(tagged.COHORT_BASE)
    assert out["cram"].endswith("/HG00096.final.cram")


def test_tagged_no_duplicate_sample_files_definition(tagged) -> None:
    """Regression: a duplicate parameterless definition would shadow the
    override, breaking cross-cohort runs. Source must contain exactly one
    ``def sample_files(`` line."""
    src = _TAGGED_PATH.read_text()
    occurrences = src.count("def sample_files(")
    assert occurrences == 1, (
        f"Expected exactly one `def sample_files(` definition, found {occurrences}. "
        "A duplicate definition would shadow the cohort_base override."
    )


def test_tagged_build_params_threads_cohort_base(tagged) -> None:
    """``build_params`` must forward ``cohort_base`` to ``sample_files``."""
    params = tagged.build_params(
        "manta", "HG00096", cohort_base="s3://test/c2"
    )
    assert params["cram_or_bam"] == "s3://test/c2/HG00096.final.cram"
    assert params["cram_or_bam_idx"] == "s3://test/c2/HG00096.final.cram.crai"


# ---------------------------------------------------------------------------
# Blocker 3 — _swap_uris must collapse both cohort strings
# ---------------------------------------------------------------------------
def test_swap_uris_replaces_short_cohort_id(orch) -> None:
    params = {"key": "s3://x/cohorts/gatk-sv-validation-2026q2/foo"}
    out = orch._swap_uris(params, "gatk-sv-validation-2026q2", "abc")
    assert out["key"] == "s3://x/cohorts/abc/foo"


def test_swap_uris_chained_replaces_long_then_short(orch) -> None:
    """Templates embed the rerun string ``...-rerun-2026-05-25`` in S3 URIs.

    The ``_start_cohort_module`` callsite chains two ``_swap_uris`` calls —
    long string first, short string second — so both forms collapse to the
    new cohort_id rather than leaving a non-existent
    ``<cohort_id>-rerun-2026-05-25/`` path behind.
    """
    params = {
        "key": "s3://x/cohorts/gatk-sv-validation-2026q2-rerun-2026-05-25/foo",
    }
    # First substitute the long rerun string, then the short one.
    out = orch._swap_uris(
        params, "gatk-sv-validation-2026q2-rerun-2026-05-25", "abc"
    )
    out = orch._swap_uris(out, "gatk-sv-validation-2026q2", "abc")
    assert out["key"] == "s3://x/cohorts/abc/foo"
    assert "rerun-2026-05-25" not in out["key"]


def test_swap_uris_chained_handles_short_form_too(orch) -> None:
    """Chained substitution is idempotent on URIs that only have the short form."""
    params = {"key": "s3://x/cohorts/gatk-sv-validation-2026q2/foo"}
    out = orch._swap_uris(
        params, "gatk-sv-validation-2026q2-rerun-2026-05-25", "abc"
    )
    out = orch._swap_uris(out, "gatk-sv-validation-2026q2", "abc")
    assert out["key"] == "s3://x/cohorts/abc/foo"


# ---------------------------------------------------------------------------
# Blocker 2 — orchestrator helpers fed into the SSM env block
# ---------------------------------------------------------------------------
def test_orch_parser_exposes_cohort_base_flag(orch) -> None:
    parser = orch._build_parser()
    args = parser.parse_args([
        "--cohort-id", "gatk-sv-156-smoke",
        "--cohort-base", "s3://test-bucket/cohorts/gatk-sv-156",
    ])
    assert args.cohort_base == "s3://test-bucket/cohorts/gatk-sv-156"


def test_derive_cohorts_prefix_from_override(orch, tmp_path) -> None:
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text("{}")

    class A:
        cohort_base = None

    args = A()
    args.manifest = str(manifest_path)
    out = orch._derive_cohorts_prefix(
        args,
        cohort_base_override="s3://omics-cohorts-ap-southeast-1-1234/cohorts/gatk-sv-156",
    )
    assert out == "cohorts/gatk-sv-156"


def test_derive_cohorts_prefix_from_manifest_destination_base(orch, tmp_path) -> None:
    manifest = tmp_path / "m.json"
    manifest.write_text(
        '{"destination_base": '
        '"s3://omics-cohorts-ap-southeast-1-__ACCOUNT_ID__/cohorts/gatk-sv-156"}'
    )

    class A:
        cohort_base = None

    args = A()
    args.manifest = str(manifest)
    out = orch._derive_cohorts_prefix(args)
    assert out == "cohorts/gatk-sv-156"


def test_derive_cohorts_prefix_legacy_default(orch, tmp_path) -> None:
    """Falls back to the legacy 2026q2 prefix when neither override nor
    manifest carry a destination_base. Preserves prior behaviour for runs
    against the original validation cohort."""
    manifest = tmp_path / "m.json"
    manifest.write_text("{}")

    class A:
        cohort_base = None

    args = A()
    args.manifest = str(manifest)
    out = orch._derive_cohorts_prefix(args)
    assert out == "cohorts/gatk-sv-validation-2026q2"


def test_gse_runs_by_sample_module_groups_correctly(orch) -> None:
    gse_run_results = {
        "manifest": {
            "runs": [
                {"id": "r1", "module": "cc", "sample": "HG00096"},
                {"id": "r2", "module": "manta", "sample": "HG00096"},
                {"id": "r3", "module": "wham", "sample": "HG00129"},
            ],
        },
        "run_results": {},
    }
    out = orch._gse_runs_by_sample_module(gse_run_results)
    assert out["HG00096"]["cc"] == "r1"
    assert out["HG00096"]["manta"] == "r2"
    assert out["HG00129"]["wham"] == "r3"


def test_gse_runs_by_sample_module_handles_none(orch) -> None:
    """Skip-gse case: caller passes None and we return an empty mapping."""
    assert orch._gse_runs_by_sample_module(None) == {}
    assert orch._gse_runs_by_sample_module({}) == {}
