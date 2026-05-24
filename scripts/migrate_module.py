"""Per-module migration driver for the GATK-SV HealthOmics migration.

Runs a single module through the pipeline end-to-end:

    fetch upstream → strip MELT → reject GCS URIs → lint locally →
    emit ZIP + divergence.json → generate parameter template

The workflow registration step is deferred unless ``--register`` is passed
and AWS credentials are available.

Usage:

    python gatk-sv-healthomics/scripts/migrate_module.py \\
        --module GatherSampleEvidence \\
        --commit 7eb2af1feea9
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--register", action="store_true", help="also register with HealthOmics")
    parser.add_argument("--semver", default="1.0.0")
    args = parser.parse_args()

    from kiro_life_sciences.gatk_sv_healthomics.models import MIGRATED_MODULES
    from kiro_life_sciences.gatk_sv_healthomics.packager import (
        lint_bundle,
        package_module,
    )

    if args.module not in MIGRATED_MODULES:
        logger.error(
            "module %s is not in MIGRATED_MODULES %s",
            args.module,
            list(MIGRATED_MODULES),
        )
        return 2

    logger.info("packaging module=%s commit=%s", args.module, args.commit)
    bundle = package_module(
        args.commit,
        args.module,
        output_dir=args.output_dir,
    )
    logger.info(
        "bundle written: %s (%d divergences)",
        bundle.zip_path,
        len(bundle.divergence),
    )

    lint = lint_bundle(bundle)
    if lint.status != "success":
        logger.error("local lint failed with %d errors", len(lint.errors))
        for err in lint.errors[:10]:
            logger.error("  %s", err)
        return 3
    logger.info("local lint status: %s", lint.status)

    # Generate parameter template from the main WDL inside the bundle.
    import tempfile
    import zipfile

    import WDL

    from kiro_life_sciences.gatk_sv_healthomics.template import (
        WdlInput,
        WdlWorkflow,
        generate_template,
    )

    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(bundle.zip_path) as zf:
            zf.extractall(tmp)
        main_path = Path(tmp) / bundle.main_wdl_path
        doc = WDL.load(str(main_path))
        if doc.workflow is None:
            logger.error("no workflow in %s", bundle.main_wdl_path)
            return 3

        inputs: list[WdlInput] = []
        for decl in doc.workflow.available_inputs:
            wdl_type = str(decl.value.type)
            optional = wdl_type.endswith("?")
            if optional:
                wdl_type = wdl_type[:-1]
            inputs.append(
                WdlInput(
                    name=decl.value.name, type=wdl_type, optional=optional, description=""
                )
            )
        workflow = WdlWorkflow(name=doc.workflow.name, inputs=tuple(inputs))
    template = generate_template(workflow)
    template_path = (
        Path("gatk-sv-healthomics/parameter-templates") / f"{args.module}.json"
    )
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(json.dumps(template.to_json_dict(), indent=2, sort_keys=True))
    logger.info("parameter template written: %s (%d inputs)", template_path, len(template.entries))

    if args.register:
        logger.info("registering with HealthOmics…")
        import boto3

        from kiro_life_sciences.gatk_sv_healthomics.models import ContainerRegistryMap
        from kiro_life_sciences.gatk_sv_healthomics.registrar import (
            RegistrationTarget,
            find_existing_workflow_id,
            load_workflow_versions,
            persist_workflow_version,
            register_module,
        )

        registry_path = Path("gatk-sv-healthomics/workflow-versions.json")
        existing = load_workflow_versions(registry_path)
        reg_map = ContainerRegistryMap.model_validate(
            json.loads(
                Path("gatk-sv-healthomics/container-registry-map/container-registry-map.json").read_text()
            )
        )
        client = boto3.client("omics", region_name="ap-southeast-1")  # type: ignore[attr-defined]
        record = register_module(
            client,
            bundle,
            template,
            reg_map,
            semver=args.semver,
            target=RegistrationTarget(
                module=args.module,  # type: ignore[arg-type]
                workflow_id=find_existing_workflow_id(existing, args.module),  # type: ignore[arg-type]
                container_registry_map_uri="s3://omics-ap-southeast-1/container-registry-map/container-registry-map.json",
                parameter_template_uri=f"s3://omics-ap-southeast-1/parameter-templates/{args.module}.json",
            ),
        )
        persist_workflow_version(record, registry_path)
        logger.info("registered %s → workflow_id=%s", args.module, record.workflow_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
