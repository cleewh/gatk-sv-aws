"""Unit tests for the ``gatk-sv-healthomics`` CLI entry point.

Covers the subcommands that don't touch AWS:

* ``validate-manifest`` with a valid and invalid manifest.
* ``template`` given a tiny WDL ZIP.
* ``build_parser`` surface area (no crash, every subcommand discoverable).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from gatk_sv_aws.cli import build_parser, main


def test_parser_exposes_all_subcommands() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    for subcommand in ("package", "template", "validate-manifest", "stage-reference"):
        assert subcommand in help_text


def test_validate_manifest_happy_path(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "cohort_id": "c1",
                "reference_build": "GRCh38",
                "samples": [
                    {
                        "sample_id": "S1",
                        "reads_uri": "s3://bucket/s1.cram",
                        "index_uri": "s3://bucket/s1.cram.crai",
                        "sex": "F",
                    }
                ],
            }
        )
    )
    rc = main(["validate-manifest", "--manifest", str(manifest_path)])
    assert rc == 0


def test_validate_manifest_reports_failures(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "cohort_id": "c1",
                "reference_build": "GRCh38",
                "samples": [
                    {
                        "sample_id": "S1",
                        "reads_uri": "s3://bucket/s1.cram",
                        "index_uri": "s3://bucket/s1.cram.bai",  # wrong index
                        "sex": "F",
                    }
                ],
            }
        )
    )
    caplog.set_level("ERROR")
    rc = main(["validate-manifest", "--manifest", str(manifest_path)])
    assert rc == 1
    assert "unsupported_format" in caplog.text


def test_template_writes_json(tmp_path: Path) -> None:
    # Build a tiny bundle ZIP with a minimal WDL workflow.
    bundle = tmp_path / "m" / "m-bundle.zip"
    bundle.parent.mkdir(parents=True)
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr(
            "main.wdl",
            "version 1.0\nworkflow M { input { File reference } output {} }\n",
        )

    output = tmp_path / "m-template.json"
    rc = main(
        [
            "template",
            "--bundle",
            str(bundle),
            "--output",
            str(output),
        ]
    )
    assert rc == 0

    data = json.loads(output.read_text())
    assert "reference" in data
    assert data["reference"]["type"] == "File"
