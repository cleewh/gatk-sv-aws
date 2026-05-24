"""Generate parameter templates for every committed bundle.

Walks ``gatk-sv-healthomics/wdl/bundles/<module>/<module>-bundle.zip`` and
writes the corresponding template JSON under
``gatk-sv-healthomics/parameter-templates/<module>.json``. Idempotent.

Usage:

    python gatk-sv-healthomics/scripts/generate_all_templates.py
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLES_DIR = REPO_ROOT / "gatk-sv-healthomics" / "wdl" / "bundles"
TEMPLATES_DIR = REPO_ROOT / "gatk-sv-healthomics" / "parameter-templates"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from kiro_life_sciences.gatk_sv_healthomics.models import MIGRATED_MODULES
    from kiro_life_sciences.gatk_sv_healthomics.template import (
        WdlInput,
        WdlWorkflow,
        generate_template,
    )

    import WDL

    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for module in MIGRATED_MODULES:
        bundle_zip = BUNDLES_DIR / module / f"{module}-bundle.zip"
        if not bundle_zip.exists():
            logger.warning("skip %s: %s missing", module, bundle_zip)
            continue

        main_wdl_path = f"wdl/{module}.wdl"

        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(bundle_zip) as zf:
                zf.extractall(tmp)
            doc = WDL.load(str(Path(tmp) / main_wdl_path))

            if doc.workflow is None:
                logger.warning("%s has no workflow; skipping", module)
                continue

            wdl_inputs: list[WdlInput] = []
            skipped: list[tuple[str, str]] = []
            allowed = {
                "File",
                "String",
                "Int",
                "Float",
                "Boolean",
                "Array[File]",
                "Array[String]",
                "Array[Int]",
            }
            # Use `.inputs` (workflow-level declarations) rather than
            # `.available_inputs` (which includes call-level inputs and can
            # contain duplicates when the workflow wires the same name into
            # multiple subcalls).
            workflow_inputs = doc.workflow.inputs or []
            for decl in workflow_inputs:
                wdl_type = str(decl.type)
                optional = wdl_type.endswith("?")
                if optional:
                    wdl_type = wdl_type[:-1]
                if wdl_type not in allowed:
                    skipped.append((decl.name, wdl_type))
                    continue
                wdl_inputs.append(
                    WdlInput(
                        name=decl.name,
                        type=wdl_type,
                        optional=optional,
                        description="",
                    )
                )
            workflow = WdlWorkflow(name=doc.workflow.name, inputs=tuple(wdl_inputs))
        template = generate_template(workflow)
        if skipped:
            logger.info(
                "  %s: skipped %d struct-typed inputs (%s)",
                module,
                len(skipped),
                ", ".join(f"{n}:{t}" for n, t in skipped[:3])
                + ("…" if len(skipped) > 3 else ""),
            )

        out_path = TEMPLATES_DIR / f"{module}.json"
        out_path.write_text(
            json.dumps(template.to_json_dict(), indent=2, sort_keys=True)
        )
        logger.info(
            "wrote %s (%d inputs)", out_path.relative_to(REPO_ROOT), len(template.entries)
        )
        total += 1

    logger.info("generated %d templates", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
