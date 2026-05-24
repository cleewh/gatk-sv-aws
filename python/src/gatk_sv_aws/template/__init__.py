"""Component (c): Parameter Template Generator + Validator for the GATK-SV migration.

Implements design §Components and interfaces → (c) Parameter Template
Generator + Validator. Parses each migrated workflow's WDL inputs and emits
a Parameter_Template JSON document, and validates a supplied template
against a WDL workflow to report missing or extra inputs.

Advances Requirements 4 (Parameter Templates) and 18 (Parser Round-Trip for
Parameter Template).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gatk_sv_aws.models import (
    ParameterTemplate,
    ParameterTemplateEntry,
)


@dataclass(frozen=True)
class WdlInput:
    """Minimal WDL input description used by the template generator/validator.

    This is the in-memory shape the Packager (§Components.a) surfaces to the
    Parameter Template Generator (§Components.c). A full WDL AST is not
    needed for Property 1/2/3; the generator only consumes per-input
    ``name``, ``type``, ``optional`` (plus an optional free-text
    ``description`` carried through from doc comments in the WDL).
    """

    name: str
    type: str  # one of models.ParameterType literals, validated downstream
    optional: bool
    description: str = ""


@dataclass(frozen=True)
class WdlWorkflow:
    """Minimal WDL workflow description used by the template generator/validator."""

    name: str
    inputs: tuple[WdlInput, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ValidationReport:
    """Output of :func:`validate_template`.

    ``is_match`` is True iff the template's input-name set exactly matches the
    WDL's input-name set AND every ``File`` typed entry has an S3 URI in
    Target_Region when the caller supplied input values. Missing/extra are
    reported per Req 18.4/18.5.
    """

    is_match: bool
    missing_inputs: tuple[str, ...] = ()
    extra_inputs: tuple[str, ...] = ()


def generate_template(wdl: WdlWorkflow) -> ParameterTemplate:
    """Generate a Parameter_Template from a WDL workflow description.

    For each declared input of ``wdl``, emit a :class:`ParameterTemplateEntry`
    with ``description`` (carried through from the WDL doc comment if any,
    otherwise a generic string identifying the input), ``type`` (verbatim
    from the WDL declaration), and ``optional`` (verbatim from the WDL
    declaration).

    Implementation target of Task 3.3.1 (Property 1, Req 4.1, 4.2, 18.1).
    """
    entries: dict[str, ParameterTemplateEntry] = {}
    seen: set[str] = set()
    for inp in wdl.inputs:
        if inp.name in seen:
            raise ValueError(
                f"duplicate WDL input name {inp.name!r} in workflow {wdl.name!r}"
            )
        seen.add(inp.name)
        description = (
            inp.description
            if inp.description.strip()
            else f"Input {inp.name} for workflow {wdl.name}"
        )
        entries[inp.name] = ParameterTemplateEntry(
            description=description,
            optional=inp.optional,
            type=inp.type,  # type: ignore[arg-type]
        )
    return ParameterTemplate(entries=entries)


def validate_template(
    template: ParameterTemplate, wdl: WdlWorkflow
) -> ValidationReport:
    """Validate a Parameter_Template against a WDL workflow.

    Compares the template's key set with the WDL's declared-input set and
    reports every name present in one but not the other. ``is_match`` is
    True iff both ``missing_inputs`` and ``extra_inputs`` are empty.

    Implementation target of Task 3.3.2 (Properties 1/2/3, Req 4.2, 18.2,
    18.4, 18.5).
    """
    wdl_names = {inp.name for inp in wdl.inputs}
    tpl_names = set(template.entries.keys())

    missing = tuple(sorted(wdl_names - tpl_names))
    extra = tuple(sorted(tpl_names - wdl_names))

    return ValidationReport(
        is_match=not missing and not extra,
        missing_inputs=missing,
        extra_inputs=extra,
    )


__all__ = [
    "WdlInput",
    "WdlWorkflow",
    "ValidationReport",
    "generate_template",
    "validate_template",
]
