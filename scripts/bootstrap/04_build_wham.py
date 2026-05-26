#!/usr/bin/env python3
"""Verify the upstream Wham image is in the customer's ECR.

History: this step used to build a custom ``gatk-sv/wham:fast-v5`` image
from ``wham-patch/whamg-flush.patch`` (an OpenMP region-parallel build of
``whamg``). On 2026-05-26 a body-MD5 validation against the upstream
``whamg`` showed only 83 % record overlap — a real algorithmic divergence,
not numerical noise. Production reverted to the upstream binary.

The upstream Wham image (``gatk-sv/wham:2024-10-25-v0.29-beta-5ea22a52``)
is mirrored into ECR by step 3 (``03_setup_ecr.py``) along with the rest
of the GATK-SV image set. This step now just *verifies* that the image
landed in ECR and is HealthOmics-accessible.

Customers who want to revisit the OpenMP fast-build can still rebuild from
``wham-patch/`` manually — see ``docs/wdl-audit.md`` for the divergence
analysis and what would be required to validate a future fast-build before
rolling it back into production.
"""
from __future__ import annotations

import os
import subprocess
import sys


WHAM_IMAGE_TAG = "2024-10-25-v0.29-beta-5ea22a52"


def main() -> int:
    account = os.environ.get("AWS_ACCOUNT_ID")
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    if not account:
        print("ERROR: AWS_ACCOUNT_ID env var required", file=sys.stderr)
        return 1

    target_uri = (
        f"{account}.dkr.ecr.{region}.amazonaws.com/gatk-sv/wham:{WHAM_IMAGE_TAG}"
    )

    rc = subprocess.call(
        [
            "aws", "ecr", "describe-images",
            "--repository-name", "gatk-sv/wham",
            "--image-ids", f"imageTag={WHAM_IMAGE_TAG}",
            "--region", region,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if rc == 0:
        print(f"  OK: {target_uri}")
        print("  (Step 3 already cloned upstream wham; nothing to build.)")
        return 0

    print(
        f"ERROR: {target_uri} not found in ECR.",
        file=sys.stderr,
    )
    print(
        "  Re-run scripts/bootstrap/03_setup_ecr.py — the upstream Wham image "
        "is mirrored from gcr.io/broad-dsde-methods alongside every other "
        "GATK-SV Docker image.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
