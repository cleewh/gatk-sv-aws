#!/usr/bin/env python3
"""Stage the GRCh38 reference bundle into the customer's regional S3 bucket.

Thin wrapper around ``scripts/stage_reference.py`` that injects the
account-id and target bucket from environment variables.

Usage:
    AWS_ACCOUNT_ID=<account> AWS_DEFAULT_REGION=ap-southeast-1 \\
        python scripts/bootstrap/02_stage_reference.py [--build GRCh38]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", default="GRCh38", choices=["GRCh37", "GRCh38"])
    args = ap.parse_args()

    account = os.environ.get("AWS_ACCOUNT_ID")
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    if not account:
        print("ERROR: AWS_ACCOUNT_ID env var required", file=sys.stderr)
        return 1

    repo_root = Path(__file__).resolve().parent.parent.parent
    manifest = repo_root / "reference-bundle" / "manifests" / f"{args.build}.json"
    bucket = f"omics-ref-{region}-{account}"

    cmd = [
        sys.executable,
        str(repo_root / "scripts" / "stage_reference.py"),
        "--manifest", str(manifest),
        "--bucket", bucket,
        "--prefix", f"gatk-sv/reference/{args.build}",
    ]
    print("Running:", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(repo_root))


if __name__ == "__main__":
    sys.exit(main())
