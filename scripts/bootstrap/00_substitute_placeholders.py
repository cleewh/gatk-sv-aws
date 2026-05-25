#!/usr/bin/env python3
"""Substitute __ACCOUNT_ID__, __RUN_CACHE_ID__, __EC2_INSTANCE_ID__ placeholders.

Reads from environment:
  AWS_ACCOUNT_ID         (required)
  GATK_SV_RUN_CACHE_ID   (optional; if not set, leaves __RUN_CACHE_ID__ as-is)
  GATK_SV_EC2_INSTANCE_ID (optional; if not set, leaves __EC2_INSTANCE_ID__ as-is)

Operates on these files (anywhere they appear in the repo):
  *.json under iam/, container-registry-map/, parameter-templates/,
  reference-bundle/, validation-cohort/inputs/, runtime/, *.json at the repo root,
  and the CDK config python/src/gatk_sv_aws/step_functions/cdk.json.

Idempotent: re-running with the same env values is a no-op.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PLACEHOLDERS = [
    ("__ACCOUNT_ID__",    "AWS_ACCOUNT_ID",          True),
    ("__RUN_CACHE_ID__",  "GATK_SV_RUN_CACHE_ID",    False),
    ("__EC2_INSTANCE_ID__", "GATK_SV_EC2_INSTANCE_ID", False),
]

ROOT = Path(__file__).resolve().parent.parent.parent

CONFIG_GLOBS = [
    "iam/policies/*.json",
    "container-registry-map/*.json",
    "parameter-templates/*.json",
    "reference-bundle/*.json",
    "reference-bundle/manifests/*.json",
    "validation-cohort/inputs/*.json",
    "runtime/*.json",
    "*.json",  # repo-root run records
    "python/src/gatk_sv_aws/step_functions/cdk.json",
]


def main() -> int:
    print("Resolving placeholder values from environment...")
    subs = []
    for placeholder, env_var, required in PLACEHOLDERS:
        value = os.environ.get(env_var)
        if value:
            subs.append((placeholder, value))
            print(f"  {placeholder:>24s}  ->  {value}")
        elif required:
            print(f"ERROR: env var {env_var} is required for placeholder {placeholder}", file=sys.stderr)
            return 1
        else:
            print(f"  {placeholder:>24s}  ->  (skipped; {env_var} not set)")

    if not subs:
        print("Nothing to substitute.")
        return 0

    files = set()
    for pattern in CONFIG_GLOBS:
        files.update(ROOT.glob(pattern))

    n_changed = 0
    for f in sorted(files):
        if not f.is_file():
            continue
        try:
            text = f.read_text()
        except UnicodeDecodeError:
            continue
        new = text
        for placeholder, value in subs:
            new = new.replace(placeholder, value)
        if new != text:
            f.write_text(new)
            n_changed += 1
            print(f"  updated {f.relative_to(ROOT)}")

    print(f"\n{n_changed} file(s) updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
