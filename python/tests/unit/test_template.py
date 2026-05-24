"""Unit tests for the Parameter Template Generator + Validator (Task 3.3.4).

Example-based tests for the component (c) API. Covers File / String / Int /
Array[File] inputs, optional defaults, duplicate input rejection at the
generator, and the ValidationReport shapes produced by each divergence
between template and WDL (Req 4.2, 4.4, 4.5).
"""

from __future__ import annotations

import pytest

from gatk_sv_aws.models import (
    ParameterTemplate,
    ParameterTemplateEntry,
)
from gatk_sv_aws.template import (
    WdlInput,
    WdlWorkflow,
    generate_template,
    validate_template,
)


# ---------------------------------------------------------------------------
# generate_template
# ---------------------------------------------------------------------------


def test_generate_template_empty_workflow() -> None:
    wdl = WdlWorkflow(name="Empty", inputs=())
    tpl = generate_template(wdl)
    assert tpl.entries == {}


def test_generate_template_file_input_required() -> None:
    wdl = WdlWorkflow(
        name="Wf",
        inputs=(WdlInput(name="reference_fasta", type="File", optional=False),),
    )
    tpl = generate_template(wdl)
    assert set(tpl.entries.keys()) == {"reference_fasta"}
    entry = tpl.entries["reference_fasta"]
    assert entry.type == "File"
    assert entry.optional is False


def test_generate_template_all_primitive_types() -> None:
    wdl = WdlWorkflow(
        name="Wf",
        inputs=(
            WdlInput(name="ref", type="File", optional=False),
            WdlInput(name="name", type="String", optional=False),
            WdlInput(name="count", type="Int", optional=False),
            WdlInput(name="threshold", type="Float", optional=True),
            WdlInput(name="flag", type="Boolean", optional=True),
            WdlInput(name="reads", type="Array[File]", optional=False),
        ),
    )
    tpl = generate_template(wdl)
    types = {name: entry.type for name, entry in tpl.entries.items()}
    assert types == {
        "ref": "File",
        "name": "String",
        "count": "Int",
        "threshold": "Float",
        "flag": "Boolean",
        "reads": "Array[File]",
    }
    optionals = {name: entry.optional for name, entry in tpl.entries.items()}
    assert optionals["threshold"] is True
    assert optionals["flag"] is True
    assert optionals["ref"] is False


def test_generate_template_preserves_description() -> None:
    wdl = WdlWorkflow(
        name="Wf",
        inputs=(
            WdlInput(
                name="ref",
                type="File",
                optional=False,
                description="GRCh38 primary assembly FASTA",
            ),
        ),
    )
    tpl = generate_template(wdl)
    assert tpl.entries["ref"].description == "GRCh38 primary assembly FASTA"


def test_generate_template_generates_default_description_when_blank() -> None:
    wdl = WdlWorkflow(
        name="Wf",
        inputs=(WdlInput(name="ref", type="File", optional=False),),
    )
    tpl = generate_template(wdl)
    assert "ref" in tpl.entries["ref"].description


def test_generate_template_rejects_duplicate_inputs() -> None:
    wdl = WdlWorkflow(
        name="Wf",
        inputs=(
            WdlInput(name="dup", type="File", optional=False),
            WdlInput(name="dup", type="String", optional=True),
        ),
    )
    with pytest.raises(ValueError, match="duplicate"):
        generate_template(wdl)


# ---------------------------------------------------------------------------
# validate_template
# ---------------------------------------------------------------------------


def _sample_wdl() -> WdlWorkflow:
    return WdlWorkflow(
        name="Wf",
        inputs=(
            WdlInput(name="ref", type="File", optional=False),
            WdlInput(name="batch_name", type="String", optional=False),
            WdlInput(name="threshold", type="Float", optional=True),
        ),
    )


def test_validate_template_exact_match() -> None:
    wdl = _sample_wdl()
    tpl = generate_template(wdl)
    report = validate_template(tpl, wdl)
    assert report.is_match is True
    assert report.missing_inputs == ()
    assert report.extra_inputs == ()


def test_validate_template_missing_required_input() -> None:
    wdl = _sample_wdl()
    tpl = generate_template(wdl)
    mutated = ParameterTemplate(
        entries={k: v for k, v in tpl.entries.items() if k != "ref"}
    )
    report = validate_template(mutated, wdl)
    assert report.is_match is False
    assert "ref" in report.missing_inputs
    assert report.extra_inputs == ()


def test_validate_template_missing_optional_input_also_reported() -> None:
    wdl = _sample_wdl()
    tpl = generate_template(wdl)
    mutated = ParameterTemplate(
        entries={k: v for k, v in tpl.entries.items() if k != "threshold"}
    )
    report = validate_template(mutated, wdl)
    assert report.is_match is False
    assert "threshold" in report.missing_inputs


def test_validate_template_extra_input() -> None:
    wdl = _sample_wdl()
    tpl = generate_template(wdl)
    mutated_entries = dict(tpl.entries)
    mutated_entries["bogus"] = ParameterTemplateEntry(
        description="extra", optional=False, type="String"
    )
    mutated = ParameterTemplate(entries=mutated_entries)
    report = validate_template(mutated, wdl)
    assert report.is_match is False
    assert "bogus" in report.extra_inputs
    assert report.missing_inputs == ()


def test_validate_template_both_missing_and_extra() -> None:
    wdl = _sample_wdl()
    tpl = generate_template(wdl)
    mutated_entries = {k: v for k, v in tpl.entries.items() if k != "ref"}
    mutated_entries["extra"] = ParameterTemplateEntry(
        description="x", optional=True, type="Int"
    )
    mutated = ParameterTemplate(entries=mutated_entries)
    report = validate_template(mutated, wdl)
    assert report.is_match is False
    assert "ref" in report.missing_inputs
    assert "extra" in report.extra_inputs


# ---------------------------------------------------------------------------
# HealthOmics-native JSON shape (Data Models → Parameter Template)
# ---------------------------------------------------------------------------


def test_parameter_template_json_roundtrip() -> None:
    wdl = _sample_wdl()
    tpl = generate_template(wdl)
    flat = tpl.to_json_dict()
    # Flat shape: keys are input names, values are dicts with description/optional/type.
    assert set(flat.keys()) == {"ref", "batch_name", "threshold"}
    assert set(flat["ref"].keys()) == {"description", "optional", "type"}
    # Round-trip via the HealthOmics-native shape.
    restored = ParameterTemplate.from_json_dict(flat)
    report = validate_template(restored, wdl)
    assert report.is_match is True
