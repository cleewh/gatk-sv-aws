# Feature: gatk-sv-healthomics-migration, Property 3: Parameter-template detects extra inputs
"""Property 3 — Parameter-template detects extra inputs.

For any matched pair ``(template, wdl)`` and any identifier ``x`` not
declared as an input in ``wdl``, inserting a template entry for ``x`` and
then validating SHALL report ``x`` as an extra input.

See design §Correctness Properties → Property 3 and §Components.c
(Parameter Template Generator + Validator).

**Validates: Requirements 18.5**

This test is RED until Task 3.3.1 implements ``generate_template`` and
Task 3.3.2 implements ``validate_template``.
"""

from __future__ import annotations

from hypothesis import assume, given, strategies as st

from gatk_sv_aws.models import (
    ParameterTemplate,
    ParameterTemplateEntry,
)
from gatk_sv_aws.template import (
    WdlWorkflow,
    generate_template,
    validate_template,
)

from tests.properties.test_property_01_template_roundtrip import (
    wdl_workflow_strategy,
)

_wdl_identifier = st.from_regex(r"\A[A-Za-z_][A-Za-z0-9_]{0,39}\Z", fullmatch=True)
_wdl_type = st.sampled_from(
    ["File", "String", "Int", "Float", "Boolean", "Array[File]"]
)


@given(wdl=wdl_workflow_strategy(), extra_name=_wdl_identifier, extra_type=_wdl_type)
def test_property_03_extra_input(
    wdl: WdlWorkflow, extra_name: str, extra_type: str
) -> None:
    """Adding an identifier not declared in the WDL ⇒ validator reports extra."""

    declared = {inp.name for inp in wdl.inputs}
    assume(extra_name not in declared)

    tpl = generate_template(wdl)
    mutated_entries = dict(tpl.entries)
    mutated_entries[extra_name] = ParameterTemplateEntry(
        description="synthetic extra input",
        optional=False,
        type=extra_type,
    )
    mutated = ParameterTemplate(entries=mutated_entries)

    report = validate_template(mutated, wdl)

    assert report.is_match is False
    assert extra_name in report.extra_inputs
