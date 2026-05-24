"""Component (g): Run Orchestrator for the GATK-SV migration.

Implements design §Components and interfaces → (g) Run Orchestrator.
Accepts a sample manifest and cohort identifier, validates the manifest,
runs a cross-region preflight, chooses DYNAMIC/STATIC storage and
RESTRICTED/VPC networking, attaches the Run_Cache, emits cost-tracking
tags, and invokes ``StartAHORun`` per module. Verifies declared outputs
after each run and handles retry classification.

Advances Requirements 6 (Sample Input Handling), 7 (Cohort VCF Output),
10 (Cost Optimization — Run Caching), 11 (Cost Optimization — Data
Locality), 14 (Monitoring, Diagnostics, and Observability), and 15 (Error
Handling and Retries).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from gatk_sv_aws.models import (
    MIGRATED_MODULES,
    CacheBehavior,
    CohortRunRecord,
    ContainerRegistryMap,
    ModuleName,
    ModuleRun,
    NetworkingMode,
    SampleManifest,
    StorageType,
    WorkflowVersionRecord,
)

# Target region defaults to ap-southeast-1 but can be overridden via
# environment variable AWS_DEFAULT_REGION or by passing region= to functions.
import os as _os
TARGET_REGION = _os.environ.get("GATK_SV_TARGET_REGION", "ap-southeast-1")

# One TiB in bytes — threshold between DYNAMIC and STATIC storage (Req 8.1, 8.2).
_ONE_TIB = 1024**4


# ---------------------------------------------------------------------------
# Sample manifest validation (Property 8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestIssue:
    """One validation issue against a :class:`SampleManifest`.

    ``rule`` is one of ``"duplicate_id"``, ``"missing_index"``,
    ``"out_of_region"``, ``"unsupported_format"``.
    """

    sample_id: str
    rule: str
    detail: str


def _classify_reads_format(reads_uri: str, index_uri: str) -> str | None:
    """Return a human-readable complaint when the reads/index pair is unsupported.

    Returns ``None`` when the pair is a supported CRAM+CRAI or BAM+BAI combo
    (Req 6.1, 6.2).
    """
    reads = reads_uri.lower()
    index = index_uri.lower()
    if reads.endswith(".cram") and index.endswith(".crai"):
        return None
    if reads.endswith(".bam") and index.endswith(".bai"):
        return None
    if reads.endswith(".cram") and not index.endswith(".crai"):
        return f"CRAM reads URI {reads_uri!r} has index URI {index_uri!r} (expected .crai)"
    if reads.endswith(".bam") and not index.endswith(".bai"):
        return f"BAM reads URI {reads_uri!r} has index URI {index_uri!r} (expected .bai)"
    return f"reads URI {reads_uri!r} is not a supported CRAM or BAM file"


def validate_manifest(
    manifest: SampleManifest,
    *,
    region_resolver: Callable[[str], str] | None = None,
    exists_resolver: Callable[[str], bool] | None = None,
) -> list[ManifestIssue]:
    """Validate a sample manifest against the rules of Property 8 (Req 6.1, 6.2, 6.5, 6.6, 11.1).

    ``region_resolver`` maps an S3 URI to its bucket region; when ``None``
    the region check is skipped. ``exists_resolver`` maps a URI to whether
    the object exists; when ``None`` the index-existence check is skipped.
    Both resolvers are injected so the orchestrator can supply boto3-backed
    implementations at run time while tests can supply oracles.

    Rules emitted:
        * ``duplicate_id`` — one issue per offending occurrence of a
          repeated ``sample_id``.
        * ``unsupported_format`` — reads URI is neither CRAM+CRAI nor
          BAM+BAI (Req 6.1, 6.2).
        * ``missing_index`` — index URI does not exist per
          ``exists_resolver``.
        * ``out_of_region`` — reads or index URI is outside Target_Region.
    """
    issues: list[ManifestIssue] = []

    # Rule: duplicate sample_id (Req 6.6). Emit one issue per offending
    # occurrence so both copies of a duplicated ID are reported.
    id_counts: dict[str, int] = {}
    for sample in manifest.samples:
        id_counts[sample.sample_id] = id_counts.get(sample.sample_id, 0) + 1
    for sample in manifest.samples:
        count = id_counts[sample.sample_id]
        if count > 1:
            issues.append(
                ManifestIssue(
                    sample_id=sample.sample_id,
                    rule="duplicate_id",
                    detail=f"sample_id appears {count} times",
                )
            )

    for sample in manifest.samples:
        # Rule: unsupported_format (Req 6.1, 6.2).
        format_complaint = _classify_reads_format(sample.reads_uri, sample.index_uri)
        if format_complaint is not None:
            issues.append(
                ManifestIssue(
                    sample_id=sample.sample_id,
                    rule="unsupported_format",
                    detail=format_complaint,
                )
            )

        # Rule: missing_index (Req 6.5).
        if exists_resolver is not None:
            if not exists_resolver(sample.index_uri):
                issues.append(
                    ManifestIssue(
                        sample_id=sample.sample_id,
                        rule="missing_index",
                        detail=f"index URI {sample.index_uri!r} does not exist",
                    )
                )

        # Rule: out_of_region (Req 11.1).
        if region_resolver is not None:
            for uri_kind, uri in (("reads", sample.reads_uri), ("index", sample.index_uri)):
                region = region_resolver(uri)
                if region != TARGET_REGION:
                    issues.append(
                        ManifestIssue(
                            sample_id=sample.sample_id,
                            rule="out_of_region",
                            detail=f"{uri_kind} URI resolved to {region}",
                        )
                    )

    return issues


# ---------------------------------------------------------------------------
# Cross-region preflight (Property 6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightOffender:
    """One artifact the preflight found outside Target_Region."""

    uri: str
    kind: str  # "s3" | "ecr"
    observed_region: str


@dataclass(frozen=True)
class PreflightReport:
    """Result of :func:`cross_region_preflight`.

    ``accepted`` is True iff every artifact resolved to Target_Region
    (``ap-southeast-1``). When False, ``offenders`` names every offending
    artifact and its observed region.
    """

    accepted: bool
    offenders: tuple[PreflightOffender, ...] = ()


def cross_region_preflight(
    manifest: SampleManifest,
    reference_prefix: str,
    registry_map: ContainerRegistryMap,
    *,
    s3_region_resolver: Callable[[str], str] | None = None,
    ecr_region_resolver: Callable[[str], str] | None = None,
) -> PreflightReport:
    """Reject cohort submissions with any artifact outside Target_Region.

    Iterates every S3 URI across the manifest (per-sample reads/index URIs
    plus the Reference_Bundle prefix) and every ECR URI across
    ``registry_map.imageMappings`` (``sourceImage`` / ``destinationImage``).
    When a resolver is not supplied, artifacts of that kind are assumed to
    resolve to Target_Region.
    """

    def _resolve_s3(uri: str) -> str:
        if s3_region_resolver is None:
            return TARGET_REGION
        return s3_region_resolver(uri)

    def _resolve_ecr(uri: str) -> str:
        if ecr_region_resolver is None:
            return TARGET_REGION
        return ecr_region_resolver(uri)

    offenders: list[PreflightOffender] = []
    seen: set[tuple[str, str]] = set()

    def _check(uri: str, kind: str, observed_region: str) -> None:
        key = (kind, uri)
        if key in seen:
            return
        seen.add(key)
        if observed_region != TARGET_REGION:
            offenders.append(
                PreflightOffender(uri=uri, kind=kind, observed_region=observed_region)
            )

    # S3 artifacts: per-sample reads/index, plus Reference_Bundle prefix.
    for sample in manifest.samples:
        _check(sample.reads_uri, "s3", _resolve_s3(sample.reads_uri))
        _check(sample.index_uri, "s3", _resolve_s3(sample.index_uri))
    _check(reference_prefix, "s3", _resolve_s3(reference_prefix))

    # ECR artifacts: every imageMappings source and destination.
    for mapping in registry_map.imageMappings:
        _check(mapping.sourceImage, "ecr", _resolve_ecr(mapping.sourceImage))
        _check(mapping.destinationImage, "ecr", _resolve_ecr(mapping.destinationImage))

    return PreflightReport(accepted=not offenders, offenders=tuple(offenders))


# ---------------------------------------------------------------------------
# Storage sizing (implementation-layer property, Task 2.11 — stubbed early)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageChoice:
    """Storage decision returned by :func:`choose_storage`."""

    storage_type: StorageType
    storage_capacity_gib: int | None


def choose_storage(total_input_bytes: int, peak_working_set_gib: float) -> StorageChoice:
    """Pick DYNAMIC vs STATIC storage for a cohort run (Req 8.1, 8.2, 8.3).

    DYNAMIC is returned when total input bytes is at or below 1 TiB.
    Otherwise STATIC is returned with a capacity computed as
    ``max(1200, ceil(peak * 1.20 / 1200) * 1200)`` GiB — HealthOmics
    allocates STATIC storage in 1200 GiB chunks.
    """
    if total_input_bytes <= _ONE_TIB:
        return StorageChoice(storage_type="DYNAMIC", storage_capacity_gib=None)
    required_gib = math.ceil(peak_working_set_gib * 1.20 / 1200) * 1200
    capacity = max(1200, required_gib)
    return StorageChoice(storage_type="STATIC", storage_capacity_gib=capacity)


# ---------------------------------------------------------------------------
# Cost tagging (Property 10)
# ---------------------------------------------------------------------------


@dataclass
class TagRecorder:
    """In-memory sink recording every tag set applied by the orchestrator.

    Used by Property 10 to assert that every resource-creating call carries
    both ``gatk-sv:cohort-id`` and ``gatk-sv:workflow-version`` tags.
    """

    applied: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)

    def record(self, resource_kind: str, arn: str, tags: Mapping[str, str]) -> None:
        self.applied.append((resource_kind, arn, dict(tags)))


def apply_cost_tags(
    recorder: TagRecorder,
    resource_kind: str,
    arn: str,
    *,
    cohort_id: str,
    workflow_version: str,
    module: str | None = None,
    sample_count: int | None = None,
    environment: str = "prod",
) -> None:
    """Apply the Cost Explorer tag set to a run-associated resource (Req 8.7, 16.4).

    ``cohort-id``, ``workflow-version``, and ``environment`` are always
    present. ``module`` and ``sample-count`` are included when supplied.
    """
    tags: dict[str, str] = {
        "gatk-sv:cohort-id": cohort_id,
        "gatk-sv:workflow-version": workflow_version,
        "gatk-sv:environment": environment,
    }
    if module is not None:
        tags["gatk-sv:module"] = module
    if sample_count is not None:
        tags["gatk-sv:sample-count"] = str(sample_count)
    recorder.record(resource_kind, arn, tags)


# ---------------------------------------------------------------------------
# Output verification (Task 2.11 — implementation-layer property)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputVerification:
    """Result of :func:`verify_outputs`."""

    status: str  # "COMPLETED" | "FAILED"
    missing: tuple[str, ...] = ()


def verify_outputs(declared_outputs: list[str], present_outputs: list[str]) -> OutputVerification:
    """Return ``COMPLETED`` iff every declared output is present; else ``FAILED`` (Req 7.4, 7.5).

    ``missing`` preserves the declared order so operator-facing reports
    describe missing outputs in a stable way.
    """
    present_set = set(present_outputs)
    missing = tuple(name for name in declared_outputs if name not in present_set)
    if not missing:
        return OutputVerification(status="COMPLETED", missing=())
    return OutputVerification(status="FAILED", missing=missing)


# ---------------------------------------------------------------------------
# Retry classifier (Task 2.11 — implementation-layer property)
# ---------------------------------------------------------------------------


RETRYABLE_ERROR_CODES = frozenset(
    {"InternalServerError", "Throttling", "ServiceUnavailable", "OutOfMemoryError"}
)

# Retry backoff constants (Req 15.1, 15.3; design §Error Handling and Retries).
_BACKOFF_BASE_SECONDS = 30.0
_BACKOFF_FACTOR = 2
_BACKOFF_CAP_SECONDS = 8 * 60.0  # 8 minutes.
_MAX_RETRY_ATTEMPTS = 3


@dataclass(frozen=True)
class RetryDecision:
    """Output of :func:`classify_retry`."""

    should_retry: bool
    delay_seconds: float  # exponential backoff delay; 0 when not retrying


def classify_retry(error_code: str, attempt_number: int) -> RetryDecision:
    """Retry classifier + exponential backoff (base 30s, factor 2, cap 8m, max 3 retries).

    Retries only when ``error_code`` is in :data:`RETRYABLE_ERROR_CODES`
    and ``attempt_number`` is strictly less than
    :data:`_MAX_RETRY_ATTEMPTS`. For ``attempt_number ∈ {1, 2}`` the delay
    is ``min(base * factor ** (attempt - 1), cap)``.
    """
    if error_code not in RETRYABLE_ERROR_CODES or attempt_number >= _MAX_RETRY_ATTEMPTS:
        return RetryDecision(should_retry=False, delay_seconds=0.0)
    delay = min(
        _BACKOFF_BASE_SECONDS * (_BACKOFF_FACTOR ** (attempt_number - 1)),
        _BACKOFF_CAP_SECONDS,
    )
    return RetryDecision(should_retry=True, delay_seconds=delay)


__all__ = [
    "TARGET_REGION",
    "ManifestIssue",
    "validate_manifest",
    "PreflightOffender",
    "PreflightReport",
    "cross_region_preflight",
    "StorageChoice",
    "choose_storage",
    "TagRecorder",
    "apply_cost_tags",
    "OutputVerification",
    "verify_outputs",
    "RETRYABLE_ERROR_CODES",
    "RetryDecision",
    "classify_retry",
    "OmicsRunClient",
    "submit_cohort",
]


# ---------------------------------------------------------------------------
# Cohort submission (Task 3.7.4)
# ---------------------------------------------------------------------------


class OmicsRunClient(Protocol):
    """Minimal protocol matching ``boto3.client('omics')`` for run submission."""

    def start_run(self, **kwargs: Any) -> dict[str, Any]: ...


def submit_cohort(
    client: OmicsRunClient,
    manifest: SampleManifest,
    cohort_id: str,
    *,
    workflow_versions: dict[ModuleName, WorkflowVersionRecord],
    output_uri: str,
    role_arn: str,
    storage: StorageChoice,
    networking: NetworkingMode = "RESTRICTED",
    cache_behavior: CacheBehavior = "CACHE_ALWAYS",
    cache_id: str,
    recorder: TagRecorder,
    now: Callable[[], datetime] | None = None,
) -> CohortRunRecord:
    """Submit the 10-module chain to HealthOmics in module order.

    Implementation target of Task 3.7.4 (Req 6.4, 7.1, 10.1, 10.2, 11.2,
    14.1, 14.2, 16.4).

    For each module in :data:`MIGRATED_MODULES` (GatherSampleEvidence →
    AnnotateVcf), calls ``client.start_run`` with the module's workflow
    version, applies Property-10 cost tags, and collects the returned
    identifiers into a :class:`~.models.CohortRunRecord`.

    * ``storage`` — result of :func:`choose_storage`.
    * ``networking`` / ``cache_behavior`` — default to RESTRICTED and
      CACHE_ALWAYS respectively (Req 11.2, 10.2).
    * ``recorder`` — :class:`TagRecorder` collecting every tag set applied;
      Property 10 asserts this covers every resource-creating call.
    * ``now`` — clock injection for testing.

    The returned :class:`CohortRunRecord` carries ``status="RUNNING"`` at
    submission time; callers update it as runs reach COMPLETED or FAILED
    by invoking :func:`verify_outputs` and writing the final state via
    the Monitoring component (Design §Components.i).
    """
    _now = now or (lambda: datetime.now(tz=timezone.utc))

    missing = [m for m in MIGRATED_MODULES if m not in workflow_versions]
    if missing:
        raise ValueError(
            f"missing workflow versions for: {', '.join(missing)}. "
            "submit_cohort requires a workflow version for every migrated module."
        )

    start = _now()
    module_runs: list[ModuleRun] = []
    for module in MIGRATED_MODULES:
        version = workflow_versions[module]
        start_kwargs: dict[str, Any] = {
            "workflowId": version.workflow_id,
            "workflowType": "PRIVATE",
            "roleArn": role_arn,
            "name": f"{cohort_id}-{module}",
            "outputUri": f"{output_uri.rstrip('/')}/{module}",
            "parameters": {"cohort_id": cohort_id},
            "storageType": storage.storage_type,
            "cacheId": cache_id,
            "cacheBehavior": cache_behavior,
            "tags": {
                "gatk-sv:cohort-id": cohort_id,
                "gatk-sv:workflow-version": version.semver,
                "gatk-sv:module": module,
                "gatk-sv:sample-count": str(len(manifest.samples)),
                "gatk-sv:environment": "prod",
            },
        }
        if version.version_name:
            start_kwargs["workflowVersionName"] = version.version_name
        if storage.storage_type == "STATIC" and storage.storage_capacity_gib:
            start_kwargs["storageCapacity"] = storage.storage_capacity_gib
        start_kwargs["networking"] = {"mode": networking}

        response = client.start_run(**start_kwargs)
        run_id = str(response["id"])
        run_arn = response.get("arn", f"arn:aws:omics:{TARGET_REGION}::run/{run_id}")

        # Property 10: apply cost tags to every resource-creating call.
        apply_cost_tags(
            recorder,
            "Run",
            run_arn,
            cohort_id=cohort_id,
            workflow_version=version.semver,
            module=module,
            sample_count=len(manifest.samples),
        )

        module_runs.append(
            ModuleRun(
                module=module,
                run_id=run_id,
                status="PENDING",
                started_at=_now(),
                finished_at=None,
            )
        )

    return CohortRunRecord(
        cohort_id=cohort_id,
        sample_count=len(manifest.samples),
        workflow_versions={m: v.semver for m, v in workflow_versions.items()},
        output_uri=output_uri,
        storage_type=storage.storage_type,
        storage_capacity_gib=storage.storage_capacity_gib,
        networking_mode=networking,
        cache_behavior=cache_behavior,
        cache_id=cache_id,
        module_runs=module_runs,
        status="RUNNING",
        started_at=start,
        finished_at=None,
    )
