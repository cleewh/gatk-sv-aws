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

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
