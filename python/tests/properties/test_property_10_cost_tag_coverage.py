# Feature: gatk-sv-healthomics-migration, Property 10: Cost-tag coverage
"""Property 10 — Cost-tag coverage.

For any cohort run submission with cohort_id ``C`` and workflow version
``V``, every AWS resource-creating API call issued by the Run Orchestrator
(StartAHORun, tag-on-create for S3 outputs, ECR tag inheritance via
repository template, CloudWatch Logs log group tags) SHALL carry both a
``gatk-sv:cohort-id = C`` tag and a ``gatk-sv:workflow-version = V`` tag.

See design §Correctness Properties → Property 10 and §Cost Optimization
Strategy → Cost Explorer tag taxonomy.

**Validates: Requirements 8.7, 16.4**

This test is RED until Task 3.7.5 implements ``apply_cost_tags``.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from gatk_sv_aws.orchestrator import (
    TagRecorder,
    apply_cost_tags,
)

_cohort_id = st.from_regex(r"\A[A-Za-z][A-Za-z0-9_-]{0,20}\Z", fullmatch=True)
_semver = st.builds(
    lambda a, b, c: f"{a}.{b}.{c}",
    st.integers(min_value=0, max_value=50),
    st.integers(min_value=0, max_value=50),
    st.integers(min_value=0, max_value=50),
)
_resource_kind = st.sampled_from(
    ["StartAHORun", "S3PutObject", "ECRRepositoryTemplate", "CloudWatchLogGroup"]
)
_arn = st.from_regex(r"\Aarn:aws:[a-z]{2,10}:ap-southeast-1:[0-9]{12}:[A-Za-z0-9_/-]{1,40}\Z", fullmatch=True)


@given(
    cohort_id=_cohort_id,
    workflow_version=_semver,
    events=st.lists(
        st.tuples(_resource_kind, _arn),
        min_size=1,
        max_size=6,
    ),
)
def test_property_10_cost_tag_coverage(
    cohort_id: str, workflow_version: str, events: list[tuple[str, str]]
) -> None:
    """Every recorded resource carries cohort-id and workflow-version tags."""

    recorder = TagRecorder()
    for resource_kind, arn in events:
        apply_cost_tags(
            recorder,
            resource_kind,
            arn,
            cohort_id=cohort_id,
            workflow_version=workflow_version,
        )

    assert len(recorder.applied) == len(events)
    for _kind, _arn, tags in recorder.applied:
        assert tags.get("gatk-sv:cohort-id") == cohort_id
        assert tags.get("gatk-sv:workflow-version") == workflow_version
