# Feature: gatk-sv-healthomics-migration, Property 1: Parameter-template round-trip
"""Property 1 — Parameter-template round-trip.

For any valid migrated WDL workflow ``w``, generating a Parameter_Template
from ``w`` and then validating the generated template against ``w`` SHALL
report a match with no missing and no extra inputs.

See design §Correctness Properties → Property 1 and §Components.c
(Parameter Template Generator + Validator).

**Validates: Requirements 4.2, 18.1, 18.2, 18.3**

This test is RED until Task 3.3.1 implements ``generate_template`` and
Task 3.3.2 implements ``validate_template``. Until then it fails at
runtime with ``NotImplementedError`` raised from the stubs in
``gatk_sv_aws.template``.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from gatk_sv_aws.template import (
    WdlInput,
    WdlWorkflow,
    generate_template,
    validate_template,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# WDL identifier rule: [A-Za-z_][A-Za-z0-9_]*, bounded length so shrinking
# produces readable counter-examples.
_wdl_identifier = st.from_regex(r"\A[A-Za-z_][A-Za-z0-9_]{0,39}\Z", fullmatch=True)

# Task 2.1 narrows the type pool to the six-element subset the property's
# strategy must draw from. The full :data:`models.ParameterType` literal has
# eight members; the extra two are reserved for Property 2/3 edge cases.
_wdl_type = st.sampled_from(
    ["File", "String", "Int", "Float", "Boolean", "Array[File]"]
)


@st.composite
def wdl_workflow_strategy(draw: st.DrawFn) -> WdlWorkflow:
    """Generate valid migrated WDL workflows for Property 1.

    Each workflow has:

    * a WDL-shaped ``name`` (1–40 chars, identifier rule),
    * 0–12 inputs with pairwise-unique names (identifier rule),
    * per-input ``type`` drawn from the six-member task-specified subset,
    * per-input ``optional`` boolean,
    * per-input free-text ``description`` (0–60 chars).
    """

    name = draw(_wdl_identifier)
    input_names = draw(
        st.lists(_wdl_identifier, min_size=0, max_size=12, unique=True)
    )
    n = len(input_names)
    types = draw(st.lists(_wdl_type, min_size=n, max_size=n))
    optionals = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    descriptions = draw(
        st.lists(st.text(min_size=0, max_size=60), min_size=n, max_size=n)
    )
    inputs = tuple(
        WdlInput(
            name=input_name,
            type=input_type,
            optional=optional,
            description=description,
        )
        for input_name, input_type, optional, description in zip(
            input_names, types, optionals, descriptions, strict=True
        )
    )
    return WdlWorkflow(name=name, inputs=inputs)


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------


@given(wdl=wdl_workflow_strategy())
def test_property_01_template_roundtrip(wdl: WdlWorkflow) -> None:
    """generate → validate round-trip returns an exact match."""

    tpl = generate_template(wdl)
    report = validate_template(tpl, wdl)

    assert report.is_match is True
    assert report.missing_inputs == ()
    assert report.extra_inputs == ()
