"""Shared Pydantic models for the GATK-SV HealthOmics Migration_System.

These models are the Python translation of the JSON schemas defined in
``.kiro/specs/gatk-sv-healthomics-migration/design.md`` under
*§Data Models* (Sample Manifest, Container Registry Map, Parameter
Template, Run Cache Reference, Cost Report, Divergence Log Entry, Workflow
Version Record) and the component API sketches in *§Components and
interfaces a–j*.

They back the ten sub-packages of :mod:`gatk_sv_aws`
(packager, registry, template, reference, iam, registrar, orchestrator,
cost, monitoring, validation).

Conventions (mirroring :mod:`gatk_sv_aws.models.catalog`):

* All models use ``model_config = ConfigDict(extra="forbid")``.
* ``Literal[...]`` is used for constrained string unions.
* ``StrEnum`` is used where a named enumeration makes runtime iteration
  ergonomic (see :class:`CostDimension`, :class:`ChangeKind`).
* Fields that mirror external JSON schemas produced by the AWS HealthOmics
  / ECR APIs (``registryMappings``, ``imageMappings``, ``upstreamRegistryUrl``,
  etc.) are kept in camelCase and flagged with ``# noqa: N815``.

The intent is that these models are *the* authoritative shape of every
artifact that moves between the components — a ``PackagedBundle`` handed
from the Packager (§Components.a) to the Registrar (§Components.f), a
``ContainerRegistryMap`` handed from the Registry Builder (§Components.b)
to ``CreateAHOWorkflow``, a ``CohortRunRecord`` returned by the Orchestrator
(§Components.g), and so on.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations and constants
# ---------------------------------------------------------------------------


# The ten migrated GATK-SV modules, in workflow submission order
# (Req 2a.1; Design §Workflow Module Mapping). Declared as a ``Literal`` so
# every field that references a module name gets compile-time checking from
# type checkers and runtime validation from Pydantic.
ModuleName = Literal[
    "GatherSampleEvidence",
    "GatherBatchEvidence",
    "ClusterBatch",
    "GenerateBatchMetrics",
    "FilterBatch",
    "MergeBatchSites",
    "GenotypeBatch",
    "RegenotypeCNVs",
    "MakeCohortVcf",
    "AnnotateVcf",
]

# Runtime-iterable counterpart to :data:`ModuleName`. The Orchestrator
# submits modules in this order (Design §Run Orchestrator, §Deployment Step 11).
MIGRATED_MODULES: tuple[ModuleName, ...] = (
    "GatherSampleEvidence",
    "GatherBatchEvidence",
    "ClusterBatch",
    "GenerateBatchMetrics",
    "FilterBatch",
    "MergeBatchSites",
    "GenotypeBatch",
    "RegenotypeCNVs",
    "MakeCohortVcf",
    "AnnotateVcf",
)


# WDL-level types exposed through the Parameter_Template (Design §Data Models
# → Parameter Template; Req 4.2). Represents the subset HealthOmics accepts
# for the migrated modules; any WDL input with a type outside this set is
# rejected by the Parameter Template Generator (§Components.c).
ParameterType = Literal[
    "File",
    "String",
    "Int",
    "Float",
    "Boolean",
    "Array[File]",
    "Array[String]",
    "Array[Int]",
]


# Reference genome builds supported by the Migration_System (Req 5.5, 5.6).
ReferenceBuild = Literal["GRCh38", "GRCh37"]


# Sample sex assignment (Design §Data Models → Sample Manifest; Req 6.3).
# "U" is for "unknown / unreported".
Sex = Literal["M", "F", "U"]


# HealthOmics storage modes (Req 8.1, 8.2).
StorageType = Literal["DYNAMIC", "STATIC"]


# HealthOmics networking modes (Req 11.2, 11.3).
NetworkingMode = Literal["RESTRICTED", "VPC"]


# HealthOmics run-cache behaviors (Req 10.2, 10.3).
CacheBehavior = Literal["CACHE_ALWAYS", "CACHE_ON_FAILURE"]


# High-level cohort run lifecycle states surfaced to operators.
RunStatus = Literal["RUNNING", "COMPLETED", "FAILED"]


class ChangeKind(StrEnum):
    """Kinds of edit the WDL Packager can apply to upstream GATK_SV sources.

    Mirrors the enumeration in Design §Data Models → Divergence Log Entry
    (Req 2a.4, 16.3, 17.2).
    """

    REMOVE_TASK = "remove_task"
    REWRITE_CONSTRUCT = "rewrite_construct"
    SWAP_CONTAINER = "swap_container"
    REMOVE_CALLER = "remove_caller"


class CostDimension(StrEnum):
    """Cost attribution dimensions surfaced by the Cost_Optimizer.

    Mirrors Design §Cost Model → Cost attribution (Req 8.6, 13.5).
    """

    COMPUTE = "compute"
    STORAGE = "storage"
    DATA_TRANSFER = "data-transfer"
    CONTAINER_PULLS = "container-pulls"


# ---------------------------------------------------------------------------
# Divergence & packaging (Design §Components.a, §Data Models → Divergence Log)
# ---------------------------------------------------------------------------


class DivergenceEntry(BaseModel):
    """A single edit the Packager applied to the upstream GATK_SV sources.

    One entry is recorded per MELT-referencing task removed, per HealthOmics-
    incompatible construct rewritten, per container reference swapped, and
    per caller removed (Req 2a.4, Req 17.2).
    """

    model_config = ConfigDict(extra="forbid")

    module: ModuleName = Field(
        ...,
        description="Migrated module whose WDL sources were edited.",
    )
    upstream_path: str = Field(
        ...,
        min_length=1,
        description="Repo-relative path of the upstream WDL file that was edited.",
    )
    change_kind: ChangeKind = Field(
        ...,
        description="Kind of edit applied. See :class:`ChangeKind`.",
    )
    reason: str = Field(
        ...,
        min_length=1,
        description="Human-readable justification for the edit, cited by the README divergence log (Req 17.2).",
    )
    upstream_commit: str = Field(
        ...,
        min_length=1,
        description="Pinned upstream GATK_SV commit SHA at which the edit was applied (Req 16.3).",
    )


class LintReport(BaseModel):
    """Result of running ``LintAHOWorkflowBundle`` against a packaged bundle.

    The Packager gates on ``status == 'success'`` with zero errors before the
    Registrar is allowed to call ``CreateAHOWorkflow`` (Req 2.3; Design
    §Components.a, §Deployment Step 6).
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "error"] = Field(
        ...,
        description="Overall lint status returned by ``LintAHOWorkflowBundle``.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Lint errors. Non-empty implies the bundle cannot be registered.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Lint warnings. Do not block registration but are surfaced to operators.",
    )
    raw_output: str = Field(
        default="",
        description="Raw linter output for operator triage; preserved verbatim.",
    )


