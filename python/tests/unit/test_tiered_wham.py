"""Unit tests for tiered wham memory provisioning.

Tests verify:
- select_wham_tier correctly partitions sizes into Standard/High_Memory tiers
- get_cram_size_bytes issues HEAD requests and propagates errors
- Non-wham modules do not trigger S3 calls
- WHAM_SIZE_THRESHOLD_BYTES constant value
- Hypothesis property: tier selection partition holds for all inputs

Validates: Requirements 1.1, 2.1, 2.2, 2.4, 4.1, 4.2, 5.1
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Add the scripts directory to sys.path so we can import run_gse_cohort
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[3] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from run_gse_cohort import (  # noqa: E402
    COHORT_BASE,
    REGION,
    WHAM_SIZE_THRESHOLD_BYTES,
    WHAM_TIERS,
    WORKFLOWS,
    build_params,
    get_cram_size_bytes,
    launch_run,
    select_wham_tier,
)


# ---------------------------------------------------------------------------
# select_wham_tier — example-based tests
# ---------------------------------------------------------------------------


class TestSelectWhamTier:
    """Tests for the pure tier selection function."""

    def test_select_wham_tier_standard(self) -> None:
        """Verify select_wham_tier returns Standard_Tier when size <= 20 GiB."""
        # 10 GiB — well below threshold
        size_bytes = 10 * 1024**3
        tier = select_wham_tier(size_bytes)
        assert tier["label"] == "Standard_Tier"
        assert tier["id"] == "2723477"
        assert tier["memory_gib"] == 16

    def test_select_wham_tier_high_memory(self) -> None:
        """Verify select_wham_tier returns High_Memory_Tier when size > 20 GiB."""
        # 25 GiB — above threshold
        size_bytes = 25 * 1024**3
        tier = select_wham_tier(size_bytes)
        assert tier["label"] == "High_Memory_Tier"
        assert tier["id"] == "6217382"
        assert tier["memory_gib"] == 30

    def test_select_wham_tier_boundary(self) -> None:
        """Verify exactly at 20 GiB (threshold) returns Standard_Tier."""
        size_bytes = WHAM_SIZE_THRESHOLD_BYTES  # exactly 20 GiB
        tier = select_wham_tier(size_bytes)
        assert tier["label"] == "Standard_Tier"
        assert tier["id"] == "2723477"

    def test_select_wham_tier_one_byte_over(self) -> None:
        """Verify one byte over threshold returns High_Memory_Tier."""
        size_bytes = WHAM_SIZE_THRESHOLD_BYTES + 1
        tier = select_wham_tier(size_bytes)
        assert tier["label"] == "High_Memory_Tier"


# ---------------------------------------------------------------------------
# get_cram_size_bytes — mock-based tests
# ---------------------------------------------------------------------------


class TestGetCramSizeBytes:
    """Tests for the S3 HEAD-based size query."""

    def test_get_cram_size_bytes_calls_head(self) -> None:
        """Mock S3 client, verify head_object is called with correct bucket/key."""
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ContentLength": 15_000_000_000}

        result = get_cram_size_bytes(mock_s3, "my-bucket", "path/to/file.cram")

        mock_s3.head_object.assert_called_once_with(
            Bucket="my-bucket", Key="path/to/file.cram"
        )
        assert result == 15_000_000_000

    def test_get_cram_size_bytes_propagates_error(self) -> None:
        """Mock S3 client raising ClientError, verify it propagates."""
        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadObject",
        )

        with pytest.raises(ClientError):
            get_cram_size_bytes(mock_s3, "my-bucket", "missing.cram")


# ---------------------------------------------------------------------------
# Non-wham modules — verify no S3 interaction
# ---------------------------------------------------------------------------


class TestNonWhamModules:
    """Verify non-wham modules don't trigger S3 HEAD calls."""

    def test_non_wham_no_s3_call(self) -> None:
        """Mock S3 and verify non-wham modules don't trigger S3 HEAD calls."""
        from unittest.mock import patch

        non_wham_modules = ["manta", "cc", "scramble", "cse"]

        for module in non_wham_modules:
            with patch("run_gse_cohort.boto3") as mock_boto3:
                mock_omics_client = MagicMock()
                mock_omics_client.start_run.return_value = {"id": "run-123"}

                # build_params should work without S3
                params = build_params(module, "NA12878")
                assert isinstance(params, dict)

                # Verify boto3.client("s3", ...) was never called
                # (non-wham modules should not create an S3 client)
                s3_calls = [
                    call
                    for call in mock_boto3.client.call_args_list
                    if call[0][0] == "s3"
                ]
                assert len(s3_calls) == 0, (
                    f"Module '{module}' should not make S3 calls"
                )


# ---------------------------------------------------------------------------
# Hypothesis property test — tier selection partition
# ---------------------------------------------------------------------------


class TestTierSelectionProperty:
    """Property-based test for tier selection partition."""

    @given(
        size_bytes=st.integers(min_value=0, max_value=100 * 1024**3),
        threshold=st.integers(min_value=1, max_value=50 * 1024**3),
    )
    @settings(max_examples=100)
    def test_wham_tier_selection_property(
        self, size_bytes: int, threshold: int
    ) -> None:
        """For any non-negative size_bytes and positive threshold,
        verify partition property holds.

        **Validates: Requirements 2.1, 2.2**
        """
        tier = select_wham_tier(size_bytes, threshold=threshold)

        if size_bytes <= threshold:
            assert tier["label"] == "Standard_Tier"
            assert tier["id"] == WHAM_TIERS["standard"]["id"]
        else:
            assert tier["label"] == "High_Memory_Tier"
            assert tier["id"] == WHAM_TIERS["high_memory"]["id"]


# ---------------------------------------------------------------------------
# Threshold constant value
# ---------------------------------------------------------------------------


class TestThresholdConstant:
    """Verify the threshold constant value."""

    def test_threshold_constant_value(self) -> None:
        """Verify WHAM_SIZE_THRESHOLD_BYTES == 21_474_836_480 (20 GiB)."""
        assert WHAM_SIZE_THRESHOLD_BYTES == 21_474_836_480
        assert WHAM_SIZE_THRESHOLD_BYTES == 20 * 1024**3
