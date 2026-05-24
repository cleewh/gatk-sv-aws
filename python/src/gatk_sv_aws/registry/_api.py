"""Public API stubs for component (b) — Container Registry Map Builder.

These stubs raise ``NotImplementedError`` so the Task 2.x property-based
tests collect cleanly while remaining RED. Real implementations arrive in
Task 3.2 (canonicalization, map construction, resolution check).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from gatk_sv_aws.models import ContainerRegistryMap


class CanonicalImage(BaseModel):
    """Canonical (registry, repository, tag-or-digest) decomposition of a container reference.

    Produced by :func:`canonicalize_image` (implementation target of
    Property 4; Req 3.5). ``tag_or_digest`` is guaranteed immutable — never
    ``"latest"`` and never empty — by the canonicalizer.
    """

    model_config = ConfigDict(extra="forbid")

    registry: str = Field(
        ...,
        min_length=1,
        description="Host portion of the upstream reference (e.g., ``quay.io``, ``registry-1.docker.io``).",
    )
    repository: str = Field(
        ...,
        min_length=1,
        description="Repository path component (e.g., ``biocontainers/samtools``).",
    )
    tag_or_digest: str = Field(
        ...,
        min_length=1,
        description="Immutable tag or ``sha256:<digest>`` — never ``latest`` / empty (Req 3.5).",
    )


def canonicalize_image(ref: str) -> CanonicalImage:
    """Parse and canonicalize a container image reference (stub).

    Implemented by Task 3.2.1. Raises ``NotImplementedError`` so the
    property-based tests in Task 2 fail RED until the real implementation
    lands.
    """

    raise NotImplementedError(
        "TODO: registry.canonicalize_image implemented by Task 3.2.1"
    )


def build_registry_map(bundles: Any) -> ContainerRegistryMap:
    """Build a Container_Registry_Map from packaged bundles (stub).

    Implemented by Task 3.2.2. Raises ``NotImplementedError`` so the
    property-based tests in Task 2 fail RED until the real implementation
    lands.
    """

    raise NotImplementedError(
        "TODO: registry.build_registry_map implemented by Task 3.2.2"
    )


def registry_map_resolves(
    registry_map: ContainerRegistryMap, image_ref: str
) -> bool:
    """Decide whether ``registry_map`` resolves ``image_ref`` (stub).

    Implemented by Task 3.2.2. Raises ``NotImplementedError`` so the
    property-based tests in Task 2 fail RED until the real implementation
    lands.
    """

    raise NotImplementedError(
        "TODO: registry.registry_map_resolves implemented by Task 3.2.2"
    )


__all__ = [
    "CanonicalImage",
    "canonicalize_image",
    "build_registry_map",
    "registry_map_resolves",
]
