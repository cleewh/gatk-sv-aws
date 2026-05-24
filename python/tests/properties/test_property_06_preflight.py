# Feature: gatk-sv-healthomics-migration, Property 6: Cross-region preflight soundness
"""Property 6 — Cross-region preflight soundness.

For any cohort submission consisting of a sample manifest, a
Reference_Bundle prefix, and a Container_Registry_Map, the cross-region
preflight SHALL accept the submission if and only if every referenced S3
URI resolves to a bucket in Target_Region and every ECR URI resolves to a
repository in Target_Region; when any artifact is outside Target_Region,
the preflight SHALL reject with a report that names each offending
artifact and its observed region.

See design §Correctness Properties → Property 6 and §Components.g.

**Validates: Requirements 1.4, 4.4, 11.1**

This test is RED until Task 3.7.2 implements ``cross_region_preflight``.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from gatk_sv_aws.models import (
    ContainerRegistryMap,
    ImageMapping,
    SampleManifest,
    SampleRecord,
)
from gatk_sv_aws.orchestrator import (
    TARGET_REGION,
    cross_region_preflight,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_AWS_REGIONS = [
    "ap-southeast-1",  # Target_Region
    "ap-southeast-2",
    "us-east-1",
    "us-west-2",
    "eu-west-1",
    "ap-northeast-1",
]

_bucket_name = st.from_regex(r"\A[a-z0-9][a-z0-9-]{2,30}[a-z0-9]\Z", fullmatch=True)
_key = st.from_regex(r"\A[A-Za-z0-9_/.-]{1,40}\Z", fullmatch=True)


@st.composite
def s3_uri_with_region(draw: st.DrawFn) -> tuple[str, str]:
    """Generate an ``s3://bucket/key`` URI paired with the bucket's declared region."""
    region = draw(st.sampled_from(_AWS_REGIONS))
    bucket = draw(_bucket_name)
    key = draw(_key)
    return f"s3://{bucket}/{key}", region


@st.composite
def ecr_uri_with_region(draw: st.DrawFn) -> tuple[str, str]:
    """Generate an ECR image URI paired with its repository region."""
    region = draw(st.sampled_from(_AWS_REGIONS))
    account = draw(st.integers(min_value=10**11, max_value=10**12 - 1)).__str__()
    repo = draw(st.from_regex(r"\A[a-z0-9][a-z0-9/_-]{1,30}[a-z0-9]\Z", fullmatch=True))
    tag = draw(st.from_regex(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,20}\Z", fullmatch=True))
    return f"{account}.dkr.ecr.{region}.amazonaws.com/{repo}:{tag}", region


@st.composite
def cohort_submission(draw: st.DrawFn) -> tuple[SampleManifest, str, ContainerRegistryMap, dict[str, str]]:
    """Generate a cohort submission plus a ``uri -> region`` oracle.

    Returns (manifest, reference_prefix, registry_map, region_oracle).
    """
    n_samples = draw(st.integers(min_value=1, max_value=4))
    sample_ids = draw(
        st.lists(
            st.from_regex(r"\A[A-Za-z][A-Za-z0-9_-]{0,15}\Z", fullmatch=True),
            min_size=n_samples,
            max_size=n_samples,
            unique=True,
        )
    )
    oracle: dict[str, str] = {}
    samples = []
    for sid in sample_ids:
        reads_uri, reads_region = draw(s3_uri_with_region())
        index_uri, index_region = draw(s3_uri_with_region())
        oracle[reads_uri] = reads_region
        oracle[index_uri] = index_region
        samples.append(
            SampleRecord(
                sample_id=sid,
                reads_uri=reads_uri,
                index_uri=index_uri,
                sex=draw(st.sampled_from(["M", "F", "U"])),
            )
        )

    reference_prefix, ref_region = draw(s3_uri_with_region())
    oracle[reference_prefix] = ref_region

    n_images = draw(st.integers(min_value=1, max_value=3))
    mappings = []
    for _ in range(n_images):
        src_uri, src_region = draw(ecr_uri_with_region())
        dst_uri, dst_region = draw(ecr_uri_with_region())
        oracle[src_uri] = src_region
        oracle[dst_uri] = dst_region
        mappings.append(ImageMapping(sourceImage=src_uri, destinationImage=dst_uri))
    registry_map = ContainerRegistryMap(registryMappings=[], imageMappings=mappings)

    manifest = SampleManifest(
        cohort_id=draw(st.from_regex(r"\A[A-Za-z][A-Za-z0-9_-]{0,15}\Z", fullmatch=True)),
        reference_build="GRCh38",
        samples=samples,
    )
    return manifest, reference_prefix, registry_map, oracle


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------


@given(submission=cohort_submission())
def test_property_06_preflight_soundness(submission) -> None:  # type: ignore[no-untyped-def]
    """Preflight accepts iff every artifact is in Target_Region."""

    manifest, reference_prefix, registry_map, oracle = submission

    def _s3_region(uri: str) -> str:
        return oracle.get(uri, "unknown")

    def _ecr_region(uri: str) -> str:
        return oracle.get(uri, "unknown")

    report = cross_region_preflight(
        manifest,
        reference_prefix,
        registry_map,
        s3_region_resolver=_s3_region,
        ecr_region_resolver=_ecr_region,
    )

    all_in_region = all(region == TARGET_REGION for region in oracle.values())

    if all_in_region:
        assert report.accepted is True
        assert report.offenders == ()
    else:
        assert report.accepted is False
        offender_uris = {o.uri for o in report.offenders}
        expected_offenders = {uri for uri, region in oracle.items() if region != TARGET_REGION}
        assert expected_offenders.issubset(offender_uris), (
            f"preflight missed offenders: {expected_offenders - offender_uris}"
        )
