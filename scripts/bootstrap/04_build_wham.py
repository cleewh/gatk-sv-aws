#!/usr/bin/env python3
"""Build the patched whamg-flush image and push to the customer's ECR.

The repo ships ``wham-patch/whamg-flush.patch`` plus four Dockerfiles
(``Dockerfile``, ``Dockerfile.fast``, ``Dockerfile.lean``,
``Dockerfile.streaming``).  Production uses ``Dockerfile.fast`` which produces
``gatk-sv/wham:fast-v5``.

This script:
  1. Logs into ECR.
  2. Fetches the upstream wham source at the commit referenced by the patch.
  3. Applies the patch.
  4. Builds with ``--build-arg ACCOUNT_ID=<account>`` (Dockerfile uses the
     customer's ECR as base for the original wham image).
  5. Pushes to ``<account>.dkr.ecr.<region>.amazonaws.com/gatk-sv/wham:fast-v5``.

Idempotent: skips if the target tag already exists in ECR.

Customer must have Docker running and the AWS CLI configured.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], **kwargs) -> int:
    print(">", " ".join(cmd))
    return subprocess.call(cmd, **kwargs)


def main() -> int:
    account = os.environ.get("AWS_ACCOUNT_ID")
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    if not account:
        print("ERROR: AWS_ACCOUNT_ID env var required", file=sys.stderr)
        return 1

    repo_root = Path(__file__).resolve().parent.parent.parent
    wham_dir = repo_root / "wham-patch"
    ecr_host = f"{account}.dkr.ecr.{region}.amazonaws.com"
    target_tag = f"{ecr_host}/gatk-sv/wham:fast-v5"

    # Check whether the image already exists in ECR
    rc = subprocess.call(
        [
            "aws", "ecr", "describe-images",
            "--repository-name", "gatk-sv/wham",
            "--image-ids", "imageTag=fast-v5",
            "--region", region,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if rc == 0:
        print(f"  {target_tag} already exists in ECR; skipping build.")
        return 0

    # Login to ECR
    login_cmd = (
        f"aws ecr get-login-password --region {region} | "
        f"docker login --username AWS --password-stdin {ecr_host}"
    )
    print(">", login_cmd)
    if subprocess.call(login_cmd, shell=True) != 0:
        return 2

    # Build
    rc = run(
        [
            "docker", "build",
            "--platform", "linux/amd64",
            "-f", str(wham_dir / "Dockerfile.fast"),
            "--build-arg", f"ACCOUNT_ID={account}",
            "-t", target_tag,
            str(wham_dir),
        ],
        cwd=str(repo_root),
    )
    if rc != 0:
        return rc

    # Push
    rc = run(["docker", "push", target_tag])
    if rc != 0:
        return rc

    print(f"\nBuilt and pushed: {target_tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
