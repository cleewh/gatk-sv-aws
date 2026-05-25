"""CLI entry point for the GATK-SV HealthOmics migration.

Declared in ``pyproject.toml`` as::

    [project.scripts]
    gatk-sv-healthomics = "gatk_sv_aws.cli:main"

Subcommands (Req 17.1):

    gatk-sv-healthomics package --module <M> --commit <sha>
        Fetch upstream, strip MELT, reject gs-scheme URIs, lint, and emit
        a bundle ZIP plus divergence.json under
        gatk-sv-healthomics/wdl/bundles/<M>/.

    gatk-sv-healthomics template --module <M>
        Generate the parameter template for a previously-packaged module
        and write it under gatk-sv-healthomics/parameter-templates/<M>.json.

    gatk-sv-healthomics validate-manifest --manifest <path>
        Validate a sample-cohort manifest against Requirement 6 rules.

    gatk-sv-healthomics stage-reference --manifest <path> \\
                                        --bucket <bucket> --prefix <prefix>
        Stage a reference-bundle manifest to a regional S3 prefix.

    gatk-sv-healthomics submit --manifest <path> --cohort-id <id> \\
                               --output-uri s3://bucket/prefix [--wait]
        Start an execution of the deployed Step Functions orchestrator
        that runs the full ten-module pipeline end-to-end. With
        ``--wait`` the command blocks until the pipeline reaches a
        terminal status and prints the cost report.

Subcommands that require AWS (``stage-reference``, ``submit``) construct
their boto3 clients lazily; unit tests do not import boto3 transitively.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _cmd_package(args: argparse.Namespace) -> int:
    from gatk_sv_aws.models import MIGRATED_MODULES
    from gatk_sv_aws.packager import (
        lint_bundle,
        package_module,
    )

    if args.module not in MIGRATED_MODULES:
        logger.error("unknown module %s", args.module)
        return 2

    bundle = package_module(args.commit, args.module, output_dir=args.output_dir)
    lint = lint_bundle(bundle)
    logger.info(
        "packaged module=%s divergences=%d lint_status=%s",
        args.module,
        len(bundle.divergence),
        lint.status,
    )
    return 0 if lint.status == "success" else 3


def _cmd_template(args: argparse.Namespace) -> int:
    import tempfile
    import zipfile

    import WDL

    from gatk_sv_aws.template import (
        WdlInput,
        WdlWorkflow,
        generate_template,
    )

    bundle_zip = Path(args.bundle)
    if not bundle_zip.exists():
        logger.error("bundle ZIP not found: %s", bundle_zip)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(bundle_zip) as zf:
            zf.extractall(tmp)
            main_wdl = args.main or _infer_main_wdl(zf)
        main_path = Path(tmp) / main_wdl

        doc = WDL.load(str(main_path))
        if doc.workflow is None:
            logger.error("no workflow declaration found in %s", main_wdl)
            return 2

        inputs: list[WdlInput] = []
        for decl in doc.workflow.available_inputs:
            wdl_type = str(decl.value.type)
            # miniwdl renders optional as "Type?"; our models wants the
            # stripped form plus an explicit optional flag.
            optional = wdl_type.endswith("?")
            if optional:
                wdl_type = wdl_type[:-1]
            inputs.append(
                WdlInput(
                    name=decl.value.name,
                    type=wdl_type,
                    optional=optional,
                    description="",
                )
            )

        workflow = WdlWorkflow(name=doc.workflow.name, inputs=tuple(inputs))
    template = generate_template(workflow)

    out_path = Path(args.output) if args.output else Path(
        f"gatk-sv-healthomics/parameter-templates/{bundle_zip.parent.name}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(template.to_json_dict(), indent=2, sort_keys=True))
    logger.info("wrote %s (%d inputs)", out_path, len(template.entries))
    return 0


def _cmd_validate_manifest(args: argparse.Namespace) -> int:
    from gatk_sv_aws.models import SampleManifest
    from gatk_sv_aws.orchestrator import validate_manifest

    data = json.loads(Path(args.manifest).read_text())
    manifest = SampleManifest.model_validate(data)
    issues = validate_manifest(manifest)

    if not issues:
        logger.info("manifest OK: %d samples", len(manifest.samples))
        return 0

    for issue in issues:
        logger.error(
            "%s %s: %s",
            issue.sample_id,
            issue.rule,
            issue.detail,
        )
    return 1


def _cmd_stage_reference(args: argparse.Namespace) -> int:
    import boto3

    from gatk_sv_aws.reference import (
        load_manifest,
        stage_reference_bundle,
    )

    manifest = load_manifest(Path(args.manifest))
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    s3 = boto3.client("s3", region_name=region)  # type: ignore[attr-defined]
    report = stage_reference_bundle(
        manifest,
        destination_bucket=args.bucket,
        destination_prefix=args.prefix,
        s3_client=s3,
    )
    logger.info(
        "staged %d files, %d failed", len(report.succeeded), len(report.failed)
    )
    for failed in report.failed:
        logger.error("FAIL %s: %s", failed.logical_name, failed.reason)
    return 0 if report.all_succeeded else 4


def _cmd_submit(args: argparse.Namespace) -> int:
    """Submit a cohort to the deployed Step Functions orchestrator."""
    from gatk_sv_aws.submit import (
        DEFAULT_REGION,
        DEFAULT_STACK_NAME,
        format_progress,
        load_manifest_json,
        submit_cohort,
        wait_for_completion,
    )

    region = args.region or os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)

    # Manifest may be a local file path, an inline JSON dict on stdin, or
    # an s3://... URI that the state machine itself will resolve.
    manifest: dict | str
    if args.manifest.startswith("s3://"):
        manifest = args.manifest
    else:
        manifest = load_manifest_json(args.manifest)

    overrides: dict | None = None
    if args.storage_type or args.cache_id or args.networking_mode:
        overrides = {}
        if args.storage_type:
            overrides["storage_type"] = args.storage_type
        if args.cache_id:
            overrides["cache_id"] = args.cache_id
        if args.networking_mode:
            overrides["networking_mode"] = args.networking_mode

    result = submit_cohort(
        cohort_id=args.cohort_id,
        manifest=manifest,
        output_uri=args.output_uri,
        region=region,
        state_machine_arn=args.state_machine_arn,
        stack_name=args.stack_name or DEFAULT_STACK_NAME,
        overrides=overrides,
    )
    logger.info(
        "submitted cohort %s -> execution %s",
        result.cohort_id,
        result.execution_arn,
    )

    if not args.wait:
        sys.stdout.write(json.dumps(result.to_json_dict(), indent=2) + "\n")
        return 0

    sys.stdout.write(
        json.dumps(result.to_json_dict(), indent=2) + "\n"
    )
    sys.stdout.flush()

    def _progress(resp: dict) -> None:
        sys.stdout.write(format_progress(resp) + "\n")
        sys.stdout.flush()

    wait = wait_for_completion(
        execution_arn=result.execution_arn,
        region=region,
        progress_callback=_progress,
    )
    sys.stdout.write(
        json.dumps(
            {
                "cohort_id": result.cohort_id,
                "status": wait.status,
                "duration_seconds": wait.duration_seconds,
                "output": wait.output,
                "error": wait.error,
                "cause": wait.cause,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    return 0 if wait.status == "SUCCEEDED" else 5


def _infer_main_wdl(zf) -> str:  # type: ignore[no-untyped-def]
    names = zf.namelist()
    for candidate in ("main.wdl",):
        if candidate in names:
            return candidate
    for name in names:
        if name.endswith(".wdl") and "/" not in name:
            return name
    raise RuntimeError("no main .wdl in bundle ZIP")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gatk-sv-healthomics",
        description="GATK-SV HealthOmics migration CLI.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_package = sub.add_parser("package", help="Package a migrated module.")
    p_package.add_argument("--module", required=True)
    p_package.add_argument("--commit", required=True)
    p_package.add_argument("--output-dir", type=Path, default=None)
    p_package.set_defaults(func=_cmd_package)

    p_template = sub.add_parser("template", help="Generate a parameter template.")
    p_template.add_argument("--bundle", required=True, help="path to a bundle ZIP")
    p_template.add_argument("--main", default=None, help="main WDL path inside the ZIP")
    p_template.add_argument("--output", default=None)
    p_template.set_defaults(func=_cmd_template)

    p_vm = sub.add_parser("validate-manifest", help="Validate a sample manifest.")
    p_vm.add_argument("--manifest", required=True)
    p_vm.set_defaults(func=_cmd_validate_manifest)

    p_sr = sub.add_parser("stage-reference", help="Stage a reference bundle to S3.")
    p_sr.add_argument("--manifest", required=True)
    p_sr.add_argument("--bucket", required=True)
    p_sr.add_argument("--prefix", required=True)
    p_sr.set_defaults(func=_cmd_stage_reference)

    p_sub = sub.add_parser(
        "submit",
        help="Start a cohort end-to-end via the deployed Step Functions orchestrator.",
    )
    p_sub.add_argument(
        "--manifest",
        required=True,
        help="Local path to manifest.json, or an s3:// URI for an in-account manifest.",
    )
    p_sub.add_argument(
        "--cohort-id",
        required=True,
        help="Stable cohort identifier (also used as the execution name and the gatk-sv:cohort-id tag).",
    )
    p_sub.add_argument(
        "--output-uri",
        required=True,
        help="s3://bucket/prefix where every module's outputs will be written.",
    )
    p_sub.add_argument(
        "--region",
        default=None,
        help="AWS region. Defaults to $AWS_DEFAULT_REGION or ap-southeast-1.",
    )
    p_sub.add_argument(
        "--stack-name",
        default=None,
        help="CloudFormation stack to look up the state machine ARN from (default: GatkSvOrchestratorStack).",
    )
    p_sub.add_argument(
        "--state-machine-arn",
        default=None,
        help="Bypass stack lookup and use this ARN directly (test/debug).",
    )
    p_sub.add_argument(
        "--storage-type",
        choices=("DYNAMIC", "STATIC"),
        default=None,
        help="Override storage type. Default DYNAMIC.",
    )
    p_sub.add_argument(
        "--cache-id",
        default=None,
        help="Override the run cache ID.",
    )
    p_sub.add_argument(
        "--networking-mode",
        choices=("RESTRICTED", "VPC"),
        default=None,
        help="Override networking mode. Default RESTRICTED.",
    )
    p_sub.add_argument(
        "--wait",
        action="store_true",
        help="Block until the pipeline reaches a terminal status; print the cost report when done.",
    )
    p_sub.set_defaults(func=_cmd_submit)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
