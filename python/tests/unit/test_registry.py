"""Unit tests for the Container Registry Map Builder (§Components.b).

These complement the Hypothesis property tests in
``tests/gatk_sv_aws/properties/test_property_04_no_floating_tags.py``
and ``test_property_05_registry_closure.py`` with specific, human-readable
examples and edge cases.

Advances Requirements 3.1, 3.5.
"""

from __future__ import annotations

import pytest

from gatk_sv_aws.models import (
    ContainerRegistryMap,
)
from gatk_sv_aws.registry import (
    CanonicalImage,
    FloatingTagError,
    build_registry_map,
    canonicalize_image,
)


# ---------------------------------------------------------------------------
# canonicalize_image: accepted references
# ---------------------------------------------------------------------------


def test_canonicalize_semver_tag() -> None:
    """A semver tag on a two-segment registry/repo is accepted verbatim."""

    canonical = canonicalize_image("quay.io/biocontainers/samtools:1.17")

    assert canonical == CanonicalImage(
        registry="quay.io",
        repository="biocontainers/samtools",
        tag="1.17",
        digest=None,
    )
    assert canonical.render() == "quay.io/biocontainers/samtools:1.17"


def test_canonicalize_date_tag() -> None:
    """A date-style tag is accepted."""

    canonical = canonicalize_image(
        "us.gcr.io/broad-dsde-methods/gatk-sv/sv-pipeline:2024-09-01"
    )

    assert canonical.registry == "us.gcr.io"
    assert canonical.repository == "broad-dsde-methods/gatk-sv/sv-pipeline"
    assert canonical.tag == "2024-09-01"
    assert canonical.digest is None


def test_canonicalize_sha256_digest() -> None:
    """A sha256 digest is accepted and sets digest (not tag)."""

    digest_hex = "a" * 64
    ref = f"registry-1.docker.io/library/ubuntu@sha256:{digest_hex}"

    canonical = canonicalize_image(ref)

    assert canonical.registry == "registry-1.docker.io"
    assert canonical.repository == "library/ubuntu"
    assert canonical.tag is None
    assert canonical.digest == f"sha256:{digest_hex}"
    assert canonical.render() == ref


def test_canonicalize_multi_segment_repository() -> None:
    """A multi-segment repository preserves all but the first ``/`` segment."""

    canonical = canonicalize_image(
        "quay.io/biocontainers/samtools:1.17"
    )

    assert canonical.registry == "quay.io"
    assert canonical.repository == "biocontainers/samtools"


def test_canonicalize_ecr_private_host() -> None:
    """An in-account ECR URI is parsed into registry + repo + tag."""

    canonical = canonicalize_image(
        "111111111111.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/sv-pipeline:v1.2.3"
    )

    assert canonical.registry == (
        "111111111111.dkr.ecr.ap-southeast-1.amazonaws.com"
    )
    assert canonical.repository == "gatk-sv/sv-pipeline"
    assert canonical.tag == "v1.2.3"


# ---------------------------------------------------------------------------
# canonicalize_image: rejected references
# ---------------------------------------------------------------------------


def test_canonicalize_rejects_latest() -> None:
    """``:latest`` is a floating tag and always rejected."""

    with pytest.raises(FloatingTagError, match=":latest"):
        canonicalize_image("quay.io/biocontainers/samtools:latest")


def test_canonicalize_rejects_bare_name() -> None:
    """A bare shorthand like ``ubuntu`` has no registry and no tag."""

    with pytest.raises(FloatingTagError, match="bare image reference"):
        canonicalize_image("ubuntu")


def test_canonicalize_rejects_bare_name_with_tag() -> None:
    """Even with a tag, a bare reference (no registry slash) is rejected."""

    # No '/' means no registry segment — the reference is ambiguous about
    # whether the tag colon is a port or a tag, and HealthOmics requires
    # an explicit registry host.
    with pytest.raises(FloatingTagError, match="bare image reference"):
        canonicalize_image("ubuntu:22.04")


def test_canonicalize_rejects_missing_tag() -> None:
    """``registry/repo`` with neither tag nor digest is rejected."""

    with pytest.raises(FloatingTagError, match="no tag or digest"):
        canonicalize_image("quay.io/biocontainers/samtools")


def test_canonicalize_rejects_non_sha256_digest() -> None:
    """Only ``@sha256:`` digests are supported."""

    with pytest.raises(FloatingTagError, match="non-sha256 digest"):
        canonicalize_image("quay.io/foo/bar@md5:deadbeef")


def test_canonicalize_rejects_empty_tag() -> None:
    """``registry/repo:`` with an empty tag is rejected."""

    with pytest.raises(FloatingTagError):
        canonicalize_image("quay.io/foo/bar:")


# ---------------------------------------------------------------------------
# build_registry_map
# ---------------------------------------------------------------------------


def test_build_registry_map_empty_bundles_emits_defaults() -> None:
    """With no bundles, the map still covers every expected upstream host."""

    registry_map = build_registry_map([])

    assert isinstance(registry_map, ContainerRegistryMap)
    assert registry_map.imageMappings == []

    upstream_hosts = {rm.upstreamRegistryUrl for rm in registry_map.registryMappings}
    assert {
        "quay.io",
        "registry-1.docker.io",
        "docker.io",
        "us.gcr.io",
        "gcr.io",
        "public.ecr.aws",
        "111111111111.dkr.ecr.ap-southeast-1.amazonaws.com",
    } <= upstream_hosts


def test_build_registry_map_covers_quay_reference() -> None:
    """Any quay.io reference resolves via the quay.io registry mapping."""

    registry_map = build_registry_map([])
    ref = "quay.io/biocontainers/samtools:1.17"

    assert any(
        ref.startswith(rm.upstreamRegistryUrl)
        for rm in registry_map.registryMappings
    )


def test_build_registry_map_covers_dockerhub_digest_reference() -> None:
    """A digest-form Docker Hub reference resolves via prefix match."""

    registry_map = build_registry_map([])
    ref = (
        "registry-1.docker.io/library/ubuntu@sha256:"
        + "b" * 64
    )

    assert any(
        ref.startswith(rm.upstreamRegistryUrl)
        for rm in registry_map.registryMappings
    )


def test_build_registry_map_covers_ecr_private_reference() -> None:
    """The emitted map also carries an entry for the in-account ECR host."""

    registry_map = build_registry_map([])
    ref = (
        "111111111111.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/sv-pipeline:v1.2.3"
    )

    assert any(
        ref.startswith(rm.upstreamRegistryUrl)
        for rm in registry_map.registryMappings
    )


def test_build_registry_map_entries_have_no_upstream_prefix() -> None:
    """Default mappings mirror the whole host; they do not narrow by prefix.

    This keeps Property 5 closure simple: every reference with a matching
    host resolves regardless of its repository path.
    """

    registry_map = build_registry_map([])

    for rm in registry_map.registryMappings:
        assert rm.upstreamRepositoryPrefix is None
        # A stable, human-readable ecrRepositoryPrefix is required by the
        # HealthOmics schema; make sure we emit one.
        assert rm.ecrRepositoryPrefix
