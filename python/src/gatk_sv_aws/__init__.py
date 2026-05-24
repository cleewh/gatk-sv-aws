"""GATK-SV HealthOmics migration package for Kiro for Life Sciences.

This package implements the Migration_System that ports the Broad Institute's
GATK-SV WDL pipeline (originally Terra/Cromwell on GCP) to AWS HealthOmics in
``ap-southeast-1``. It is the code home for the components described in the
spec at ``.kiro/specs/gatk-sv-healthomics-migration/`` (see ``design.md``
§Components and interfaces a–j, and ``requirements.md`` Reqs 1–18).

The package is organized as ten sub-packages, one per design component:

* :mod:`.packager` — (a) WDL Packager & Linter (Req 2, 2a)
* :mod:`.registry` — (b) Container Registry Map Builder (Req 3)
* :mod:`.template` — (c) Parameter Template Generator + Validator (Req 4, 18)
* :mod:`.reference` — (d) Reference Bundle Stager (Req 5)
* :mod:`.iam` — (e) IAM Role Synthesizer (Req 12)
* :mod:`.registrar` — (f) Workflow Registrar (Req 16)
* :mod:`.orchestrator` — (g) Run Orchestrator (Req 6, 7, 10, 11, 14, 15)
* :mod:`.cost` — (h) Cost Optimizer (Req 8, 9, 13)
* :mod:`.monitoring` — (i) Monitoring & Diagnostics (Req 14, 15)
* :mod:`.validation` — (j) Validation Harness (Req 13)

The ``__version__`` below is a scaffolding placeholder; the workflow
Registrar component (sub-package :mod:`.registrar`) will bump it when
HealthOmics workflow versions are published (Req 16.1, 16.2).
"""

from gatk_sv_aws import (
    cost,
    iam,
    monitoring,
    orchestrator,
    packager,
    reference,
    registrar,
    registry,
    template,
    validation,
)

__version__ = "0.0.0"

__all__ = [
    "packager",
    "registry",
    "template",
    "reference",
    "iam",
    "registrar",
    "orchestrator",
    "cost",
    "monitoring",
    "validation",
]
