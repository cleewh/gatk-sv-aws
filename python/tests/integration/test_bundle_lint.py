"""Integration test: every committed bundle ZIP lints cleanly (Req 2.3).

Iterates over ``gatk-sv-healthomics/wdl/bundles/<module>/<module>-bundle.zip``
and runs ``lint_bundle`` (miniwdl-backed local linter) on each.

Does not require AWS credentials; guard is that bundles must exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gatk_sv_aws.models import (
    MIGRATED_MODULES,
    PackagedBundle,
)
from gatk_sv_aws.packager import lint_bundle

BUNDLES_ROOT = (
    Path(__file__).resolve().parents[4] / "gatk-sv-healthomics" / "wdl" / "bundles"
)


@pytest.mark.parametrize("module", list(MIGRATED_MODULES))
def test_bundle_lints_cleanly(module: str) -> None:
    bundle_zip = BUNDLES_ROOT / module / f"{module}-bundle.zip"
    if not bundle_zip.exists():
        pytest.skip(
            f"bundle ZIP not found: {bundle_zip}; run scripts/migrate_module.py."
        )

    bundle = PackagedBundle(
        zip_path=bundle_zip,
        main_wdl_path=_infer_main_wdl(bundle_zip),
        module=module,  # type: ignore[arg-type]
        upstream_commit="7eb2af1feea9",
        divergence=[],
    )
    report = lint_bundle(bundle)
    assert report.status == "success", (
        f"module={module} lint errors:\n" + "\n".join(report.errors)
    )


def _infer_main_wdl(bundle_zip: Path) -> str:
    import zipfile

    with zipfile.ZipFile(bundle_zip) as zf:
        names = zf.namelist()
    module = bundle_zip.parent.name
    # Packager emits bundles under wdl/<module>.wdl (see packager/__init__.py).
    for candidate in (f"wdl/{module}.wdl", f"{module}.wdl", "main.wdl"):
        if candidate in names:
            return candidate
    for name in names:
        if name.endswith(".wdl") and "/" not in name:
            return name
    raise RuntimeError(f"no main .wdl in {bundle_zip}")
