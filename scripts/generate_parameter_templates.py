#!/usr/bin/env python3
"""Generate parameter-templates/<Module>.json for each Phase 8 (Req 19) module.

Reads the workflow input declarations from each bundle's main WDL and writes a
HealthOmics parameter template JSON. Format matches the existing templates
(parameter-templates/AnnotateVcf.json, etc.).

Re-running is idempotent.

Usage:
    .venv/bin/python scripts/generate_parameter_templates.py

For Phase 8 (Req 19) modules only — the existing 10 templates from Tasks 4.2-4.11
are not regenerated.
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (module_name, main_wdl_path_in_zip)
PHASE_8_BUNDLES = [
    ("EvidenceQC",            "wdl/EvidenceQC.wdl"),
    ("RefineComplexVariants", "wdl/RefineComplexVariants.wdl"),
    ("JoinRawCalls",          "wdl/JoinRawCalls.wdl"),
    ("SVConcordance",         "wdl/SVConcordance.wdl"),
    ("ScoreGenotypes",        "wdl/ScoreGenotypes.wdl"),
    ("FilterGenotypes",       "wdl/FilterGenotypes.wdl"),
    ("MainVcfQC",             "wdl/MainVcfQc.wdl"),
    ("VisualizeCnvs",         "wdl/VisualizeCnvs.wdl"),
]


# Regex matchers for WDL workflow input blocks.
# Match: workflow Name { input { ... } ...
WORKFLOW_INPUT_BLOCK = re.compile(
    r"^workflow\s+\w+\s*\{[^}]*?input\s*\{(.*?)\}",
    re.MULTILINE | re.DOTALL,
)

# Match: <Type> <name> [= default]?
# Types: File | String | Int | Float | Boolean | Array[Type] | Type? | Map[K,V]
INPUT_DECL = re.compile(
    r"""
    \s*
    (?P<type>(?:Array\[[^\]]+\][?+]?|Map\[[^\]]+\][?]?|[A-Z]\w*[?]?))
    \s+
    (?P<name>\w+)
    (?:\s*=\s*(?P<default>[^\n]+?))?
    \s*$
    """,
    re.VERBOSE | re.MULTILINE,
)


def load_main_wdl(bundle_zip: Path, main_path: str) -> str:
    with zipfile.ZipFile(bundle_zip) as z:
        return z.read(main_path).decode("utf-8")


def parse_workflow_inputs(wdl_text: str) -> list[dict]:
    """Return [{name, type, optional, has_default}] for each workflow input."""
    m = WORKFLOW_INPUT_BLOCK.search(wdl_text)
    if not m:
        return []
    block = m.group(1)

    inputs: list[dict] = []
    seen_names: set[str] = set()
    # The block can have nested braces (e.g. struct refs), so we filter
    # any line that doesn't look like a valid declaration.
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip inline comments.
        if "#" in stripped:
            stripped = stripped.split("#", 1)[0].rstrip()
        # Anchor against the line content as a single-line input decl.
        match = INPUT_DECL.match(stripped) if stripped else None
        if not match:
            continue
        name = match.group("name")
        if name in seen_names:
            continue
        seen_names.add(name)
        wdl_type = match.group("type")
        # An input is "optional" in HealthOmics terms if it is either:
        #   1. A WDL ?-suffixed type (File?, String?, ...)
        #   2. Has a default value
        is_optional = wdl_type.endswith("?") or match.group("default") is not None
        # Strip the trailing ? from the declared type for the template; keep
        # base form (File, String, Array[File], etc.).
        clean_type = wdl_type.rstrip("?+")
        inputs.append({
            "name": name,
            "type": clean_type,
            "optional": is_optional,
        })
    return inputs


def format_template(module_name: str, inputs: list[dict]) -> dict:
    """Convert parsed inputs to a HealthOmics-format parameter template dict."""
    return {
        inp["name"]: {
            "description": f"Input {inp['name']} for workflow {module_name}",
            "optional": inp["optional"],
            "type": inp["type"],
        }
        for inp in sorted(inputs, key=lambda x: x["name"])
    }


def main() -> int:
    output_dir = ROOT / "parameter-templates"
    bundle_dir = ROOT / "wdl" / "bundles"

    n_emitted = 0
    for module, main_wdl in PHASE_8_BUNDLES:
        bundle_zip = bundle_dir / module / f"{module}-bundle.zip"
        if not bundle_zip.exists():
            print(f"  SKIP {module}: bundle not found at {bundle_zip}", file=sys.stderr)
            continue

        wdl_text = load_main_wdl(bundle_zip, main_wdl)
        inputs = parse_workflow_inputs(wdl_text)
        if not inputs:
            print(f"  WARN {module}: no workflow inputs detected (regex may need tuning)",
                  file=sys.stderr)
            continue

        template = format_template(module, inputs)
        target = output_dir / f"{module}.json"
        target.write_text(json.dumps(template, indent=2, sort_keys=True))
        print(f"  OK   {module}: {len(inputs)} inputs -> {target.relative_to(ROOT)}")
        n_emitted += 1

    print(f"\nDone: {n_emitted} parameter templates emitted under {output_dir.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
