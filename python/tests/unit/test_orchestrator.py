"""Unit tests for the GATK-SV Run Orchestrator component (Design §Components.g).

These example-based tests complement the Hypothesis property tests in
``tests/gatk_sv_aws/properties/`` by covering specific boundary
cases and error shapes called out in the requirements:

* Manifest validation happy path and rule-specific rejections
  (Reqs 6.1, 6.2, 6.5, 6.6, 11.1).
* STATIC sizing boundary around exactly 1 TiB (Req 8.1, 8.2).
* Retry classifier terminal states (Req 15.1, 15.2, 15.3).
* Output presence verifier (Req 7.4, 7.5).
* Cost-tag coverage on every recorded call (Req 8.7, 16.4).
"""

from __future__ import annotations

from gatk_sv_aws.models import (
    SampleManifest,
    SampleRecord,
)
from gatk_sv_aws.orchestrator import (
    TARGET_REGION,
    ManifestIssue,
    TagRecorder,
    apply_cost_tags,
    choose_storage,
    classify_retry,
    validate_manifest,
    verify_outputs,
)

_ONE_TIB = 1024**4


def _make_sample(
    sample_id: str = "S1",
    reads_uri: str = "s3://bucket/reads.cram",
    index_uri: str = "s3://bucket/reads.cram.crai",
    sex: str = "M",
) -> SampleRecord:
    return SampleRecord(
        sample_id=sample_id,
        reads_uri=reads_uri,
        index_uri=index_uri,
        sex=sex,
    )


def _make_manifest(*samples: SampleRecord, cohort_id: str = "cohort-a") -> SampleManifest:
    return SampleManifest(
        cohort_id=cohort_id,
        reference_build="GRCh38",
        samples=list(samples),
    )


# ---------------------------------------------------------------------------
# validate_manifest
# ---------------------------------------------------------------------------


def test_validate_manifest_happy_path_returns_no_issues() -> None:
    manifest = _make_manifest(
        _make_sample(sample_id="S1"),
        _make_sample(
            sample_id="S2",
            reads_uri="s3://bucket/sample2.bam",
            index_uri="s3://bucket/sample2.bam.bai",
        ),
    )

    issues = validate_manifest(
        manifest,
        region_resolver=lambda _uri: TARGET_REGION,
        exists_resolver=lambda _uri: True,
    )

    assert issues == []


def test_validate_manifest_detects_duplicate_sample_ids() -> None:
    # Two samples share the id "DUP" — every offending occurrence must be
    # reported so the operator sees both copies of the collision (Req 6.6).
    manifest = _make_manifest(
        _make_sample(sample_id="DUP", reads_uri="s3://b/a.cram", index_uri="s3://b/a.cram.crai"),
        _make_sample(sample_id="DUP", reads_uri="s3://b/b.cram", index_uri="s3://b/b.cram.crai"),
        _make_sample(sample_id="OK", reads_uri="s3://b/c.cram", index_uri="s3://b/c.cram.crai"),
    )

    issues = validate_manifest(manifest)

    duplicate_issues = [i for i in issues if i.rule == "duplicate_id"]
    assert len(duplicate_issues) == 2
    assert {i.sample_id for i in duplicate_issues} == {"DUP"}


def test_validate_manifest_detects_out_of_region_uri() -> None:
    manifest = _make_manifest(_make_sample())
    # The reads URI resolves outside Target_Region.
    region_map = {
        "s3://bucket/reads.cram": "us-east-1",
        "s3://bucket/reads.cram.crai": TARGET_REGION,
    }

    issues = validate_manifest(
        manifest,
        region_resolver=lambda uri: region_map.get(uri, "unknown"),
    )

    out_of_region = [i for i in issues if i.rule == "out_of_region"]
    assert len(out_of_region) == 1
    assert out_of_region[0].sample_id == "S1"
    assert "us-east-1" in out_of_region[0].detail


