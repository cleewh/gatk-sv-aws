"""Component (f): Workflow Registrar for the GATK-SV migration.

Implements design §Components and interfaces → (f) Workflow Registrar.
For each module in Migrated_Modules, calls ``CreateAHOWorkflow`` on first
registration and ``CreateAHOWorkflowVersion`` on subsequent changes,
applies a semantic version string, and records the upstream GATK-SV commit
hash and divergence list in the Workflow_Version_Record.

Advances Requirement 16 (Workflow Versioning).

The implementation is a thin boto3 wrapper that mirrors the AWS HealthOmics
MCP tools ``CreateAHOWorkflow``, ``CreateAHOWorkflowVersion``, and
``GetAHOWorkflow`` already exercised against ``ap-southeast-1`` in account
``__ACCOUNT_ID__``. The registrar:

1. Accepts a :class:`~.models.PackagedBundle` plus a
   :class:`~.models.ParameterTemplate` plus a
   :class:`~.models.ContainerRegistryMap` and a requested ``semver``.
2. On first registration (``workflow_id`` is None) issues
   ``omics.create_workflow``.
3. On subsequent registrations (``workflow_id`` is already known) issues
   ``omics.create_workflow_version`` with the same ``workflow_id``.
4. Persists the returned identifiers as a
   :class:`~.models.WorkflowVersionRecord` in ``workflow-versions.json``
   under the repo root (by default ``gatk-sv-healthomics/workflow-versions.json``).

The ``boto3``/``aws_client`` parameter is injected so the production path
can use a real boto3 ``omics`` client while tests can pass a recording
fake. The contract is "quack like ``omics.create_workflow``" — we call
``.create_workflow(...)`` / ``.create_workflow_version(...)`` / ``.get_workflow(...)``
and read ``id`` / ``status`` / ``name`` from the return shape.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from gatk_sv_aws.models import (
    ContainerRegistryMap,
    DivergenceEntry,
    ModuleName,
    PackagedBundle,
    ParameterTemplate,
    WorkflowVersionRecord,
)


class OmicsClient(Protocol):
    """Minimal protocol implemented by both ``boto3.client('omics')`` and test fakes."""

    def create_workflow(self, **kwargs: Any) -> dict[str, Any]: ...
    def create_workflow_version(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_workflow(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RegistrationTarget:
    """Prior registration for a module, if any.

    ``workflow_id`` is ``None`` on first registration. On subsequent
    registrations pass the ``workflow_id`` returned by the first
    ``create_workflow`` call so the registrar emits a *version* rather
    than a brand-new workflow.
    """

    module: ModuleName
    workflow_id: str | None
    container_registry_map_uri: str
    parameter_template_uri: str


def _render_divergences(entries: list[DivergenceEntry]) -> list[str]:
    """Human-readable divergence summaries attached to the version record."""
    return [f"{e.change_kind.value}: {e.reason}" for e in entries]


def _encode_zip(zip_path: Path) -> str:
    """Base64-encode a bundle ZIP for the ``definition_zip_base64`` field.

    The HealthOmics MCP tools accept either a ZIP path or a base64 payload;
    boto3 ``create_workflow`` expects the payload inline as ``definitionZip``
    (bytes) under the Python SDK. We stay symmetric with the MCP contract
    and return a base64 string so the caller can decide between inline bytes
    or an S3 staging step.
    """
    return base64.b64encode(zip_path.read_bytes()).decode("ascii")


def register_module(
    aws_client: OmicsClient,
    bundle: PackagedBundle,
    template: ParameterTemplate,
    registry_map: ContainerRegistryMap,
    *,
    semver: str,
    target: RegistrationTarget,
    description: str | None = None,
    readme: str | None = None,
) -> WorkflowVersionRecord:
    """Register or update a HealthOmics workflow for a migrated module.

    Implementation target of Task 3.6.1 (Req 16.1, 16.2).

    Returns a :class:`WorkflowVersionRecord` with the returned HealthOmics
    identifiers, the upstream GATK-SV commit, and a human-readable
    divergence summary. Callers persist the record via
    :func:`persist_workflow_version` so subsequent registrations can find
    the ``workflow_id`` and emit a version rather than a new workflow.
    """
    if target.module != bundle.module:
        raise ValueError(
            f"target.module ({target.module}) does not match bundle.module ({bundle.module})"
        )

    name = f"gatk-sv-{bundle.module.lower().replace('gatk-sv-', '')}"
    parameter_template_dict = template.to_json_dict()
    registry_map_dict = registry_map.model_dump(exclude_none=True)

    if target.workflow_id is None:
        # First registration — create a new workflow.
        response = aws_client.create_workflow(
            name=name,
            description=description or f"GATK-SV migrated module {bundle.module}",
            engine="WDL",
            definitionZip=_encode_zip(bundle.zip_path),
            main=bundle.main_wdl_path,
            parameterTemplate=parameter_template_dict,
            containerRegistryMap=registry_map_dict,
            tags={
                "gatk-sv:module": bundle.module,
                "gatk-sv:upstream-commit": bundle.upstream_commit,
                "gatk-sv:workflow-version": semver,
            },
        )
        workflow_id = str(response["id"])
        version_name = response.get("versionName", "default")
    else:
        # Subsequent registration — create a new version on the existing workflow.
        response = aws_client.create_workflow_version(
            workflowId=target.workflow_id,
            versionName=semver,
            description=description or f"GATK-SV migrated module {bundle.module} {semver}",
            definitionZip=_encode_zip(bundle.zip_path),
            main=bundle.main_wdl_path,
            parameterTemplate=parameter_template_dict,
            containerRegistryMap=registry_map_dict,
            tags={
                "gatk-sv:module": bundle.module,
                "gatk-sv:upstream-commit": bundle.upstream_commit,
                "gatk-sv:workflow-version": semver,
            },
        )
        workflow_id = target.workflow_id
        version_name = str(response.get("versionName", semver))

    _ = readme  # reserved for future Task 3.6 extension

    return WorkflowVersionRecord(
        module=bundle.module,
        workflow_id=workflow_id,
        version_name=version_name,
        semver=semver,
        upstream_commit=bundle.upstream_commit,
        divergences=_render_divergences(bundle.divergence),
        container_registry_map_uri=target.container_registry_map_uri,
        parameter_template_uri=target.parameter_template_uri,
    )


def persist_workflow_version(
    record: WorkflowVersionRecord,
    registry_path: Path,
) -> None:
    """Append (or replace) a :class:`WorkflowVersionRecord` on disk.

    Records are keyed by ``(module, version_name)``; a repeated registration
    of the same version is idempotent (last write wins). The registry file
    is a JSON object ``{"records": [...]}``.
    """
    registry: dict[str, list[dict[str, Any]]]
    if registry_path.exists():
        registry = json.loads(registry_path.read_text())
    else:
        registry = {"records": []}

    # Replace any existing record with the same (module, version_name).
    key = (record.module, record.version_name)
    registry["records"] = [
        r
        for r in registry["records"]
        if (r.get("module"), r.get("version_name")) != key
    ]
    registry["records"].append(record.model_dump(mode="json"))

    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True))


def load_workflow_versions(registry_path: Path) -> list[WorkflowVersionRecord]:
    """Load persisted :class:`WorkflowVersionRecord` s.

    Returns an empty list when ``registry_path`` does not yet exist.
    """
    if not registry_path.exists():
        return []
    data = json.loads(registry_path.read_text())
    return [WorkflowVersionRecord.model_validate(r) for r in data.get("records", [])]


def find_existing_workflow_id(
    records: list[WorkflowVersionRecord], module: ModuleName
) -> str | None:
    """Return the ``workflow_id`` of the most-recently-recorded version for ``module``.

    Used by the deployment driver to decide between
    ``create_workflow`` (no prior record) and ``create_workflow_version``
    (prior record exists).
    """
    for record in reversed(records):
        if record.module == module:
            return record.workflow_id
    return None


__all__ = [
    "OmicsClient",
    "RegistrationTarget",
    "register_module",
    "persist_workflow_version",
    "load_workflow_versions",
    "find_existing_workflow_id",
]