class PackagedBundle(BaseModel):
    """A single module's WDL ZIP produced by the Packager (§Components.a).

    Holds enough metadata for the Registrar (§Components.f) to invoke
    ``CreateAHOWorkflow`` / ``CreateAHOWorkflowVersion`` and to record the
    :class:`WorkflowVersionRecord` with the upstream commit and divergence
    list (Req 16.3).
    """

    model_config = ConfigDict(extra="forbid")

    zip_path: Path = Field(
        ...,
        description="Local path to the packaged ``<module>-bundle.zip``.",
    )
    main_wdl_path: str = Field(
        ...,
        min_length=1,
        description="Path, relative to the ZIP root, of the module's main WDL file.",
    )
    module: ModuleName = Field(
        ...,
        description="Migrated module this bundle implements.",
    )
    upstream_commit: str = Field(
        ...,
        min_length=1,
        description="Pinned upstream GATK_SV commit SHA from which the bundle was derived.",
    )
    divergence: list[DivergenceEntry] = Field(
        default_factory=list,
        description="Every edit applied to the upstream sources to produce this bundle.",
    )
    lint_report: LintReport | None = Field(
        default=None,
        description="Populated by the Packager after calling ``LintAHOWorkflowBundle``.",
    )


# ---------------------------------------------------------------------------
# Container registry (Design §Components.b, §Data Models → Container Registry Map)
# ---------------------------------------------------------------------------


