"""Unit tests for the Step Functions Lambda handlers.

Tests cover the four Lambda handlers:
- validate_manifest: manifest validation with S3 resolution
- start_run: HealthOmics run submission with cache and tags
- poll_status: run status polling with terminal state detection
- gather_cost: cost report generation and S3 write

All tests use mocked boto3 clients to avoid real AWS calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# validate_manifest tests
# ---------------------------------------------------------------------------


class TestValidateManifestHandler:
    """Tests for the validate-manifest Lambda handler."""

    def _make_valid_manifest_dict(self) -> dict[str, Any]:
        return {
            "cohort_id": "test-cohort",
            "reference_build": "GRCh38",
            "samples": [
                {
                    "sample_id": "S1",
                    "reads_uri": "s3://bucket/s1.cram",
                    "index_uri": "s3://bucket/s1.cram.crai",
                    "sex": "M",
                },
                {
                    "sample_id": "S2",
                    "reads_uri": "s3://bucket/s2.bam",
                    "index_uri": "s3://bucket/s2.bam.bai",
                    "sex": "F",
                },
            ],
        }

    @patch(
        "gatk_sv_aws.step_functions.lambdas.validate_manifest.s3_client"
    )
    def test_valid_inline_manifest_passes(self, mock_s3: MagicMock) -> None:
        from gatk_sv_aws.step_functions.lambdas.validate_manifest import (
            handler,
        )

        # Mock get_bucket_location to return target region
        mock_s3.get_bucket_location.return_value = {
            "LocationConstraint": "ap-southeast-1"
        }

        event = {
            "cohort_id": "test-cohort",
            "sample_manifest": self._make_valid_manifest_dict(),
            "output_uri": "s3://output-bucket/prefix",
            "target_region": "ap-southeast-1",
        }

        result = handler(event, None)

        assert result["validation_status"] == "PASSED"
        assert result["sample_count"] == 2
        assert result["errors"] == []
        assert result["manifest"] is not None

    @patch(
        "gatk_sv_aws.step_functions.lambdas.validate_manifest.s3_client"
    )
    def test_manifest_with_duplicate_ids_fails(self, mock_s3: MagicMock) -> None:
        from gatk_sv_aws.step_functions.lambdas.validate_manifest import (
            handler,
        )

        mock_s3.get_bucket_location.return_value = {
            "LocationConstraint": "ap-southeast-1"
        }

        manifest = self._make_valid_manifest_dict()
        manifest["samples"][1]["sample_id"] = "S1"  # Duplicate

        event = {
            "cohort_id": "test-cohort",
            "sample_manifest": manifest,
            "output_uri": "s3://output-bucket/prefix",
            "target_region": "ap-southeast-1",
        }

        result = handler(event, None)

        assert result["validation_status"] == "FAILED"
        duplicate_errors = [e for e in result["errors"] if e["rule"] == "duplicate_id"]
        assert len(duplicate_errors) == 2

    @patch(
        "gatk_sv_aws.step_functions.lambdas.validate_manifest.s3_client"
    )
    def test_manifest_with_out_of_region_uri_fails(self, mock_s3: MagicMock) -> None:
        from gatk_sv_aws.step_functions.lambdas.validate_manifest import (
            handler,
        )

        # Return us-east-1 for the bucket
        mock_s3.get_bucket_location.return_value = {
            "LocationConstraint": "us-east-1"
        }

        event = {
            "cohort_id": "test-cohort",
            "sample_manifest": self._make_valid_manifest_dict(),
            "output_uri": "s3://output-bucket/prefix",
            "target_region": "ap-southeast-1",
        }

        result = handler(event, None)

        assert result["validation_status"] == "FAILED"
        region_errors = [e for e in result["errors"] if e["rule"] == "out_of_region"]
        assert len(region_errors) > 0

    @patch(
        "gatk_sv_aws.step_functions.lambdas.validate_manifest.s3_client"
    )
    def test_manifest_with_unsupported_format_fails(self, mock_s3: MagicMock) -> None:
        from gatk_sv_aws.step_functions.lambdas.validate_manifest import (
            handler,
        )

        mock_s3.get_bucket_location.return_value = {
            "LocationConstraint": "ap-southeast-1"
        }

        manifest = self._make_valid_manifest_dict()
        manifest["samples"][0]["reads_uri"] = "s3://bucket/s1.sam"
        manifest["samples"][0]["index_uri"] = "s3://bucket/s1.sam.bai"

        event = {
            "cohort_id": "test-cohort",
            "sample_manifest": manifest,
            "output_uri": "s3://output-bucket/prefix",
            "target_region": "ap-southeast-1",
        }

        result = handler(event, None)

        assert result["validation_status"] == "FAILED"
        format_errors = [
            e for e in result["errors"] if e["rule"] == "unsupported_format"
        ]
        assert len(format_errors) > 0

    @patch(
        "gatk_sv_aws.step_functions.lambdas.validate_manifest.s3_client"
    )
    def test_manifest_from_s3_uri_is_resolved(self, mock_s3: MagicMock) -> None:
        from gatk_sv_aws.step_functions.lambdas.validate_manifest import (
            handler,
        )

        manifest_dict = self._make_valid_manifest_dict()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(manifest_dict).encode("utf-8"))
        }
        mock_s3.get_bucket_location.return_value = {
            "LocationConstraint": "ap-southeast-1"
        }

        event = {
            "cohort_id": "test-cohort",
            "sample_manifest": "s3://manifests-bucket/cohort/manifest.json",
            "output_uri": "s3://output-bucket/prefix",
            "target_region": "ap-southeast-1",
        }

        result = handler(event, None)

        assert result["validation_status"] == "PASSED"
        assert result["sample_count"] == 2
        mock_s3.get_object.assert_called_once_with(
            Bucket="manifests-bucket", Key="cohort/manifest.json"
        )

    def test_invalid_input_schema_returns_failed(self) -> None:
        from gatk_sv_aws.step_functions.lambdas.validate_manifest import (
            handler,
        )

        # Missing required fields
        event = {"cohort_id": "test"}

        result = handler(event, None)

        assert result["validation_status"] == "FAILED"
        assert len(result["errors"]) > 0
        assert result["errors"][0]["rule"] == "input_schema"


# ---------------------------------------------------------------------------
# start_run tests
# ---------------------------------------------------------------------------


class TestStartRunHandler:
    """Tests for the start-run Lambda handler."""

    @patch(
        "gatk_sv_aws.step_functions.lambdas.start_run.omics_client"
    )
    def test_successful_run_submission(self, mock_omics: MagicMock) -> None:
        from gatk_sv_aws.step_functions.lambdas.start_run import (
            handler,
        )

        mock_omics.start_run.return_value = {
            "id": "run-123456",
            "arn": "arn:aws:omics:ap-southeast-1:__ACCOUNT_ID__:run/run-123456",
            "status": "PENDING",
        }

        event = {
            "module": "GatherBatchEvidence",
            "workflow_id": "wf-001",
            "workflow_version_name": "v1.0.0",
            "parameters": {"cohort_id": "test-cohort"},
            "output_uri": "s3://output-bucket/runs/test-cohort/GatherBatchEvidence/",
            "cohort_id": "test-cohort",
            "sample_count": 5,
            "attempt_number": 1,
        }

        result = handler(event, None)

        assert result["run_id"] == "run-123456"
        assert result["arn"] == "arn:aws:omics:ap-southeast-1:__ACCOUNT_ID__:run/run-123456"
        assert result["status"] == "PENDING"
        assert result["module"] == "GatherBatchEvidence"
        assert result["attempt_number"] == 1

    @patch(
        "gatk_sv_aws.step_functions.lambdas.start_run.omics_client"
    )
    def test_run_submission_includes_cache_and_tags(self, mock_omics: MagicMock) -> None:
        from gatk_sv_aws.step_functions.lambdas.start_run import (
            handler,
        )

        mock_omics.start_run.return_value = {
            "id": "run-789",
            "arn": "arn:aws:omics:ap-southeast-1:__ACCOUNT_ID__:run/run-789",
            "status": "PENDING",
        }

        event = {
            "module": "ClusterBatch",
            "workflow_id": "wf-002",
            "workflow_version_name": "v2.0.0",
            "parameters": {},
            "output_uri": "s3://output-bucket/runs/cohort-x/ClusterBatch/",
            "cohort_id": "cohort-x",
            "sample_count": 10,
            "attempt_number": 2,
        }

        handler(event, None)

        # Verify the call to start_run
        call_kwargs = mock_omics.start_run.call_args[1]
        assert call_kwargs["cacheId"] is not None
        assert call_kwargs["cacheBehavior"] == "CACHE_ALWAYS"
        assert call_kwargs["storageType"] == "DYNAMIC"
        assert call_kwargs["tags"]["gatk-sv:cohort-id"] == "cohort-x"
        assert call_kwargs["tags"]["gatk-sv:workflow-version"] == "v2.0.0"
        assert call_kwargs["tags"]["gatk-sv:module"] == "ClusterBatch"
        assert call_kwargs["tags"]["gatk-sv:sample-count"] == "10"

    @patch(
        "gatk_sv_aws.step_functions.lambdas.start_run.omics_client"
    )
    def test_run_name_includes_cohort_module_attempt(
        self, mock_omics: MagicMock
    ) -> None:
        from gatk_sv_aws.step_functions.lambdas.start_run import (
            handler,
        )

        mock_omics.start_run.return_value = {
            "id": "run-abc",
            "arn": "arn:aws:omics:ap-southeast-1:__ACCOUNT_ID__:run/run-abc",
            "status": "PENDING",
        }

        event = {
            "module": "FilterBatch",
            "workflow_id": "wf-003",
            "workflow_version_name": "v1.0.0",
            "parameters": {},
            "output_uri": "s3://output-bucket/runs/my-cohort/FilterBatch/",
            "cohort_id": "my-cohort",
            "sample_count": 3,
            "attempt_number": 3,
        }

        handler(event, None)

        call_kwargs = mock_omics.start_run.call_args[1]
        assert call_kwargs["name"] == "my-cohort-FilterBatch-attempt3"

    @patch.dict(
        "os.environ",
        {
            "HEALTHOMICS_ROLE_ARN": "arn:aws:iam::111111111111:role/custom-role",
            "CACHE_ID": "custom-cache-99",
        },
    )
    @patch(
        "gatk_sv_aws.step_functions.lambdas.start_run.omics_client"
    )
    def test_reads_role_and_cache_from_environment(
        self, mock_omics: MagicMock
    ) -> None:
        from gatk_sv_aws.step_functions.lambdas.start_run import (
            handler,
        )

        mock_omics.start_run.return_value = {
            "id": "run-env",
            "arn": "arn:aws:omics:ap-southeast-1:111111111111:run/run-env",
            "status": "PENDING",
        }

        event = {
            "module": "GenotypeBatch",
            "workflow_id": "wf-004",
            "workflow_version_name": "v1.0.0",
            "parameters": {},
            "output_uri": "s3://output-bucket/runs/cohort-env/GenotypeBatch/",
            "cohort_id": "cohort-env",
            "sample_count": 7,
            "attempt_number": 1,
        }

        handler(event, None)

        call_kwargs = mock_omics.start_run.call_args[1]
        assert call_kwargs["roleArn"] == "arn:aws:iam::111111111111:role/custom-role"
        assert call_kwargs["cacheId"] == "custom-cache-99"

    def test_invalid_input_raises_value_error(self) -> None:
        from gatk_sv_aws.step_functions.lambdas.start_run import (
            handler,
        )

        with pytest.raises(ValueError, match="Invalid start-run input"):
            handler({"module": "InvalidModule"}, None)


# ---------------------------------------------------------------------------
# poll_status tests
# ---------------------------------------------------------------------------


class TestPollStatusHandler:
    """Tests for the poll-status Lambda handler."""

    @patch(
        "gatk_sv_aws.step_functions.lambdas.poll_status.omics_client"
    )
    def test_running_status_is_not_terminal(self, mock_omics: MagicMock) -> None:
        from gatk_sv_aws.step_functions.lambdas.poll_status import (
            handler,
        )

        mock_omics.get_run.return_value = {
            "status": "RUNNING",
            "startTime": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        }

        event = {
            "run_id": "run-001",
            "module": "GatherBatchEvidence",
            "cohort_id": "test-cohort",
            "attempt_number": 1,
        }

        result = handler(event, None)

        assert result["run_id"] == "run-001"
        assert result["status"] == "RUNNING"
        assert result["is_terminal"] is False
        assert result["output_uri"] is None
        assert result["failure_reason"] is None

    @patch(
        "gatk_sv_aws.step_functions.lambdas.poll_status.omics_client"
    )
    def test_completed_status_is_terminal_with_output_uri(
        self, mock_omics: MagicMock
    ) -> None:
        from gatk_sv_aws.step_functions.lambdas.poll_status import (
            handler,
        )

        mock_omics.get_run.return_value = {
            "status": "COMPLETED",
            "runOutputUri": "s3://output-bucket/runs/test-cohort/GBE/run-001/",
            "startTime": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            "stopTime": datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc),
        }

        event = {
            "run_id": "run-001",
            "module": "GatherBatchEvidence",
            "cohort_id": "test-cohort",
            "attempt_number": 1,
        }

        result = handler(event, None)

        assert result["status"] == "COMPLETED"
        assert result["is_terminal"] is True
        assert result["output_uri"] == "s3://output-bucket/runs/test-cohort/GBE/run-001/"
        assert result["duration_seconds"] == 3600
        assert result["is_cache_hit"] is False

    @patch(
        "gatk_sv_aws.step_functions.lambdas.poll_status.omics_client"
    )
    def test_cache_hit_detected_for_short_duration(
        self, mock_omics: MagicMock
    ) -> None:
        from gatk_sv_aws.step_functions.lambdas.poll_status import (
            handler,
        )

        mock_omics.get_run.return_value = {
            "status": "COMPLETED",
            "runOutputUri": "s3://output-bucket/cached/",
            "startTime": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            "stopTime": datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
        }

        event = {
            "run_id": "run-cached",
            "module": "ClusterBatch",
            "cohort_id": "test-cohort",
            "attempt_number": 1,
        }

        result = handler(event, None)

        assert result["is_cache_hit"] is True
        assert result["duration_seconds"] == 5

    @patch(
        "gatk_sv_aws.step_functions.lambdas.poll_status.omics_client"
    )
    def test_failed_status_extracts_failure_reason_and_error_code(
        self, mock_omics: MagicMock
    ) -> None:
        from gatk_sv_aws.step_functions.lambdas.poll_status import (
            handler,
        )

        mock_omics.get_run.return_value = {
            "status": "FAILED",
            "statusMessage": "InternalServerError: task execution failed",
            "startTime": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            "stopTime": datetime(2026, 1, 1, 0, 30, 0, tzinfo=timezone.utc),
        }

        event = {
            "run_id": "run-failed",
            "module": "GenotypeBatch",
            "cohort_id": "test-cohort",
            "attempt_number": 2,
        }

        result = handler(event, None)

        assert result["status"] == "FAILED"
        assert result["is_terminal"] is True
        assert result["failure_reason"] == "InternalServerError: task execution failed"
        assert result["error_code"] == "InternalServerError"
        assert result["duration_seconds"] == 1800

    @patch(
        "gatk_sv_aws.step_functions.lambdas.poll_status.omics_client"
    )
    def test_cancelled_status_is_terminal(self, mock_omics: MagicMock) -> None:
        from gatk_sv_aws.step_functions.lambdas.poll_status import (
            handler,
        )

        mock_omics.get_run.return_value = {
            "status": "CANCELLED",
            "startTime": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            "stopTime": datetime(2026, 1, 1, 0, 10, 0, tzinfo=timezone.utc),
        }

        event = {
            "run_id": "run-cancelled",
            "module": "MakeCohortVcf",
            "cohort_id": "test-cohort",
            "attempt_number": 1,
        }

        result = handler(event, None)

        assert result["status"] == "CANCELLED"
        assert result["is_terminal"] is True

    def test_invalid_input_raises_value_error(self) -> None:
        from gatk_sv_aws.step_functions.lambdas.poll_status import (
            handler,
        )

        with pytest.raises(ValueError, match="Invalid poll-status input"):
            handler({"run_id": ""}, None)


# ---------------------------------------------------------------------------
# gather_cost tests
# ---------------------------------------------------------------------------


class TestGatherCostHandler:
    """Tests for the gather-cost Lambda handler."""

    @patch(
        "gatk_sv_aws.step_functions.lambdas.gather_cost.s3_client"
    )
    @patch(
        "gatk_sv_aws.step_functions.lambdas.gather_cost.omics_client"
    )
    def test_successful_cost_report_generation(
        self, mock_omics: MagicMock, mock_s3: MagicMock
    ) -> None:
        from gatk_sv_aws.step_functions.lambdas.gather_cost import (
            handler,
        )

        # Mock GetRun responses for two modules
        mock_omics.get_run.side_effect = [
            {
                "startTime": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                "stopTime": datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc),
            },
            {
                "startTime": datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc),
                "stopTime": datetime(2026, 1, 1, 2, 0, 5, tzinfo=timezone.utc),
            },
        ]

        event = {
            "cohort_id": "test-cohort",
            "sample_count": 5,
            "output_uri": "s3://output-bucket/runs/test-cohort",
            "module_runs": [
                {
                    "module": "GatherSampleEvidence",
                    "run_id": "run-111",
                    "status": "COMPLETED",
                    "is_cache_hit": False,
                },
                {
                    "module": "GatherBatchEvidence",
                    "run_id": "run-222",
                    "status": "COMPLETED",
                    "is_cache_hit": True,
                },
            ],
        }

        result = handler(event, None)

        assert result["cost_report"]["cohort_id"] == "test-cohort"
        assert result["cost_report"]["sample_count"] == 5
        assert len(result["cost_report"]["modules"]) == 2

        # First module: 2 hours = 7200 seconds * 0.001 = 7.2 USD
        assert result["cost_report"]["modules"][0]["cost_usd"] == pytest.approx(7.2)
        assert result["cost_report"]["modules"][0]["duration_seconds"] == 7200
        assert result["cost_report"]["modules"][0]["is_cache_hit"] is False

        # Second module: cache hit = 0 USD
        assert result["cost_report"]["modules"][1]["cost_usd"] == 0.0
        assert result["cost_report"]["modules"][1]["is_cache_hit"] is True

        # Total and per-sample
        assert result["cost_report"]["total_cost_usd"] == pytest.approx(7.2)
        assert result["cost_report"]["per_sample_cost_usd"] == pytest.approx(7.2 / 5)

        # Cost report URI
        assert (
            result["cost_report_uri"]
            == "s3://output-bucket/runs/test-cohort/cost-report.json"
        )

        # Verify S3 write
        mock_s3.put_object.assert_called_once()
        put_kwargs = mock_s3.put_object.call_args[1]
        assert put_kwargs["Bucket"] == "output-bucket"
        assert put_kwargs["Key"] == "runs/test-cohort/cost-report.json"
        assert put_kwargs["ContentType"] == "application/json"

    @patch(
        "gatk_sv_aws.step_functions.lambdas.gather_cost.s3_client"
    )
    @patch(
        "gatk_sv_aws.step_functions.lambdas.gather_cost.omics_client"
    )
    def test_cost_report_uri_handles_trailing_slash(
        self, mock_omics: MagicMock, mock_s3: MagicMock
    ) -> None:
        from gatk_sv_aws.step_functions.lambdas.gather_cost import (
            handler,
        )

        mock_omics.get_run.return_value = {
            "startTime": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            "stopTime": datetime(2026, 1, 1, 0, 10, 0, tzinfo=timezone.utc),
        }

        event = {
            "cohort_id": "test-cohort",
            "sample_count": 1,
            "output_uri": "s3://output-bucket/runs/test-cohort/",  # trailing slash
            "module_runs": [
                {
                    "module": "GatherSampleEvidence",
                    "run_id": "run-111",
                    "status": "COMPLETED",
                    "is_cache_hit": False,
                },
            ],
        }

        result = handler(event, None)

        # Should have exactly one slash before cost-report.json
        assert (
            result["cost_report_uri"]
            == "s3://output-bucket/runs/test-cohort/cost-report.json"
        )

    @patch(
        "gatk_sv_aws.step_functions.lambdas.gather_cost.s3_client"
    )
    @patch(
        "gatk_sv_aws.step_functions.lambdas.gather_cost.omics_client"
    )
    def test_cost_report_generated_at_is_iso_timestamp(
        self, mock_omics: MagicMock, mock_s3: MagicMock
    ) -> None:
        from gatk_sv_aws.step_functions.lambdas.gather_cost import (
            handler,
        )

        mock_omics.get_run.return_value = {
            "startTime": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            "stopTime": datetime(2026, 1, 1, 0, 5, 0, tzinfo=timezone.utc),
        }

        event = {
            "cohort_id": "test-cohort",
            "sample_count": 2,
            "output_uri": "s3://output-bucket/prefix",
            "module_runs": [
                {
                    "module": "ClusterBatch",
                    "run_id": "run-333",
                    "status": "COMPLETED",
                    "is_cache_hit": False,
                },
            ],
        }

        result = handler(event, None)

        # generated_at should be a valid ISO timestamp
        generated_at = result["cost_report"]["generated_at"]
        assert generated_at is not None
        # Should be parseable as ISO format
        datetime.fromisoformat(generated_at)

    def test_invalid_input_raises_value_error(self) -> None:
        from gatk_sv_aws.step_functions.lambdas.gather_cost import (
            handler,
        )

        with pytest.raises(ValueError, match="Invalid gather-cost input"):
            handler({"cohort_id": "x"}, None)


# ---------------------------------------------------------------------------
# Task 6.1: StateMachineInput schema validation tests
# ---------------------------------------------------------------------------


class TestStateMachineInput:
    """Tests for the StateMachineInput model (Task 6.1)."""

    def test_valid_input_with_inline_manifest(self) -> None:
        from gatk_sv_aws.step_functions.models import (
            StateMachineInput,
        )

        data = {
            "cohort_id": "cohort-1",
            "sample_manifest": {"samples": [{"sample_id": "S1"}]},
            "output_uri": "s3://bucket/prefix",
        }
        model = StateMachineInput.model_validate(data)
        assert model.cohort_id == "cohort-1"
        assert model.output_uri == "s3://bucket/prefix"
        assert model.overrides is None

    def test_valid_input_with_s3_uri_manifest(self) -> None:
        from gatk_sv_aws.step_functions.models import (
            StateMachineInput,
        )

        data = {
            "cohort_id": "cohort-2",
            "sample_manifest": "s3://manifests/cohort-2.json",
            "output_uri": "s3://bucket/output",
        }
        model = StateMachineInput.model_validate(data)
        assert model.sample_manifest == "s3://manifests/cohort-2.json"

    def test_valid_input_with_overrides(self) -> None:
        from gatk_sv_aws.step_functions.models import (
            StateMachineInput,
        )

        data = {
            "cohort_id": "cohort-3",
            "sample_manifest": {"samples": []},
            "output_uri": "s3://bucket/out",
            "overrides": {
                "storage_type": "STATIC",
                "cache_id": "12345",
                "networking_mode": "VPC",
            },
        }
        model = StateMachineInput.model_validate(data)
        assert model.overrides is not None
        assert model.overrides.storage_type == "STATIC"
        assert model.overrides.cache_id == "12345"
        assert model.overrides.networking_mode == "VPC"

    def test_missing_cohort_id_fails(self) -> None:
        from pydantic import ValidationError

        from gatk_sv_aws.step_functions.models import (
            StateMachineInput,
        )

        with pytest.raises(ValidationError):
            StateMachineInput.model_validate(
                {
                    "sample_manifest": {"samples": []},
                    "output_uri": "s3://bucket/out",
                }
            )

    def test_missing_sample_manifest_fails(self) -> None:
        from pydantic import ValidationError

        from gatk_sv_aws.step_functions.models import (
            StateMachineInput,
        )

        with pytest.raises(ValidationError):
            StateMachineInput.model_validate(
                {
                    "cohort_id": "cohort-1",
                    "output_uri": "s3://bucket/out",
                }
            )

    def test_missing_output_uri_fails(self) -> None:
        from pydantic import ValidationError

        from gatk_sv_aws.step_functions.models import (
            StateMachineInput,
        )

        with pytest.raises(ValidationError):
            StateMachineInput.model_validate(
                {
                    "cohort_id": "cohort-1",
                    "sample_manifest": {"samples": []},
                }
            )

    def test_empty_cohort_id_fails(self) -> None:
        from pydantic import ValidationError

        from gatk_sv_aws.step_functions.models import (
            StateMachineInput,
        )

        with pytest.raises(ValidationError):
            StateMachineInput.model_validate(
                {
                    "cohort_id": "",
                    "sample_manifest": {"samples": []},
                    "output_uri": "s3://bucket/out",
                }
            )


# ---------------------------------------------------------------------------
# Task 6.3: Pipeline output structure tests
# ---------------------------------------------------------------------------


class TestPipelineOutputModels:
    """Tests for PipelineSuccessOutput and PipelineFailureOutput (Task 6.3)."""

    def test_success_output_with_all_fields(self) -> None:
        from gatk_sv_aws.step_functions.models import (
            ModuleRunSummary,
            PipelineSuccessOutput,
        )

        module_runs = [
            ModuleRunSummary(
                module="GatherSampleEvidence",
                run_id=f"run-{i}",
                duration_seconds=3600,
                is_cache_hit=False,
            )
            for i in range(10)
        ]
        # Fix module names to be valid
        modules = [
            "GatherSampleEvidence", "GatherBatchEvidence", "ClusterBatch",
            "GenerateBatchMetrics", "FilterBatch", "MergeBatchSites",
            "GenotypeBatch", "RegenotypeCNVs", "MakeCohortVcf", "AnnotateVcf",
        ]
        module_runs = [
            ModuleRunSummary(
                module=m,
                run_id=f"run-{i}",
                duration_seconds=3600,
                is_cache_hit=(i % 2 == 0),
            )
            for i, m in enumerate(modules)
        ]

        output = PipelineSuccessOutput(
            cohort_id="cohort-1",
            status="COMPLETED",
            module_runs=module_runs,
            total_cost_usd=32.50,
            per_sample_cost_usd=6.50,
            output_uri="s3://bucket/prefix",
            duration_seconds=86400,
            cost_report_uri="s3://bucket/prefix/cost-report.json",
        )

        assert output.status == "COMPLETED"
        assert len(output.module_runs) == 10
        assert output.total_cost_usd == 32.50
        assert output.per_sample_cost_usd == 6.50

    def test_failure_output_with_all_fields(self) -> None:
        from gatk_sv_aws.step_functions.models import (
            CompletedModuleSummary,
            PipelineFailureOutput,
        )

        output = PipelineFailureOutput(
            cohort_id="cohort-1",
            status="FAILED",
            failed_module="ClusterBatch",
            failed_run_id="run-333",
            error_message="OutOfMemoryError: task exceeded memory limit",
            error_code="OutOfMemoryError",
            retry_attempts=3,
            completed_modules=[
                CompletedModuleSummary(module="GatherSampleEvidence", run_id="run-111"),
                CompletedModuleSummary(module="GatherBatchEvidence", run_id="run-222"),
            ],
            partial_cost_report={"total_cost_usd": 15.0},
        )

        assert output.status == "FAILED"
        assert output.failed_module == "ClusterBatch"
        assert output.retry_attempts == 3
        assert len(output.completed_modules) == 2
        assert output.partial_cost_report is not None

    def test_failure_output_without_optional_fields(self) -> None:
        from gatk_sv_aws.step_functions.models import (
            PipelineFailureOutput,
        )

        output = PipelineFailureOutput(
            cohort_id="cohort-1",
            failed_module="GatherSampleEvidence",
            failed_run_id="run-001",
            error_message="ServiceUnavailable: temporary failure",
            retry_attempts=3,
        )

        assert output.error_code is None
        assert output.completed_modules == []
        assert output.partial_cost_report is None


# ---------------------------------------------------------------------------
# Task 6.5: Structured logging tests
# ---------------------------------------------------------------------------


class TestStructuredLogging:
    """Tests for structured logging configuration (Task 6.5)."""

    def test_structured_formatter_outputs_json(self) -> None:
        import json
        import logging

        from gatk_sv_aws.step_functions.logging_config import (
            StructuredFormatter,
        )

        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=None,
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["level"] == "INFO"
        assert parsed["message"] == "Test message"
        assert "timestamp" in parsed

    def test_configure_lambda_logging_adds_context(self) -> None:
        import json
        import logging
        from io import StringIO

        from gatk_sv_aws.step_functions.logging_config import (
            configure_lambda_logging,
        )

        logger = configure_lambda_logging(
            cohort_id="test-cohort",
            module="GatherBatchEvidence",
            attempt_number=2,
            logger_name="test_structured_logging",
        )

        # Capture output
        stream = StringIO()
        logger.handlers[0].stream = stream

        logger.info("Test log entry")

        output = stream.getvalue().strip()
        parsed = json.loads(output)

        assert parsed["cohort_id"] == "test-cohort"
        assert parsed["current_module"] == "GatherBatchEvidence"
        assert parsed["attempt_number"] == 2
        assert parsed["message"] == "Test log entry"

    def test_structured_logging_includes_extra_fields(self) -> None:
        import json
        import logging
        from io import StringIO

        from gatk_sv_aws.step_functions.logging_config import (
            configure_lambda_logging,
        )

        logger = configure_lambda_logging(
            cohort_id="cohort-x",
            module="ClusterBatch",
            attempt_number=1,
            logger_name="test_extra_fields",
        )

        stream = StringIO()
        logger.handlers[0].stream = stream

        logger.info("Run submitted", extra={"run_id": "run-123", "workflow_id": "wf-001"})

        output = stream.getvalue().strip()
        parsed = json.loads(output)

        assert parsed["run_id"] == "run-123"
        assert parsed["workflow_id"] == "wf-001"
        assert parsed["cohort_id"] == "cohort-x"
        assert parsed["current_module"] == "ClusterBatch"
