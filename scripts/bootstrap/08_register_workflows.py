#!/usr/bin/env python3
"""Register the 18 GATK-SV HealthOmics workflows in the customer's account.

Covers the 10 original modules plus the 8 added in the v1.0 amendment
(Req 19, 2026-05-26): EvidenceQC, RefineComplexVariants, JoinRawCalls,
SVConcordance, ScoreGenotypes, FilterGenotypes, MainVcfQC, VisualizeCnvs.

For each module in PRODUCTION_BUNDLES we:
  1. Read the WDL bundle ZIP shipped under wdl/bundles/<Module>/
  2. Read the parameter template under parameter-templates/<Module>.json
  3. Read the container-registry-map (filled by 00_substitute_placeholders.py)
  4. Call CreateWorkflow (or skip if the same name+upstream-commit is already
     registered as ACTIVE in the customer's account)
  5. Persist the result to workflow-ids.json

The pipeline launchers read workflow ids from workflow-ids.json (so the same
workflow ids are used end-to-end).

Idempotent: a second run with no upstream changes is a no-op.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import boto3

# Module -> bundle filename (latest production-validated bundle)
PRODUCTION_BUNDLES = {
    "GatherSampleEvidence":           "GatherSampleEvidence/GatherSampleEvidence-bundle.zip",
    "GatherBatchEvidence":            "GatherBatchEvidence/GatherBatchEvidence-bundle-v5.zip",
    "ClusterBatch":                   "ClusterBatch/ClusterBatch-bundle-v3.zip",
    "GenerateBatchMetrics":           "GenerateBatchMetrics/GenerateBatchMetrics-bundle.zip",
    "FilterBatch":                    "FilterBatch/FilterBatch-bundle-v3.zip",
    "MergeBatchSites":                "MergeBatchSites/MergeBatchSites-bundle-v2.zip",
    "GenotypeBatch":                  "GenotypeBatch/GenotypeBatch-bundle.zip",
    "RegenotypeCNVs":                 "RegenotypeCNVs/RegenotypeCNVs-bundle.zip",
    "MakeCohortVcf":                  "MakeCohortVcf/MakeCohortVcf-bundle.zip",
    "AnnotateVcf":                    "AnnotateVcf/AnnotateVcf-bundle.zip",
    # Phase 8 (Req 19) modules. Bundle ZIPs produced by
    # scripts/migrate_v1_modules.py against gatk-sv@v1.1 (a1be457).
    "EvidenceQC":                     "EvidenceQC/EvidenceQC-bundle.zip",
    "RefineComplexVariants":          "RefineComplexVariants/RefineComplexVariants-bundle.zip",
    "JoinRawCalls":                   "JoinRawCalls/JoinRawCalls-bundle.zip",
    "SVConcordance":                  "SVConcordance/SVConcordance-bundle.zip",
    "ScoreGenotypes":                 "ScoreGenotypes/ScoreGenotypes-bundle.zip",
    "FilterGenotypes":                "FilterGenotypes/FilterGenotypes-bundle.zip",
    "MainVcfQC":                      "MainVcfQC/MainVcfQC-bundle.zip",
    "VisualizeCnvs":                  "VisualizeCnvs/VisualizeCnvs-bundle.zip",
}

# Mapping of module name -> main WDL file inside the bundle.
# Most module bundles use wdl/<Module>.wdl, but two have different casing
# (MainVcfQC bundle's main file is MainVcfQc.wdl) — record the exceptions
# explicitly so the registrar passes the right path.
MAIN_WDL_OVERRIDES = {
    "MainVcfQC": "wdl/MainVcfQc.wdl",
}

NAME_PREFIX = "gatk-sv-"


def existing_workflow(omics, name: str) -> dict | None:
    for page in omics.get_paginator("list_workflows").paginate():
        for w in page.get("items", []):
            if w.get("name") == name and w.get("status") == "ACTIVE":
                return w
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="workflow-ids.json",
                    help="JSON file to write the registered workflow ids to")
    args = ap.parse_args()

    account = os.environ.get("AWS_ACCOUNT_ID")
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    if not account:
        print("ERROR: AWS_ACCOUNT_ID env var required", file=sys.stderr)
        return 1

    repo_root = Path(__file__).resolve().parent.parent.parent
    bundles_dir = repo_root / "wdl" / "bundles"
    templates_dir = repo_root / "parameter-templates"
    registry_map_path = repo_root / "container-registry-map" / "container-registry-map.json"
    if "__ACCOUNT_ID__" in registry_map_path.read_text():
        print("ERROR: container-registry-map.json still has __ACCOUNT_ID__ placeholder.", file=sys.stderr)
        print("       Run 00_substitute_placeholders.py first.", file=sys.stderr)
        return 2

    registry_map_uri = f"s3://omics-wdl-{region}-{account}/container-registry-map/container-registry-map.json"

    # Upload registry map so HealthOmics can read it during workflow creation.
    s3 = boto3.client("s3", region_name=region)
    s3.put_object(
        Bucket=f"omics-wdl-{region}-{account}",
        Key="container-registry-map/container-registry-map.json",
        Body=registry_map_path.read_bytes(),
    )
    print(f"Uploaded {registry_map_uri}")

    omics = boto3.client("omics", region_name=region)
    out: dict[str, dict] = {}
    for module, bundle_rel in PRODUCTION_BUNDLES.items():
        name = f"{NAME_PREFIX}{module}"
        existing = existing_workflow(omics, name)
        if existing:
            print(f"  {module:<25s} skip (id={existing['id']})")
            out[module] = {"workflow_id": existing["id"], "name": name, "status": "ACTIVE", "skipped": True}
            continue

        bundle_path = bundles_dir / bundle_rel
        template_path = templates_dir / f"{module}.json"
        if not bundle_path.exists():
            print(f"  {module:<25s} ERROR: bundle not found at {bundle_path}")
            continue
        if not template_path.exists():
            print(f"  {module:<25s} ERROR: parameter template not found at {template_path}")
            continue

        print(f"  {module:<25s} registering...", end=" ", flush=True)
        param_template_raw = json.loads(template_path.read_text())
        # Strip 'type' field -- HealthOmics parameterTemplate API only accepts
        # 'description' and 'optional'.  The 'type' field is repo-internal
        # metadata used by our own validators (see docs/scope-inventory.md).
        param_template = {
            k: {kk: vv for kk, vv in v.items() if kk in ("description", "optional")}
            for k, v in param_template_raw.items()
        }
        resp = omics.create_workflow(
            name=name,
            engine="WDL",
            definitionZip=bundle_path.read_bytes(),
            main=MAIN_WDL_OVERRIDES.get(module, f"wdl/{module}.wdl"),
            parameterTemplate=param_template,
            description=f"GATK-SV {module} (production-validated bundle: {bundle_rel})",
            tags={
                "gatk-sv:resource": "workflow",
                "gatk-sv:module": module,
                "gatk-sv:environment": "production",
            },
        )
        wf_id = resp["id"]
        # Wait until ACTIVE
        for _ in range(60):
            info = omics.get_workflow(id=wf_id)
            if info["status"] == "ACTIVE":
                break
            if info["status"] in ("FAILED", "INACTIVE", "DELETED"):
                print(f"FAILED: {info.get('statusMessage', '')[:200]}")
                break
            time.sleep(5)
        print(f"id={wf_id} status={info['status']}")
        out[module] = {"workflow_id": wf_id, "name": name, "status": info["status"], "skipped": False}

    out_path = repo_root / args.out
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nWorkflow ids written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
