#!/usr/bin/env python3
"""Configure ECR pull-through caches and clone the 12 GATK-SV images.

Reads container-registry-map/container-registry-map.json (after
00_substitute_placeholders.py has filled in the account id) and:

1. For each registryMappings entry, ensures the ECR pull-through cache
   rule exists for that upstream registry. Falls through cleanly if the
   rule is already configured.

2. For each imageMappings entry, ensures the image is cloned into the
   private ECR repo with HealthOmics access permissions.

3. Verifies every entry is HealthOmics-accessible
   (ecr:BatchGetImage, ecr:GetDownloadUrlForLayer granted to omics.amazonaws.com).

Idempotent: skips entries that already match the target state.

Note: this uses the existing ``scripts/clone_gcr_images.sh`` for the heavy
lifting. We just wrap it to read account + region from env.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    account = os.environ.get("AWS_ACCOUNT_ID")
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    if not account:
        print("ERROR: AWS_ACCOUNT_ID env var required", file=sys.stderr)
        return 1

    repo_root = Path(__file__).resolve().parent.parent.parent
    cmd = ["bash", str(repo_root / "scripts" / "clone_gcr_images.sh")]
    env = os.environ.copy()
    env["AWS_ACCOUNT_ID"] = account
    env["AWS_DEFAULT_REGION"] = region
    print("Running:", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(repo_root), env=env)


if __name__ == "__main__":
    sys.exit(main())
