"""Unit tests for the GATK-SV Workflow Registrar (Design §Components.f).

Exercises the boto3-shaped contract of :func:`register_module` and the
persistence round-trip through :func:`persist_workflow_version` /
:func:`load_workflow_versions`.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest

from gatk_sv_aws.models import (
    ChangeKind,
    ContainerRegistryMap,
    DivergenceEntry,
    PackagedBundle,
    ParameterTemplate,
    ParameterTemplateEntry,
    RegistryMapping,
    WorkflowVersionRecord,
)
from gatk_sv_aws.registrar import (
    RegistrationTarget,
    find_existing_workflow_id,
    load_workflow_versions,
    persist_workflow_version,
    register_module,
)


class RecordingOmicsClient:
    """Fake implementing the :class:`~.registrar.OmicsClient` protocol."""

    def __init__(self, workflow_id: str = "1234567", version_name: str = "1.0.0") -> None:
        self.workflow_id = workflow_id
        self.version_name = version_name
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create_workflow(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_workflow", dict(kwargs)))
        return {"id": self.workflow_id, "name": kwargs["name"], "status": "CREATING"}

    def create_workflow_version(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_workflow_version", dict(kwargs)))
        return {
            "workflowId": kwargs["workflowId"],
            "versionName": kwargs["versionName"],
            "status": "CREATING",
        }

    def get_workflow(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_workflow", dict(kwargs)))
        return {"id": kwargs["id"], "status": "ACTIVE"}


def _make_bundle(tmp_path: Path, *, module: str = "GatherSampleEvidence") -> PackagedBundle:
    zip_path = tmp_path / f"{module}-bundle.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("main.wdl", "version 1.0\nworkflow x { input { File ref } }\n")
    divergences = [
        DivergenceEntry(
            module=module,  # type: ignore[arg-type]
            upstream_path="wdl/MELT.wdl",
            change_kind=ChangeKind.REMOVE_CALLER,
            reason="MELT excluded per Req 2a.3",
            upstream_commit="7eb2af1feea9",
        )
    ]
    return PackagedBundle(
        zip_path=zip_path,
        main_wdl_path="main.wdl",
        module=module,  # type: ignore[arg-type]
        upstream_commit="7eb2af1feea9",
        divergence=divergences,
    )


def _make_template() -> ParameterTemplate:
    return ParameterTemplate(
        entries={
            "ref": ParameterTemplateEntry(
                description="Reference FASTA.",
                optional=False,
                type="File",
            )
        }
    )


def _make_map() -> ContainerRegistryMap:
    return ContainerRegistryMap(
        registryMappings=[
            RegistryMapping(upstreamRegistryUrl="quay.io", ecrRepositoryPrefix="quay")
        ]
    )


def test_first_registration_calls_create_workflow(tmp_path: Path) -> None:
    client = RecordingOmicsClient(workflow_id="9690943")
    bundle = _make_bundle(tmp_path)

    record = register_module(
        client,
        bundle,
        _make_template(),
        _make_map(),
        semver="1.0.0",
        target=RegistrationTarget(
            module="GatherSampleEvidence",
            workflow_id=None,
            container_registry_map_uri="s3://bkt/reg-map.json",
            parameter_template_uri="s3://bkt/tpl.json",
        ),
    )

    assert len(client.calls) == 1
    action, kwargs = client.calls[0]
    assert action == "create_workflow"
    assert kwargs["name"] == "gatk-sv-gathersampleevidence"
    assert kwargs["engine"] == "WDL"
    assert kwargs["main"] == "main.wdl"
    assert kwargs["parameterTemplate"] == {
        "ref": {"description": "Reference FASTA.", "optional": False, "type": "File"}
    }
    assert kwargs["tags"]["gatk-sv:module"] == "GatherSampleEvidence"
    assert kwargs["tags"]["gatk-sv:upstream-commit"] == "7eb2af1feea9"

    assert isinstance(record, WorkflowVersionRecord)
    assert record.workflow_id == "9690943"
    assert record.semver == "1.0.0"
    assert record.upstream_commit == "7eb2af1feea9"
    assert record.divergences == ["remove_caller: MELT excluded per Req 2a.3"]


def test_subsequent_registration_calls_create_workflow_version(tmp_path: Path) -> None:
    client = RecordingOmicsClient()
    bundle = _make_bundle(tmp_path)

    record = register_module(
        client,
        bundle,
        _make_template(),
        _make_map(),
        semver="1.1.0",
        target=RegistrationTarget(
            module="GatherSampleEvidence",
            workflow_id="9690943",
            container_registry_map_uri="s3://bkt/reg-map.json",
            parameter_template_uri="s3://bkt/tpl.json",
        ),
    )

    assert len(client.calls) == 1
    action, kwargs = client.calls[0]
    assert action == "create_workflow_version"
    assert kwargs["workflowId"] == "9690943"
    assert kwargs["versionName"] == "1.1.0"
    assert record.workflow_id == "9690943"
    assert record.version_name == "1.1.0"


def test_mismatched_module_raises(tmp_path: Path) -> None:
    client = RecordingOmicsClient()
    bundle = _make_bundle(tmp_path, module="GatherSampleEvidence")

    with pytest.raises(ValueError, match="target.module"):
        register_module(
            client,
            bundle,
            _make_template(),
            _make_map(),
            semver="1.0.0",
            target=RegistrationTarget(
                module="AnnotateVcf",
                workflow_id=None,
                container_registry_map_uri="s3://bkt/reg-map.json",
                parameter_template_uri="s3://bkt/tpl.json",
            ),
        )


def test_persist_and_load_round_trip(tmp_path: Path) -> None:
    client = RecordingOmicsClient(workflow_id="9690943")
    record = register_module(
        client,
        _make_bundle(tmp_path),
        _make_template(),
        _make_map(),
        semver="1.0.0",
        target=RegistrationTarget(
            module="GatherSampleEvidence",
            workflow_id=None,
            container_registry_map_uri="s3://bkt/reg-map.json",
            parameter_template_uri="s3://bkt/tpl.json",
        ),
    )

    registry_path = tmp_path / "workflow-versions.json"
    persist_workflow_version(record, registry_path)
    loaded = load_workflow_versions(registry_path)

    assert len(loaded) == 1
    assert loaded[0].workflow_id == "9690943"
    assert loaded[0].semver == "1.0.0"


def test_persist_replaces_same_module_version(tmp_path: Path) -> None:
    """Repeated registrations of the same ``(module, version_name)`` are idempotent."""
    client = RecordingOmicsClient(workflow_id="9690943")
    bundle = _make_bundle(tmp_path)
    # Both registrations go through create_workflow_version so version_name matches.
    first = register_module(
        client,
        bundle,
        _make_template(),
        _make_map(),
        semver="1.0.0",
        target=RegistrationTarget(
            module="GatherSampleEvidence",
            workflow_id="9690943",
            container_registry_map_uri="s3://bkt/reg-map.json",
            parameter_template_uri="s3://bkt/tpl.json",
        ),
    )
    second = register_module(
        client,
        bundle,
        _make_template(),
        _make_map(),
        semver="1.0.0",
        target=RegistrationTarget(
            module="GatherSampleEvidence",
            workflow_id="9690943",
            container_registry_map_uri="s3://bkt/reg-map.json",
            parameter_template_uri="s3://bkt/tpl.json",
        ),
    )

    registry_path = tmp_path / "workflow-versions.json"
    persist_workflow_version(first, registry_path)
    persist_workflow_version(second, registry_path)

    loaded = load_workflow_versions(registry_path)
    assert len(loaded) == 1
    assert loaded[0].version_name == "1.0.0"


def test_find_existing_workflow_id_prefers_latest(tmp_path: Path) -> None:
    records = [
        WorkflowVersionRecord(
            module="GatherSampleEvidence",
            workflow_id="9690943",
            version_name="1.0.0",
            semver="1.0.0",
            upstream_commit="7eb2af1feea9",
            divergences=[],
            container_registry_map_uri="s3://bkt/reg-map.json",
            parameter_template_uri="s3://bkt/tpl.json",
        ),
        WorkflowVersionRecord(
            module="AnnotateVcf",
            workflow_id="8239108",
            version_name="1.0.0",
            semver="1.0.0",
            upstream_commit="7eb2af1feea9",
            divergences=[],
            container_registry_map_uri="s3://bkt/reg-map.json",
            parameter_template_uri="s3://bkt/tpl.json",
        ),
    ]
    assert find_existing_workflow_id(records, "GatherSampleEvidence") == "9690943"
    assert find_existing_workflow_id(records, "AnnotateVcf") == "8239108"
    assert find_existing_workflow_id(records, "ClusterBatch") is None