class RegistryMapping(BaseModel):
    """One ``registryMappings`` entry in a HealthOmics Container Registry Map.

    Field names match the AWS HealthOmics / ECR API schema verbatim and are
    therefore camelCase (Design §Data Models → Container Registry Map).
    """

    model_config = ConfigDict(extra="forbid")

    upstreamRegistryUrl: str = Field(  # noqa: N815
        ...,
        min_length=1,
        description="Upstream registry URL (e.g. ``quay.io``, ``registry-1.docker.io``).",
    )
    ecrRepositoryPrefix: str = Field(  # noqa: N815
        ...,
        min_length=1,
        description="Private ECR repository prefix that fronts the upstream registry.",
    )
    upstreamRepositoryPrefix: str | None = Field(  # noqa: N815
        default=None,
        description="Optional upstream repository prefix (e.g. ``biocontainers``).",
    )
    ecrAccountId: str | None = Field(  # noqa: N815
        default=None,
        description="Optional AWS account ID hosting the ECR repositories.",
    )


class ImageMapping(BaseModel):
    """One ``imageMappings`` entry in a HealthOmics Container Registry Map.

    Used for explicit per-image redirects (e.g., an image that cannot be
    resolved through a Pull_Through_Cache and was cloned into a private ECR
    repo — Req 3.4).
    """

    model_config = ConfigDict(extra="forbid")

    sourceImage: str = Field(  # noqa: N815
        ...,
        min_length=1,
        description="Upstream image reference as it appears in the WDL ``runtime.docker`` block.",
    )
    destinationImage: str = Field(  # noqa: N815
        ...,
        min_length=1,
        description="Private ECR image reference used at run time.",
    )


class ContainerRegistryMap(BaseModel):
    """The JSON document passed to ``CreateAHOWorkflow`` as ``container_registry_map``.

    Emitted by the Container Registry Map Builder (§Components.b) and verified
    to have no floating tags (Property 4, Req 3.5) and to be closed under the
    union of ``runtime.docker`` references in the packaged bundles
    (Property 5, Req 3.1, 3.3, 3.4).
    """

    model_config = ConfigDict(extra="forbid")

    registryMappings: list[RegistryMapping] = Field(  # noqa: N815
        default_factory=list,
        description="Registry-level redirects that cover whole upstream registries.",
    )
    imageMappings: list[ImageMapping] = Field(  # noqa: N815
        default_factory=list,
        description="Image-level redirects for references a registry mapping does not cover.",
    )


# ---------------------------------------------------------------------------
# Parameter templates (Design §Components.c, §Data Models → Parameter Template)
# ---------------------------------------------------------------------------


