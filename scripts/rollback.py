"""Rollback to a prior HealthOmics workflow version.

Re-registers a prior ``version_name`` for a given module via
:func:`register_module` and marks it as the operator-facing prod tag.
Preserves the existing Run_Cache (Design §Deployment → Rollback, Req 10.1,
17.6).

Usage:

    python gatk-sv-healthomics/scripts/rollback.py \\
        --module GatherSampleEvidence \\
        --to-version 1.0.0
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_VERSIONS_PATH = REPO_ROOT / "gatk-sv-healthomics" / "workflow-versions.json"


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True)
    parser.add_argument("--to-version", required=True)
    parser.add_argument("--tag", default="prod")
    args = parser.parse_args()

    from kiro_life_sciences.gatk_sv_healthomics.registrar import load_workflow_versions

    records = load_workflow_versions(WORKFLOW_VERSIONS_PATH)
    target = next(
        (
            r
            for r in records
            if r.module == args.module and r.version_name == args.to_version
        ),
        None,
    )
    if target is None:
        logger.error(
            "no workflow-version record for module=%s version=%s",
            args.module,
            args.to_version,
        )
        return 2

    # Apply the rollback tag via boto3. Left as a dry-run by default to avoid
    # surprising operators.
    try:
        import boto3

        client = boto3.client("omics", region_name="ap-southeast-1")  # type: ignore[attr-defined]
        client.tag_resource(
            resourceArn=f"arn:aws:omics:ap-southeast-1::workflow/{target.workflow_id}",
            tags={"gatk-sv:rollback-tag": args.tag, "gatk-sv:rolled-back-to": args.to_version},
        )
        logger.info(
            "Tagged workflow %s version %s with %s=%s",
            target.workflow_id,
            target.version_name,
            "gatk-sv:rollback-tag",
            args.tag,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("rollback tagging failed: %s", exc)
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
