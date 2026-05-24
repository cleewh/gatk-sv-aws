# Feature: gatk-sv-healthomics-migration, Property 4: Container registry map has no floating tags
"""Property 4 — Container registry map has no floating tags.

For any Container_Registry_Map produced by the builder, every
``sourceImage`` and every ``destinationImage`` in the map SHALL be pinned
to either an immutable tag (semver-shaped, date-shaped, or sha256-prefixed)
or a ``@sha256:<digest>`` reference; the map builder SHALL reject any
image reference ending in ``:latest`` or lacking an explicit tag.

See design §Correctness Properties → Property 4 and §Components.b.

**Validates: Requirements 3.5**

This test is RED until Task 3.2.1 implements ``canonicalize_image`` and
Task 3.2.2 implements ``build_registry_map``.
"""

from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from gatk_sv_aws.registry import (
    FloatingTagError,
    canonicalize_image,
)

# ---------------------------------------------------------------------------
# Strategies — image reference components
# ---------------------------------------------------------------------------

_registry = st.sampled_from(
    [
        "quay.io",
        "registry-1.docker.io",
        "docker.io",
        "us.gcr.io",
        "gcr.io",
        "public.ecr.aws",
        "111111111111.dkr.ecr.ap-southeast-1.amazonaws.com",
    ]
)

_repository_segment = st.from_regex(
    r"\A[a-z0-9][a-z0-9_.-]{0,62}[a-z0-9]\Z", fullmatch=True
)
_repository = st.lists(_repository_segment, min_size=1, max_size=3).map("/".join)

_semver_tag = st.builds(
    lambda ma, mi, pa: f"{ma}.{mi}.{pa}",
    st.integers(min_value=0, max_value=99),
    st.integers(min_value=0, max_value=99),
    st.integers(min_value=0, max_value=99),
)
_date_tag = st.builds(
    lambda y, m, d: f"{y:04d}-{m:02d}-{d:02d}",
    st.integers(min_value=2020, max_value=2029),
    st.integers(min_value=1, max_value=12),
    st.integers(min_value=1, max_value=28),
)
_sha256_hex = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)

_good_tag = st.one_of(_semver_tag, _date_tag)
_good_digest = _sha256_hex.map(lambda h: f"sha256:{h}")


@st.composite
def good_image_reference(draw: st.DrawFn) -> str:
    """Strategy for image references with an immutable tag or digest."""
    registry = draw(_registry)
    repository = draw(_repository)
    use_digest = draw(st.booleans())
    if use_digest:
        return f"{registry}/{repository}@{draw(_good_digest)}"
    return f"{registry}/{repository}:{draw(_good_tag)}"


@st.composite
def bad_image_reference(draw: st.DrawFn) -> str:
    """Strategy for image references that must be rejected.

    Covers ``:latest``, bare repository (no registry + no tag), and
    reference with registry + repository but no tag or digest.
    """
    kind = draw(st.sampled_from(["latest", "bare", "no_tag"]))
    registry = draw(_registry)
    repository = draw(_repository)

    if kind == "latest":
        return f"{registry}/{repository}:latest"
    if kind == "bare":
        # bare means: no registry component and no tag
        return repository
    # no_tag: registry + repository without any tag or digest
    return f"{registry}/{repository}"


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------


@given(ref=good_image_reference())
def test_property_04a_canonicalize_accepts_immutable(ref: str) -> None:
    """canonicalize_image accepts immutable-tag or digest references."""
    canonical = canonicalize_image(ref)
    rendered = canonical.render()
    assert not rendered.endswith(":latest")
    # Either the original had a digest (render preserves it) or a tag (render preserves it).
    assert (":" in rendered.split("/")[-1]) or ("@sha256:" in rendered)


@given(ref=bad_image_reference())
def test_property_04b_canonicalize_rejects_floating(ref: str) -> None:
    """canonicalize_image rejects :latest, bare names, and missing tags."""
    with pytest.raises(FloatingTagError):
        canonicalize_image(ref)
