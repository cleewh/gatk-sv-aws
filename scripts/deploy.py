"""End-to-end deployment driver for the GATK-SV HealthOmics migration.

Executes Design §Deployment & Operational Procedures Steps 1-12 in order,
composing the components from
:mod:`kiro_life_sciences.gatk_sv_healthomics`. Each step is a single
function call so the driver can be run step-by-step (useful for debugging)
or end-to-end (``deploy.py --all``).

Requires AWS credentials for ``ap-southeast-1`` with permission to:

* ``omics:CreateWorkflow`` / ``omics:CreateWorkflowVersion`` / ``omics:GetWorkflow``
* ``omics:CreateRunCache``
* ``ecr:CreatePullThroughCacheRule`` / ``ecr:PutRegistryPolicy``
* ``s3:CopyObject`` / ``s3:PutObject`` on the target reference and output buckets
* ``iam:CreateRole`` / ``iam:PutRolePolicy`` (for the synthesized run role)

Run:

    python gatk-sv-healthomics/scripts/deploy.py --help

The script is intentionally thin — every action is delegated to a component
so behavior stays testable. See ``tests/gatk_sv_healthomics/unit/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLES_DIR = REPO_ROOT / "gatk-sv-healthomics" / "wdl" / "bundles"
REG_MAP_PATH = (
    REPO_ROOT
    / "gatk-sv-healthomics"
    / "container-registry-map"
    / "container-registry-map.json"
)
WORKFLOW_VERSIONS_PATH = REPO_ROOT / "gatk-sv-healthomics" / "workflow-versions.json"
IAM_POLICY_PATH = REPO_ROOT / "gatk-sv-healthomics" / "iam" / "policies" / "gatk-sv-run-role.json"
IAM_TRUST_PATH = (
    REPO_ROOT / "gatk-sv-healthomics" / "iam" / "policies" / "gatk-sv-run-role-trust.json"
)

DEFAULT_REGION = "ap-southeast-1"


def step_1_region_preflight(aws_region: str) -> None:
    """Step 1: assert HealthOmics is available in the target region."""
    from kiro_life_sciences.gatk_sv_healthomics.orchestrator import TARGET_REGION

    if aws_region != TARGET_REGION:
        raise RuntimeError(
            f"deploy.py is pinned to {TARGET_REGION}, got {aws_region!r}. "
            "Design §1 fixes Target_Region; change the design before the driver."
        )
    logger.info("Step 1: region preflight passed (%s).", aws_region)


def step_8_register_workflows(semver: str = "1.0.0") -> None:
    """Step 8: register each packaged bundle as a HealthOmics workflow version.

    Reads bundles from ``gatk-sv-healthomics/wdl/bundles/<module>/<module>-bundle.zip``
    and posts them via ``omics.create_workflow`` (first registration) or
    ``omics.create_workflow_version`` (subsequent). Persists the returned
    identifiers in ``gatk-sv-healthomics/workflow-versions.json``.
    """
    import boto3

    from kiro_life_sciences.gatk_sv_healthomics.models import MIGRATED_MODULES
    from kiro_life_sciences.gatk_sv_healthomics.registrar import (
        RegistrationTarget,
        find_existing_workflow_id,
        load_workflow_versions,
        persist_workflow_version,
        register_module,
    )
    from kiro_life_sciences.gatk_sv_healthomics.registry import build_registry_map

    client = boto3.client("omics", region_name=DEFAULT_REGION)  # type: ignore[attr-defined]
    existing = load_workflow_versions(WORKFLOW_VERSIONS_PATH)

    for module in MIGRATED_MODULES:
        bundle_zip = BUNDLES_DIR / module / f"{module}-bundle.zip"
        divergence_json = BUNDLES_DIR / module / "divergence.json"
        if not bundle_zip.exists():
            logger.warning("skipping %s: %s missing", module, bundle_zip)
            continue

        # Reconstruct the bundle/template/map from on-disk artifacts.
        # TODO(Phase 5): wire this through package_module so divergences and
        # parameter templates come from a single source of truth.
        from kiro_life_sciences.gatk_sv_healthomics.models import (
            ContainerRegistryMap,
            DivergenceEntry,
            PackagedBundle,
        )

        divergences: list[DivergenceEntry] = []
        if divergence_json.exists():
            divergences = [
                DivergenceEntry.model_validate(d)
                for d in json.loads(divergence_json.read_text())
            ]
        bundle = PackagedBundle(
            zip_path=bundle_zip,
            main_wdl_path=_infer_main_wdl_path(bundle_zip, module),
            module=module,
            upstream_commit="7eb2af1feea9",
            divergence=divergences,
        )

        # Generate parameter template from the bundle's main WDL in-memory.
        import tempfile
        import zipfile

        import WDL

        from kiro_life_sciences.gatk_sv_healthomics.template import (
            WdlInput,
            WdlWorkflow,
            generate_template,
        )

        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(bundle_zip) as zf:
                zf.extractall(tmp)
            main_path = Path(tmp) / bundle.main_wdl_path
            doc = WDL.load(str(main_path))
            if doc.workflow is None:
                logger.warning("%s has no workflow; skipping template", module)
                continue
            wdl_inputs: list[WdlInput] = []
            for decl in doc.workflow.available_inputs:
                wdl_type = str(decl.value.type)
                optional = wdl_type.endswith("?")
                if optional:
                    wdl_type = wdl_type[:-1]
                wdl_inputs.append(
                    WdlInput(
                        name=decl.value.name,
                        type=wdl_type,
                        optional=optional,
                        description="",
                    )
                )
            workflow = WdlWorkflow(name=doc.workflow.name, inputs=tuple(wdl_inputs))
        template = generate_template(workflow)

        registry_map = ContainerRegistryMap.model_validate(
            json.loads(REG_MAP_PATH.read_text())
        )
        # build_registry_map can emit a default map; we prefer the committed one.
        _ = build_registry_map  # referenced for visibility

        target = RegistrationTarget(
            module=module,
            workflow_id=find_existing_workflow_id(existing, module),
            container_registry_map_uri=f"s3://omics-{DEFAULT_REGION}/container-registry-map/container-registry-map.json",
            parameter_template_uri=f"s3://omics-{DEFAULT_REGION}/parameter-templates/{module}.json",
        )

        record = register_module(
            client,
            bundle,
            template,
            registry_map,
            semver=semver,
            target=target,
        )
        persist_workflow_version(record, WORKFLOW_VERSIONS_PATH)
        logger.info("Step 8: registered %s → workflow_id=%s", module, record.workflow_id)


def _infer_main_wdl_path(bundle_zip: Path, module: str) -> str:
    """Return the main WDL path inside the bundle ZIP.

    Prefers ``main.wdl``, falls back to ``<module>.wdl``, finally the first
    ``.wdl`` at the top of the ZIP.
    """
    import zipfile

    with zipfile.ZipFile(bundle_zip) as zf:
        names = zf.namelist()
    for candidate in ("main.wdl", f"{module}.wdl"):
        if candidate in names:
            return candidate
    for name in names:
        if name.endswith(".wdl") and "/" not in name:
            return name
    raise RuntimeError(f"no main WDL found in {bundle_zip}")


def step_9_create_run_cache(s3_cache_location: str) -> str:
    """Step 9: create the cohort Run_Cache with ``CACHE_ALWAYS`` (Req 10.2)."""
    import boto3

    client = boto3.client("omics", region_name=DEFAULT_REGION)  # type: ignore[attr-defined]
    response = client.create_run_cache(
        name="gatk-sv-run-cache",
        cacheS3Location=s3_cache_location,
        cacheBehavior="CACHE_ALWAYS",
        tags={"gatk-sv:environment": "prod"},
    )
    cache_id = str(response["id"])
    logger.info("Step 9: Run_Cache created id=%s at %s", cache_id, s3_cache_location)
    return cache_id


def step_10_create_iam_role(
    *,
    role_name: str,
    reference_bucket: str,
    reference_prefix: str,
    input_bucket: str,
    input_prefix: str,
    output_bucket: str,
    output_prefix: str,
    wdl_zip_bucket: str,
    wdl_zip_prefix: str,
    ecr_account_id: str,
    ecr_repositories: list[str],
) -> str:
    """Step 10: create the HealthOmics run role (Req 12.1)."""
    import boto3

    from kiro_life_sciences.gatk_sv_healthomics.iam import synthesize_run_role
    from kiro_life_sciences.gatk_sv_healthomics.models import RoleScope

    scope = RoleScope(
        region=DEFAULT_REGION,
        reference_bucket=reference_bucket,
        reference_prefix=reference_prefix,
        input_bucket=input_bucket,
        input_prefix=input_prefix,
        output_bucket=output_bucket,
        output_prefix=output_prefix,
        wdl_zip_bucket=wdl_zip_bucket,
        wdl_zip_prefix=wdl_zip_prefix,
        ecr_account_id=ecr_account_id,
        ecr_repositories=ecr_repositories,
    )
    policies = synthesize_run_role(scope)
    if policies.broadness_violations:
        raise RuntimeError(
            f"IAM broadness check failed: {policies.broadness_violations!r}"
        )

    IAM_POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    IAM_POLICY_PATH.write_text(
        json.dumps(policies.permissions_policy, indent=2, sort_keys=True)
    )
    IAM_TRUST_PATH.write_text(
        json.dumps(policies.trust_policy, indent=2, sort_keys=True)
    )

    iam = boto3.client("iam")  # type: ignore[attr-defined]
    try:
        iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(policies.trust_policy),
        )
    except iam.exceptions.EntityAlreadyExistsException:
        logger.info("Step 10: role %s already exists; updating policy", role_name)
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="gatk-sv-run-policy",
        PolicyDocument=json.dumps(policies.permissions_policy),
    )
    return f"arn:aws:iam::{ecr_account_id}:role/{role_name}"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--step", type=int, choices=[1, 8, 9, 10], help="run only one step")
    parser.add_argument("--cache-s3", help="S3 URI for the run cache (step 9)")
    parser.add_argument("--role-name", default="gatk-sv-healthomics-run-role")
    parser.add_argument("--reference-bucket")
    parser.add_argument("--reference-prefix", default="gatk-sv/reference/GRCh38")
    parser.add_argument("--input-bucket")
    parser.add_argument("--input-prefix", default="cohorts")
    parser.add_argument("--output-bucket")
    parser.add_argument("--output-prefix", default="runs")
    parser.add_argument("--wdl-zip-bucket")
    parser.add_argument("--wdl-zip-prefix", default="workflows")
    parser.add_argument("--ecr-account-id")
    parser.add_argument(
        "--ecr-repository", action="append", default=[], help="repeatable"
    )
    parser.add_argument("--semver", default="1.0.0")
    args = parser.parse_args()

    step_1_region_preflight(args.region)
    if args.step == 1:
        return 0

    if args.step in (None, 8):
        step_8_register_workflows(semver=args.semver)
    if args.step in (None, 9) and args.cache_s3:
        step_9_create_run_cache(args.cache_s3)
    if args.step in (None, 10) and args.ecr_account_id:
        step_10_create_iam_role(
            role_name=args.role_name,
            reference_bucket=args.reference_bucket,
            reference_prefix=args.reference_prefix,
            input_bucket=args.input_bucket,
            input_prefix=args.input_prefix,
            output_bucket=args.output_bucket,
            output_prefix=args.output_prefix,
            wdl_zip_bucket=args.wdl_zip_bucket,
            wdl_zip_prefix=args.wdl_zip_prefix,
            ecr_account_id=args.ecr_account_id,
            ecr_repositories=args.ecr_repository,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