def test_validate_manifest_detects_unsupported_format() -> None:
    # A .sam reads file is not supported (Req 6.1, 6.2 only accept CRAM+CRAI
    # and BAM+BAI).
    manifest = _make_manifest(
        _make_sample(
            sample_id="S1",
            reads_uri="s3://bucket/reads.sam",
            index_uri="s3://bucket/reads.sam.bai",
        ),
    )

    issues = validate_manifest(manifest)

    unsupported = [i for i in issues if i.rule == "unsupported_format"]
    assert len(unsupported) == 1
    assert unsupported[0].sample_id == "S1"


def test_validate_manifest_detects_cram_gz_as_unsupported() -> None:
    # .cram.gz is not a supported reads format (the reads extension is
    # technically .gz — only .cram and .bam are accepted).
    manifest = _make_manifest(
        _make_sample(
            sample_id="S1",
            reads_uri="s3://bucket/reads.cram.gz",
            index_uri="s3://bucket/reads.cram.gz.crai",
        ),
    )

    issues = validate_manifest(manifest)

    assert any(i.rule == "unsupported_format" for i in issues)


def test_validate_manifest_detects_missing_index_via_exists_resolver() -> None:
    manifest = _make_manifest(
        _make_sample(sample_id="S1"),
        _make_sample(
            sample_id="S2",
            reads_uri="s3://bucket/s2.cram",
            index_uri="s3://bucket/s2.cram.crai",
        ),
    )
    exists = {
        "s3://bucket/reads.cram": True,
        "s3://bucket/reads.cram.crai": True,
        "s3://bucket/s2.cram": True,
        "s3://bucket/s2.cram.crai": False,  # Missing index for S2.
    }

    issues = validate_manifest(
        manifest,
        exists_resolver=lambda uri: exists[uri],
    )

    missing = [i for i in issues if i.rule == "missing_index"]
    assert len(missing) == 1
    assert missing[0].sample_id == "S2"


# ---------------------------------------------------------------------------
# choose_storage
# ---------------------------------------------------------------------------


def test_choose_storage_dynamic_at_or_below_one_tib() -> None:
    # Exactly 1 TiB is still DYNAMIC (Req 8.1).
    choice = choose_storage(_ONE_TIB, peak_working_set_gib=100.0)
    assert choice.storage_type == "DYNAMIC"
    assert choice.storage_capacity_gib is None


def test_choose_storage_static_above_one_tib() -> None:
    # One byte over 1 TiB flips to STATIC (Req 8.2).
    choice = choose_storage(_ONE_TIB + 1, peak_working_set_gib=100.0)
    assert choice.storage_type == "STATIC"
    # capacity = max(1200, ceil(100 * 1.20 / 1200) * 1200) = max(1200, 1200) = 1200
    assert choice.storage_capacity_gib == 1200


def test_choose_storage_rounds_up_to_1200_gib_multiple() -> None:
    # Peak 5000 GiB * 1.20 = 6000 GiB; ceil(6000 / 1200) * 1200 = 6000.
    choice = choose_storage(_ONE_TIB * 2, peak_working_set_gib=5000.0)
    assert choice.storage_type == "STATIC"
    assert choice.storage_capacity_gib == 6000

    # Peak 5001 GiB * 1.20 = 6001.2 GiB; ceil(6001.2 / 1200) = 6; 6 * 1200 = 7200.
    choice = choose_storage(_ONE_TIB * 2, peak_working_set_gib=5001.0)
    assert choice.storage_type == "STATIC"
    assert choice.storage_capacity_gib == 7200
    assert choice.storage_capacity_gib % 1200 == 0


# ---------------------------------------------------------------------------
# classify_retry
# ---------------------------------------------------------------------------


def test_classify_retry_retryable_first_attempt_uses_base_delay() -> None:
    decision = classify_retry("Throttling", attempt_number=1)
    assert decision.should_retry is True
    assert decision.delay_seconds == 30.0


def test_classify_retry_retryable_second_attempt_doubles_delay() -> None:
    decision = classify_retry("InternalServerError", attempt_number=2)
    assert decision.should_retry is True
    assert decision.delay_seconds == 60.0


def test_classify_retry_retryable_third_attempt_does_not_retry() -> None:
    decision = classify_retry("ServiceUnavailable", attempt_number=3)
    assert decision.should_retry is False
    assert decision.delay_seconds == 0.0


