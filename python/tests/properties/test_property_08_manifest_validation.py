# Feature: gatk-sv-healthomics-migration, Property 8: Sample manifest validation
"""Property 8 — Sample manifest validation.

For any sample manifest, the validator SHALL accept the manifest if and
only if (a) every sample identifier is unique, (b) every reads URI has a
companion index URI (index object exists), and (c) every URI is an S3 URI
in Target_Region; when any of (a), (b), or (c) is violated, the validator
SHALL reject and name every offending sample identifier together with the
rule it violated.

See design §Correctness Properties → Property 8 and §Data Models → Sample
Manifest.

**Validates: Requirements 6.5, 6.6**

This test is RED until Task 3.7.1 implements ``validate_manifest``.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from gatk_sv_aws.models import (
    SampleManifest,
    SampleRecord,
)
from gatk_sv_aws.orchestrator import validate_manifest

_AWS_REGIONS = [
    "ap-southeast-1",  # Target_Region
    "ap-southeast-2",
    "us-east-1",
    "eu-west-1",
]

_sample_id = st.from_regex(r"\A[A-Za-z][A-Za-z0-9_-]{0,15}\Z", fullmatch=True)
_bucket = st.from_regex(r"\A[a-z0-9][a-z0-9-]{2,20}[a-z0-9]\Z", fullmatch=True)
_key = st.from_regex(r"\A[A-Za-z0-9_-]{1,15}\Z", fullmatch=True)


@st.composite
def manifest_with_oracles(draw: st.DrawFn):
    """Generate a manifest plus two oracles.

    Returns (manifest, region_oracle, exists_oracle) so the test can
    reason about which rules (a), (b), (c) were violated.
    """
    n = draw(st.integers(min_value=1, max_value=4))
    allow_duplicate = draw(st.booleans())
    base_ids = draw(st.lists(_sample_id, min_size=n, max_size=n, unique=not allow_duplicate))

    region_oracle: dict[str, str] = {}
    exists_oracle: dict[str, bool] = {}
    samples = []
    for sid in base_ids:
        reads_uri = f"s3://{draw(_bucket)}/{draw(_key)}.cram"
        index_uri = f"{reads_uri}.crai"
        region_oracle[reads_uri] = draw(st.sampled_from(_AWS_REGIONS))
        region_oracle[index_uri] = draw(st.sampled_from(_AWS_REGIONS))
        exists_oracle[reads_uri] = True
        exists_oracle[index_uri] = draw(st.booleans())
        samples.append(
            SampleRecord(
                sample_id=sid,
                reads_uri=reads_uri,
                index_uri=index_uri,
                sex=draw(st.sampled_from(["M", "F", "U"])),
            )
        )

    manifest = SampleManifest(
        cohort_id=draw(st.from_regex(r"\A[A-Za-z][A-Za-z0-9_-]{0,15}\Z", fullmatch=True)),
        reference_build="GRCh38",
        samples=samples,
    )
    return manifest, region_oracle, exists_oracle


TARGET_REGION = "ap-southeast-1"


@given(submission=manifest_with_oracles())
def test_property_08_manifest_validation(submission) -> None:  # type: ignore[no-untyped-def]
    """Validator accepts iff uniqueness, index presence, and region all hold."""

    manifest, region_oracle, exists_oracle = submission

    ids = [s.sample_id for s in manifest.samples]
    unique_ok = len(ids) == len(set(ids))

    index_ok = all(exists_oracle[s.index_uri] for s in manifest.samples)

    region_ok = all(region == TARGET_REGION for region in region_oracle.values())

    def _region_resolver(uri: str) -> str:
        return region_oracle.get(uri, "unknown")

    def _exists_resolver(uri: str) -> bool:
        return exists_oracle[uri]

    # The validator takes a region_resolver and an exists_resolver. Both are
    # injected so the orchestrator can supply boto3-backed implementations
    # at run time while this test supplies oracles.
    issues = validate_manifest(
        manifest,
        region_resolver=_region_resolver,
        exists_resolver=_exists_resolver,
    )

    clean = unique_ok and index_ok and region_ok

    if clean:
        assert issues == []
    else:
        assert len(issues) >= 1
        rules = {issue.rule for issue in issues}
        if not unique_ok:
            assert "duplicate_id" in rules
        if not region_ok:
            assert "out_of_region" in rules
        if not index_ok:
            assert "missing_index" in rules
            missing_index_sample_ids = {
                issue.sample_id for issue in issues if issue.rule == "missing_index"
            }
            expected_missing = {
                s.sample_id for s in manifest.samples if not exists_oracle[s.index_uri]
            }
            assert expected_missing.issubset(missing_index_sample_ids)
