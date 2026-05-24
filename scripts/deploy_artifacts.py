"""Upload workflow artifacts to S3 at the paths referenced by workflow-versions.json.

For each migrated module, uploads:

* ``wdl/bundles/<module>/<module>-bundle.zip``        → ``s3://{wdl}/workflows/<module>/<module>-bundle.zip``
* ``parameter-templates/<module>.json``               → ``s3://{wdl}/parameter-templates/<module>.json``

Plus the global container registry map:

* ``container-registry-map/container-registry-map.json`` → ``s3://{wdl}/container-registry-map/container-registry-map.json``

Idempotent (``put_object`` overwrites).
"""

from __future__ import annotations

import os
import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLES_DIR = REPO_ROOT / "gatk-sv-healthomics" / "wdl" / "bundles"
TEMPLATES_DIR = REPO_ROOT / "gatk-sv-healthomics" / "parameter-templates"
REG_MAP_PATH = (
    REPO_ROOT
    / "gatk-sv-healthomics"
    / "container-registry-map"
    / "container-registry-map.json"
)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bucket", default=f"omics-wdl-ap-southeast-1-{os.environ.get('AWS_ACCOUNT_ID', '__ACCOUNT_ID__')}"
    )
    parser.add_argument("--region", default="ap-southeast-1")
    args = parser.parse_args()

    import boto3

    from kiro_life_sciences.gatk_sv_healthomics.models import MIGRATED_MODULES

    s3 = boto3.client("s3", region_name=args.region)  # type: ignore[attr-defined]

    def _put(local: Path, key: str) -> None:
        s3.put_object(
            Bucket=args.bucket,
            Key=key,
            Body=local.read_bytes(),
        )
        logger.info("  %s -> s3://%s/%s", local.relative_to(REPO_ROOT), args.bucket, key)

    # Registry map
    logger.info("uploading container registry map…")
    _put(REG_MAP_PATH, "container-registry-map/container-registry-map.json")

    # Per-module artifacts
    for module in MIGRATED_MODULES:
        bundle = BUNDLES_DIR / module / f"{module}-bundle.zip"
        template = TEMPLATES_DIR / f"{module}.json"
        if not bundle.exists():
            logger.warning("skip %s: %s missing", module, bundle)
            continue
        logger.info("uploading %s artifacts…", module)
        _put(bundle, f"workflows/{module}/{module}-bundle.zip")
        if template.exists():
            _put(template, f"parameter-templates/{module}.json")

    logger.info("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