def test_classify_retry_non_retryable_code_never_retries() -> None:
    decision = classify_retry("AccessDenied", attempt_number=1)
    assert decision.should_retry is False
    assert decision.delay_seconds == 0.0


# ---------------------------------------------------------------------------
# verify_outputs
# ---------------------------------------------------------------------------


def test_verify_outputs_reports_completed_when_all_present() -> None:
    report = verify_outputs(
        declared_outputs=["cohort.vcf.gz", "cohort.vcf.gz.tbi"],
        present_outputs=["cohort.vcf.gz.tbi", "cohort.vcf.gz"],
    )
    assert report.status == "COMPLETED"
    assert report.missing == ()


def test_verify_outputs_reports_failed_with_missing_names() -> None:
    report = verify_outputs(
        declared_outputs=["cohort.vcf.gz", "cohort.vcf.gz.tbi", "metrics.tsv"],
        present_outputs=["cohort.vcf.gz"],
    )
    assert report.status == "FAILED"
    # Missing preserves declared order.
    assert report.missing == ("cohort.vcf.gz.tbi", "metrics.tsv")


# ---------------------------------------------------------------------------
# apply_cost_tags
# ---------------------------------------------------------------------------


def test_apply_cost_tags_records_cohort_and_workflow_version_on_every_call() -> None:
    recorder = TagRecorder()
    apply_cost_tags(
        recorder,
        "StartAHORun",
        "arn:aws:omics:ap-southeast-1:123456789012:run/abc",
        cohort_id="cohort-x",
        workflow_version="1.2.3",
    )
    apply_cost_tags(
        recorder,
        "S3PutObject",
        "arn:aws:s3:ap-southeast-1:123456789012:object/out",
        cohort_id="cohort-x",
        workflow_version="1.2.3",
        module="GenotypeBatch",
        sample_count=100,
    )

    assert len(recorder.applied) == 2
    for _kind, _arn, tags in recorder.applied:
        assert tags["gatk-sv:cohort-id"] == "cohort-x"
        assert tags["gatk-sv:workflow-version"] == "1.2.3"
        assert tags["gatk-sv:environment"] == "prod"

    # The second call supplied module and sample_count so those keys appear.
    _, _, second_tags = recorder.applied[1]
    assert second_tags["gatk-sv:module"] == "GenotypeBatch"
    assert second_tags["gatk-sv:sample-count"] == "100"

    # The first call omitted module and sample_count so those keys are absent.
    _, _, first_tags = recorder.applied[0]
    assert "gatk-sv:module" not in first_tags
    assert "gatk-sv:sample-count" not in first_tags


def test_apply_cost_tags_records_custom_environment() -> None:
    recorder = TagRecorder()
    apply_cost_tags(
        recorder,
        "CloudWatchLogGroup",
        "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/omics/x",
        cohort_id="cohort-v",
        workflow_version="0.1.0",
        environment="validation",
    )

    _, _, tags = recorder.applied[0]
    assert tags["gatk-sv:environment"] == "validation"


# ---------------------------------------------------------------------------
# Defensive sanity check on the frozen-dataclass contract
# ---------------------------------------------------------------------------


def test_manifest_issue_is_frozen_dataclass() -> None:
    issue = ManifestIssue(sample_id="S1", rule="duplicate_id", detail="x")
    # Frozen dataclass: attribute assignment must raise.
    try:
        issue.sample_id = "S2"  # type: ignore[misc]
    except Exception:
        pass
    else:  # pragma: no cover - fails only if frozen=True regresses
        raise AssertionError("ManifestIssue should be a frozen dataclass")



# ---------------------------------------------------------------------------
# submit_cohort (Task 3.7.4 — Req 6.4, 7.1, 10.1, 10.2, 11.2, 14.1, 14.2, 16.4)
# ---------------------------------------------------------------------------


def _make_version_records() -> dict:
    from gatk_sv_aws.models import (
        MIGRATED_MODULES,
        WorkflowVersionRecord,
    )

    return {
        module: WorkflowVersionRecord(
            module=module,
            workflow_id=f"wf-{i}",
            version_name="1.0.0",
            semver="1.0.0",
            upstream_commit="7eb2af1feea9",
            divergences=[],
            container_registry_map_uri="s3://bkt/reg-map.json",
            parameter_template_uri=f"s3://bkt/tpl/{module}.json",
        )
        for i, module in enumerate(MIGRATED_MODULES)
    }


class FakeRunClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.counter = 0

    def start_run(self, **kwargs):
        self.counter += 1
        run_id = f"run-{self.counter:06d}"
        self.calls.append(kwargs)
        return {
            "id": run_id,
            "arn": f"arn:aws:omics:ap-southeast-1::run/{run_id}",
            "status": "PENDING",
        }


def test_submit_cohort_calls_start_run_once_per_module() -> None:
    from gatk_sv_aws.models import MIGRATED_MODULES
    from gatk_sv_aws.orchestrator import (
        StorageChoice,
        submit_cohort,
    )

    manifest = _make_manifest(
        _make_sample("S1"),
        _make_sample(
            "S2",
            reads_uri="s3://bucket/s2.cram",
            index_uri="s3://bucket/s2.cram.crai",
            sex="F",
        ),
    )
    client = FakeRunClient()
    recorder = TagRecorder()

    record = submit_cohort(
        client,
        manifest,
        cohort_id="validation-2026q2",
        workflow_versions=_make_version_records(),
        output_uri="s3://outputs/validation-2026q2",
        role_arn="arn:aws:iam::__ACCOUNT_ID__:role/gatk-sv-run",
        storage=StorageChoice(storage_type="DYNAMIC", storage_capacity_gib=None),
        cache_id="cache-1",
        recorder=recorder,
    )

    # One StartRun per migrated module.
    assert len(client.calls) == len(MIGRATED_MODULES)
    # Modules submitted in order.
    submitted_modules = [call["tags"]["gatk-sv:module"] for call in client.calls]
    assert submitted_modules == list(MIGRATED_MODULES)
    # Record captures 10 module runs.
    assert len(record.module_runs) == len(MIGRATED_MODULES)
    assert record.status == "RUNNING"
    assert record.cohort_id == "validation-2026q2"
    # Cost tags applied to every resource (Property 10).
    assert len(recorder.applied) == len(MIGRATED_MODULES)
    for _, _, tags in recorder.applied:
        assert tags["gatk-sv:cohort-id"] == "validation-2026q2"
        assert tags["gatk-sv:workflow-version"] == "1.0.0"


def test_submit_cohort_honors_static_storage_capacity() -> None:
    from gatk_sv_aws.orchestrator import (
        StorageChoice,
        submit_cohort,
    )

    manifest = _make_manifest(_make_sample("S1"))
    client = FakeRunClient()

    submit_cohort(
        client,
        manifest,
        cohort_id="large-cohort",
        workflow_versions=_make_version_records(),
        output_uri="s3://outputs/large-cohort",
        role_arn="arn:aws:iam::__ACCOUNT_ID__:role/gatk-sv-run",
        storage=StorageChoice(storage_type="STATIC", storage_capacity_gib=2400),
        cache_id="cache-1",
        recorder=TagRecorder(),
    )

    for call in client.calls:
        assert call["storageType"] == "STATIC"
        assert call["storageCapacity"] == 2400


def test_submit_cohort_rejects_missing_workflow_version() -> None:
    from gatk_sv_aws.models import MIGRATED_MODULES
    from gatk_sv_aws.orchestrator import (
        StorageChoice,
        submit_cohort,
    )
    import pytest

    versions = _make_version_records()
    # Drop one module.
    removed_module = MIGRATED_MODULES[0]
    del versions[removed_module]

    with pytest.raises(ValueError, match=removed_module):
        submit_cohort(
            FakeRunClient(),
            _make_manifest(_make_sample("S1")),
            cohort_id="incomplete-cohort",
            workflow_versions=versions,
            output_uri="s3://outputs/incomplete-cohort",
            role_arn="arn:aws:iam::__ACCOUNT_ID__:role/gatk-sv-run",
            storage=StorageChoice(storage_type="DYNAMIC", storage_capacity_gib=None),
            cache_id="cache-1",
            recorder=TagRecorder(),
        )