class ParameterTemplateEntry(BaseModel):
    """A single entry in a Parameter_Template.

    The HealthOmics parameter template JSON is a flat object keyed by input
    name; each value has ``description``, ``optional``, ``type``. This class
    models the value.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        ...,
        min_length=1,
        description="Human-readable description of the input (Req 4.2).",
    )
    optional: bool = Field(
        ...,
        description="True iff the corresponding WDL input is declared optional (Req 4.2, 4.5).",
    )
    type: ParameterType = Field(
        ...,
        description="WDL type of the input. Constrained to the subset in :data:`ParameterType`.",
    )


class ParameterTemplate(BaseModel):
    """A full Parameter_Template as consumed by ``CreateAHOWorkflow``.

    The HealthOmics-native on-disk shape is a flat JSON object keyed by
    input name (see Design §Data Models → Parameter Template). For Python
    ergonomics, :class:`ParameterTemplate` wraps that map in an ``entries``
    attribute and provides :meth:`to_json_dict` / :meth:`from_json_dict`
    helpers for the flat shape (used for file IO and MCP calls).
    """

    model_config = ConfigDict(extra="forbid")

    entries: dict[str, ParameterTemplateEntry] = Field(
        default_factory=dict,
        description=(
            "Mapping from workflow input name to its template entry. "
            "Keys MUST match a declared input name in the WDL workflow; "
            "this is enforced by the Parameter Template Validator (Req 18.2, 18.4, 18.5)."
        ),
    )

    def to_json_dict(self) -> dict[str, dict[str, Any]]:
        """Serialize to the flat HealthOmics parameter-template JSON shape."""
        return {name: entry.model_dump() for name, entry in self.entries.items()}

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> ParameterTemplate:
        """Build from the flat HealthOmics parameter-template JSON shape."""
        return cls(
            entries={
                name: ParameterTemplateEntry.model_validate(value)
                for name, value in data.items()
            }
        )


# ---------------------------------------------------------------------------
# Sample manifest (Design §Components.g, §Data Models → Sample Manifest)
# ---------------------------------------------------------------------------


class SampleRecord(BaseModel):
    """One sample in a Sample_Manifest (Req 6.3)."""

    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(
        ...,
        min_length=1,
        description="Cohort-unique sample identifier (Req 6.6).",
    )
    reads_uri: str = Field(
        ...,
        min_length=1,
        description="S3 URI of the CRAM or BAM (Req 6.1, 6.2).",
    )
    index_uri: str = Field(
        ...,
        min_length=1,
        description="S3 URI of the companion CRAI or BAI index (Req 6.5).",
    )
    sex: Sex = Field(
        ...,
        description="Sex assignment: ``M``, ``F``, or ``U`` (unknown).",
    )


class SampleManifest(BaseModel):
    """A cohort's sample manifest (Design §Data Models → Sample Manifest).

    Pydantic validation here covers only structural rules (non-empty samples,
    per-sample required fields). Semantic validation (URI schemes, region,
    index-presence, uniqueness) is done by the Run Orchestrator's
    ``validate_manifest`` (§Components.g; Req 6.5, 6.6, 11.1).
    """

    model_config = ConfigDict(extra="forbid")

    cohort_id: str = Field(
        ...,
        min_length=1,
        description="Cohort identifier used for Cost Explorer tagging (Req 8.7, 14.1).",
    )
    reference_build: ReferenceBuild = Field(
        ...,
        description="Reference genome build. GRCh38 is the default (Req 5.5, 5.6).",
    )
    samples: list[SampleRecord] = Field(
        ...,
        description="One entry per sample in the cohort.",
    )

    @model_validator(mode="after")
    def _require_samples(self) -> Self:
        if not self.samples:
            raise ValueError("SampleManifest.samples must contain at least one sample")
        return self


# ---------------------------------------------------------------------------
# IAM role (Design §Components.e, §IAM & Security)
# ---------------------------------------------------------------------------


class RoleScope(BaseModel):
    """Declared cohort scope passed to the IAM Role Synthesizer.

    Every prefix field is a literal prefix, never a wildcard; the role
    synthesizer expands it into ARNs. Any prefix ending in ``*`` is rejected
    here because the broadness check (Property 7, Req 12.6) treats such
    inputs as already-widened scopes.
    """

    model_config = ConfigDict(extra="forbid")

    region: str = Field(
        ...,
        description="Target_Region for the deployment (Req 1.3, 12.1). Follows standard AWS region format.",
    )
    reference_bucket: str = Field(
        ...,
        min_length=1,
        description="S3 bucket holding the Reference_Bundle (Req 12.2).",
    )
    reference_prefix: str = Field(
        ...,
        min_length=1,
        description="S3 prefix under ``reference_bucket`` that contains the Reference_Bundle (Req 12.2).",
    )
    input_bucket: str = Field(
        ...,
        min_length=1,
        description="S3 bucket holding Sample_Input data (Req 12.2).",
    )
    input_prefix: str = Field(
        ...,
        min_length=1,
        description="S3 prefix under ``input_bucket`` that contains Sample_Input data (Req 12.2).",
    )
    output_bucket: str = Field(
        ...,
        min_length=1,
        description="S3 bucket that receives run outputs (Req 12.3).",
    )
    output_prefix: str = Field(
        ...,
        min_length=1,
        description="S3 prefix under ``output_bucket`` that receives run outputs (Req 12.3).",
    )
    wdl_zip_bucket: str = Field(
        ...,
        min_length=1,
        description="S3 bucket that hosts the workflow definition ZIPs (Req 12.2).",
    )
    wdl_zip_prefix: str = Field(
        ...,
        min_length=1,
        description="S3 prefix under ``wdl_zip_bucket`` that hosts the workflow definition ZIPs (Req 12.2).",
    )
    ecr_account_id: str = Field(
        ...,
        min_length=1,
        description="AWS account ID that owns the ECR repositories in the Container_Registry_Map.",
    )
    ecr_repositories: list[str] = Field(
        ...,
        description=(
            "ECR repository names (not ARNs) that HealthOmics may pull from. "
            "The synthesizer expands these into per-repo ARNs (Req 12.4)."
        ),
    )
    log_group_prefix: str = Field(
        default="/aws/omics/",
        description="CloudWatch Logs log-group prefix that HealthOmics may write to (Req 12.5).",
    )

    @field_validator(
        "reference_prefix",
        "input_prefix",
        "output_prefix",
        "wdl_zip_prefix",
        "log_group_prefix",
    )
    @classmethod
    def _reject_trailing_wildcard(cls, value: str) -> str:
        if value.endswith("*"):
            raise ValueError(
                f"RoleScope prefixes must not end with '*' (got {value!r}); "
                "wildcards are introduced by the synthesizer, not the scope."
            )
        return value


class BroadnessViolation(BaseModel):
    """One statement in a candidate IAM policy that exceeds the declared scope.

    Emitted by the broadness checker (Design §IAM & Security; Req 12.6).
    """

    model_config = ConfigDict(extra="forbid")

    statement_sid: str = Field(
        ...,
        min_length=1,
        description="``Sid`` of the offending policy statement.",
    )
    resource: str = Field(
        ...,
        min_length=1,
        description="Resource ARN (or wildcard) that triggered the rejection.",
    )
    declared_scope: str = Field(
        ...,
        min_length=1,
        description="The declared-scope resource against which the offending resource was compared.",
    )
    reason: str = Field(
        ...,
        min_length=1,
        description="Human-readable reason, e.g. ``'wildcard Resource'`` or ``'strict prefix of declared'``.",
    )


class RolePolicies(BaseModel):
    """The synthesized output of the IAM Role Synthesizer (§Components.e).

    ``permissions_policy`` and ``trust_policy`` are full IAM policy documents
    in canonical JSON form. ``broadness_violations`` is empty iff the
    broadness check passed; a non-empty list blocks deployment (Req 12.6).
    """

    model_config = ConfigDict(extra="forbid")

    permissions_policy: dict[str, Any] = Field(
        ...,
        description="IAM permissions policy document (``Version`` + ``Statement`` array).",
    )
    trust_policy: dict[str, Any] = Field(
        ...,
        description="IAM trust policy document permitting ``omics.amazonaws.com`` to assume the role.",
    )
    broadness_violations: list[BroadnessViolation] = Field(
        default_factory=list,
        description="Every statement that failed the broadness check (Req 12.6).",
    )


# ---------------------------------------------------------------------------
# Cohort run records (Design §Components.g)
# ---------------------------------------------------------------------------


class ModuleRun(BaseModel):
    """Runtime record for one HealthOmics run within a cohort submission.

    The Orchestrator produces one :class:`ModuleRun` per module in
    submission order (GatherSampleEvidence → AnnotateVcf).
    """

    model_config = ConfigDict(extra="forbid")

    module: ModuleName = Field(
        ...,
        description="Migrated module this run executes.",
    )
    run_id: str = Field(
        ...,
        min_length=1,
        description="HealthOmics run identifier returned by ``StartAHORun``.",
    )
    status: str = Field(
        ...,
        min_length=1,
        description=(
            "HealthOmics run status string (e.g. ``PENDING``, ``RUNNING``, "
            "``COMPLETED``, ``FAILED``). Kept as a free-form string so new "
            "HealthOmics states surface without a code change."
        ),
    )
    started_at: datetime = Field(
        ...,
        description="Wall-clock time the run entered ``RUNNING`` (Req 14.1).",
    )
    finished_at: datetime | None = Field(
        default=None,
        description="Wall-clock time the run reached ``COMPLETED`` or ``FAILED`` (Req 14.2).",
    )


class CohortRunRecord(BaseModel):
    """Top-level record returned by the Run Orchestrator for a cohort submission.

    Captures every field needed to correlate a cohort's module chain with
    its HealthOmics runs, its cost-explorer tags, and its status (Req 6.4,
    7.1, 10.1, 11.2, 14.1, 14.2, 16.4).
    """

    model_config = ConfigDict(extra="forbid")

    cohort_id: str = Field(
        ...,
        min_length=1,
        description="Cohort identifier; propagated as the ``gatk-sv:cohort-id`` tag (Req 8.7).",
    )
    sample_count: int = Field(
        ...,
        ge=1,
        description="Number of samples in the cohort (Req 8.7, 13.4).",
    )
    workflow_versions: dict[ModuleName, str] = Field(
        ...,
        description="Per-module semantic workflow version string used for this submission (Req 16.4).",
    )
    output_uri: str = Field(
        ...,
        min_length=1,
        description="Caller-supplied S3 output prefix (Req 7.1).",
    )
    storage_type: StorageType = Field(
        ...,
        description="HealthOmics storage mode chosen for the runs (Req 8.1, 8.2).",
    )
    storage_capacity_gib: int | None = Field(
        default=None,
        ge=1,
        description=(
            "HealthOmics STATIC storage capacity in GiB. Required when "
            "``storage_type == 'STATIC'`` (Req 8.2)."
        ),
    )
    networking_mode: NetworkingMode = Field(
        ...,
        description="HealthOmics networking mode (Req 11.2, 11.3).",
    )
    cache_behavior: CacheBehavior = Field(
        ...,
        description="HealthOmics run-cache behavior (Req 10.2, 10.3).",
    )
    cache_id: str = Field(
        ...,
        min_length=1,
        description="HealthOmics run-cache ID attached to every module run (Req 10.1, 10.4).",
    )
    module_runs: list[ModuleRun] = Field(
        default_factory=list,
        description="One entry per module, in submission order.",
    )
    status: RunStatus = Field(
        ...,
        description="Overall cohort lifecycle state (Req 14.2).",
    )
    started_at: datetime = Field(
        ...,
        description="Wall-clock time the first module run entered ``RUNNING``.",
    )
    finished_at: datetime | None = Field(
        default=None,
        description="Wall-clock time the cohort reached its terminal state.",
    )
    cost_usd: float | None = Field(
        default=None,
        ge=0,
        description="Measured total cost in USD, populated by the Cost_Optimizer (Req 8.5, 14.2).",
    )

    @model_validator(mode="after")
    def _static_capacity_required(self) -> Self:
        if self.storage_type == "STATIC" and self.storage_capacity_gib is None:
            raise ValueError(
                "CohortRunRecord.storage_capacity_gib is required when storage_type == 'STATIC'"
            )
        return self


# ---------------------------------------------------------------------------
# Cost reporting (Design §Components.h, §Data Models → Cost Report, §Cost Model)
# ---------------------------------------------------------------------------


class RunCostEntry(BaseModel):
    """Per-run cost line in a :class:`CostReport`."""

    model_config = ConfigDict(extra="forbid")

    module: ModuleName = Field(
        ...,
        description="Migrated module that produced the run.",
    )
    run_id: str = Field(
        ...,
        min_length=1,
        description="HealthOmics run identifier the cost is attributed to.",
    )
    cost_usd: float = Field(
        ...,
        ge=0,
        description="Measured cost in USD for this run (Req 8.5).",
    )
    wall_clock_sec: int = Field(
        ...,
        ge=0,
        description="Run wall-clock duration in seconds (Req 14.2).",
    )
    tags: dict[str, str] = Field(
        default_factory=dict,
        description="Cost Explorer tags applied to the run (Req 8.7).",
    )


class CostAttribution(BaseModel):
    """One ``(module, dimension)`` cost attribution row.

    Produced by the Cost_Optimizer when surfacing overages (Req 8.6, 13.5).
    """

    model_config = ConfigDict(extra="forbid")

    module: ModuleName = Field(
        ...,
        description="Migrated module responsible for this cost share.",
    )
    dimension: CostDimension = Field(
        ...,
        description="Cost dimension. See :class:`CostDimension`.",
    )
    cost_usd: float = Field(
        ...,
        ge=0,
        description="Dollar cost attributed to ``(module, dimension)`` for the cohort.",
    )


class CostReport(BaseModel):
    """Top-level cost report emitted by the Cost_Optimizer (§Components.h).

    Shape matches Design §Data Models → Cost Report and §Cost Model. Fields
    are computed against the Cost Explorer tag set defined in Property 10
    (Req 8.5, 8.6, 8.7, 13.4, 13.5).
    """

    model_config = ConfigDict(extra="forbid")

    cohort_id: str = Field(
        ...,
        min_length=1,
        description="Cohort identifier the report aggregates (Req 8.7).",
    )
    sample_count: int = Field(
        ...,
        ge=1,
        description="Sample count used as the denominator of ``per_sample_cost_usd`` (Req 13.4).",
    )
    runs: list[RunCostEntry] = Field(
        default_factory=list,
        description="Per-run cost lines.",
    )
    total_cost_usd: float = Field(
        ...,
        ge=0,
        description="Sum of ``runs[].cost_usd``.",
    )
    per_sample_cost_usd: float = Field(
        ...,
        ge=0,
        description="``total_cost_usd / sample_count`` (Req 8.7, 13.4).",
    )
    target_usd: float = Field(
        default=7.00,
        ge=0,
        description="Per_Sample_Cost_Target in USD; default is the 7.00 value set by Req 8.5.",
    )
    over_target: bool = Field(
        ...,
        description="True iff ``per_sample_cost_usd > target_usd`` (Req 8.6, 13.5).",
    )
    attribution: list[CostAttribution] = Field(
        default_factory=list,
        description="Per-(module, dimension) attribution rows (Req 8.6).",
    )


# ---------------------------------------------------------------------------
# Workflow versioning (Design §Components.f, §Data Models → Workflow Version Record)
# ---------------------------------------------------------------------------


class WorkflowVersionRecord(BaseModel):
    """Record persisted by the Registrar after ``CreateAHOWorkflow[Version]``.

    Captures the traceability chain from a HealthOmics workflow version back
    to its upstream GATK_SV commit and the divergence list that produced it
    (Req 16.1, 16.2, 16.3).
    """

    model_config = ConfigDict(extra="forbid")

    module: ModuleName = Field(
        ...,
        description="Migrated module this version belongs to.",
    )
    workflow_id: str = Field(
        ...,
        min_length=1,
        description="HealthOmics workflow ID returned by ``CreateAHOWorkflow``.",
    )
    version_name: str = Field(
        ...,
        min_length=1,
        description="HealthOmics workflow version name (Req 16.2).",
    )
    semver: str = Field(
        ...,
        min_length=1,
        description="Semantic version string applied to the version (Req 16.2).",
    )
    upstream_commit: str = Field(
        ...,
        min_length=1,
        description="Pinned upstream GATK_SV commit SHA (Req 16.3).",
    )
    divergences: list[str] = Field(
        default_factory=list,
        description=(
            "Human-readable divergence summaries attached to this version. "
            "The corresponding machine-readable list lives alongside the "
            "bundle as :class:`DivergenceEntry` records (Req 16.3, 17.2)."
        ),
    )
    container_registry_map_uri: str = Field(
        ...,
        min_length=1,
        description="S3 URI of the :class:`ContainerRegistryMap` JSON attached to this version (Req 3.1).",
    )
    parameter_template_uri: str = Field(
        ...,
        min_length=1,
        description="S3 URI of the :class:`ParameterTemplate` JSON attached to this version (Req 4.1).",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    # Enumerations and constants
    "MIGRATED_MODULES",
    "ModuleName",
    "ParameterType",
    "ReferenceBuild",
    "Sex",
    "StorageType",
    "NetworkingMode",
    "CacheBehavior",
    "RunStatus",
    "ChangeKind",
    "CostDimension",
    # Divergence & packaging
    "DivergenceEntry",
    "LintReport",
    "PackagedBundle",
    # Container registry
    "RegistryMapping",
    "ImageMapping",
    "ContainerRegistryMap",
    # Parameter templates
    "ParameterTemplateEntry",
    "ParameterTemplate",
    # Sample manifest
    "SampleRecord",
    "SampleManifest",
    # IAM role
    "RoleScope",
    "BroadnessViolation",
    "RolePolicies",
    # Cohort run records
    "ModuleRun",
    "CohortRunRecord",
    # Cost reporting
    "RunCostEntry",
    "CostAttribution",
    "CostReport",
    # Workflow versioning
    "WorkflowVersionRecord",
]
