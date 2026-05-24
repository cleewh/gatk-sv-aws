# Feature: gatk-sv-healthomics-migration, Property 2: Parameter-template detects missing required inputs
"""Property 2 — Parameter-template detects missing required inputs.

For any matched pair ``(template, wdl)`` and any required input ``i``
declared in ``wdl``, removing the entry for ``i`` from ``template`` and
then validating SHALL report ``i`` as a missing input.

See design §Correctness Properties → Property 2 and §Components.c
(Parameter Template Generator + Validator).

**Validates: Requirements 18.4**

This test is RED until Task 3.3.1 implements ``generate_template`` and
Task 3.3.2 implements ``validate_template``.
"""

from __future__ import annotations

from hypothesis import assume, given, strategies as st

from gatk_sv_aws.models import ParameterTemplate
from gatk_sv_aws.template import (
    WdlWorkflow,
    generate_template,
    validate_template,
)

from tests.properties.test_property_01_template_roundtrip import (
    wdl_workflow_strategy,
)


@given(wdl=wdl_workflow_strategy(), data=st.data())
def test_property_02_missing_required(wdl: WdlWorkflow, data: st.DataObject) -> None:
    """Removing a required input from a matched template ⇒ validator reports missing."""

    required_names = [inp.name for inp in wdl.inputs if not inp.optional]
    assume(required_names)  # only meaningful when at least one required input exists

    target = data.draw(st.sampled_from(required_names))

    tpl = generate_template(wdl)
    mutated_entries = {name: entry for name, entry in tpl.entries.items() if name != target}
    mutated = ParameterTemplate(entries=mutated_entries)

    report = validate_template(mutated, wdl)

    assert report.is_match is False
    assert target in report.missing_inputs
