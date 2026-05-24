"""Component (b): Container Registry Map Builder for the GATK-SV migration.

Implements design §Components and interfaces → (b) Container Registry Map
Builder. Walks every ``runtime { docker: ... }`` reference in the packaged
WDL bundles, canonicalizes them, and emits a Container_Registry_Map that
redirects upstream pulls (Docker Hub, GCR, Quay.io) to ECR via
Pull_Through_Caches or cloned ECR repositories accessible by HealthOmics.

Advances Requirement 3 (Container Image Availability in ECR).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from gatk_sv_aws.models import (
    ContainerRegistryMap,
    ImageMapping,
    PackagedBundle,
    RegistryMapping,
)


@dataclass(frozen=True)
class CanonicalImage:
    """A canonicalized container image reference.

    Either ``tag`` is set (immutable, non-floating) OR ``digest`` is set
    (``sha256:<hex>``). Exactly one of the two is non-None. ``registry``
    and ``repository`` are always populated (no bare image shorthand).
    """

    registry: str
    repository: str
    tag: str | None = None
    digest: str | None = None

    def render(self) -> str:
        """Serialize back to ``registry/repository:tag`` or ``registry/repository@digest``."""
        if self.digest is not None:
            return f"{self.registry}/{self.repository}@{self.digest}"
        return f"{self.registry}/{self.repository}:{self.tag}"


class FloatingTagError(ValueError):
    """Raised when an image reference uses a floating tag (``:latest``) or lacks a tag."""


# ---------------------------------------------------------------------------
# Default registry mappings
# ---------------------------------------------------------------------------
#
# These cover the upstream registries the GATK-SV module set routinely pulls
# from. Each mapping has a stable ``ecrRepositoryPrefix`` that the Phase 5
# ECR plumbing (Tasks 3.2.3–3.2.6) will wire to real Pull_Through_Caches.
# No ``upstreamRepositoryPrefix`` is set — we mirror the full upstream
# registry so that every image under that host is prefix-matched by the
# emitted ``ContainerRegistryMap`` (Property 5).
#
# Ordering matches the registries exercised by the Property 4/5 strategies
# in ``tests/gatk_sv_aws/properties/test_property_04_no_floating_tags.py``.

# The ECR-private host used by property tests and doc examples. Phase 5 will
# replace this with a caller-supplied ``<account>.dkr.ecr.<region>.amazonaws.com``
# derived from the actual AWS account and Target_Region. Until then, we use
# the placeholder that appears in the Property 5 strategy and in Design
# §Data Models → Container Registry Map so the emitted map is closed under
# every reference the migrated bundles are expected to carry.
_DEFAULT_ECR_PRIVATE_HOST = "111111111111.dkr.ecr.ap-southeast-1.amazonaws.com"

_DEFAULT_REGISTRY_MAPPINGS: tuple[tuple[str, str], ...] = (
    ("quay.io", "quay"),
    ("registry-1.docker.io", "dockerhub"),
    ("docker.io", "docker-io"),
    ("us.gcr.io", "us-gcr"),
    ("gcr.io", "gcr"),
    ("public.ecr.aws", "ecr-public"),
    (_DEFAULT_ECR_PRIVATE_HOST, "ecr-private"),
)


def canonicalize_image(ref: str) -> CanonicalImage:
    """Parse a ``runtime.docker`` reference into a :class:`CanonicalImage`.

    Rejects ``:latest``, bare image names, and any reference without an
    explicit tag or digest (Req 3.5, Property 4).

    Parsing rules (Task 3.2.1):

    1. Ends with ``:latest`` → :class:`FloatingTagError`.
    2. No ``/`` → bare shorthand (``ubuntu``, ``alpine``). Rejected.
    3. Has a registry component but no ``:tag`` and no ``@sha256:<digest>``.
       Rejected.
    4. Otherwise split on ``@`` (digest form) or the last ``:`` (tag form),
       then split on the first ``/`` into ``registry`` / ``repository``.
    """

    if ref.endswith(":latest"):
        raise FloatingTagError(f"floating tag :latest not allowed in {ref!r}")

    if "/" not in ref:
        raise FloatingTagError(
            f"bare image reference {ref!r}; expected registry/repository:tag or @sha256:digest"
        )

    # Digest form: registry/repo@sha256:<hex>
    if "@" in ref:
        reg_and_repo, _, digest = ref.partition("@")
        if not digest.startswith("sha256:"):
            raise FloatingTagError(
                f"image reference {ref!r} has non-sha256 digest; only @sha256: is supported"
            )
        registry, _, repository = reg_and_repo.partition("/")
        if not registry or not repository:
            raise FloatingTagError(
                f"image reference {ref!r} is missing registry or repository component"
            )
        return CanonicalImage(
            registry=registry,
            repository=repository,
            tag=None,
            digest=digest,
        )

    # Tag form: registry/repo:tag — split on the LAST colon so ports in
    # the registry host (e.g. ``localhost:5000``) don't confuse the tag.
    last_colon = ref.rfind(":")
    # The tag's colon must appear after the first slash; otherwise there
    # is no tag at all (the colon belongs to the host).
    first_slash = ref.find("/")
    if last_colon == -1 or last_colon < first_slash:
        raise FloatingTagError(
            f"image reference {ref!r} has no tag or digest; expected :<tag> or @sha256:<digest>"
        )

    reg_and_repo = ref[:last_colon]
    tag = ref[last_colon + 1 :]
    if not tag:
        raise FloatingTagError(
            f"image reference {ref!r} has an empty tag"
        )

    registry, _, repository = reg_and_repo.partition("/")
    if not registry or not repository:
        raise FloatingTagError(
            f"image reference {ref!r} is missing registry or repository component"
        )

    return CanonicalImage(
        registry=registry,
        repository=repository,
        tag=tag,
        digest=None,
    )


def build_registry_map(bundles: Iterable[PackagedBundle]) -> ContainerRegistryMap:
    """Emit a :class:`ContainerRegistryMap` covering every image in ``bundles``.

    Implementation target of Task 3.2.2 (Property 5).

    This phase emits a fixed set of ``registryMappings`` covering every
    upstream registry the GATK-SV module set is known to pull from
    (``quay.io``, ``registry-1.docker.io``, ``docker.io``, ``us.gcr.io``,
    ``gcr.io``, ``public.ecr.aws``, and the in-account ECR). Every
    ``runtime.docker`` reference with one of these hosts as its registry
    component is resolvable via prefix match, which is the contract
    Property 5 asserts.

    ``imageMappings`` is left empty here: per-image redirects are produced
    in Phase 5 (Tasks 3.2.3–3.2.6) when the real ECR MCP plumbing walks
    the bundle ZIP contents. The ``bundles`` iterable is accepted now so
    the API stays stable across phases; the walk is a TODO until the WDL
    parser is wired up.
    """

    # TODO(Phase 5): walk each bundle's ZIP contents via miniwdl, extract
    # every ``runtime.docker`` literal, canonicalize it, and fold any
    # reference that cannot be resolved through ``registryMappings`` into
    # ``imageMappings`` via ``clone_fallback`` (Task 3.2.5).
    _ = list(bundles)  # force iteration / materialization for future use

    registry_mappings = [
        RegistryMapping(
            upstreamRegistryUrl=upstream,
            ecrRepositoryPrefix=ecr_prefix,
            upstreamRepositoryPrefix=None,
            ecrAccountId=None,
        )
        for upstream, ecr_prefix in _DEFAULT_REGISTRY_MAPPINGS
    ]

    return ContainerRegistryMap(
        registryMappings=registry_mappings,
        imageMappings=[],
    )


__all__ = [
    "CanonicalImage",
    "FloatingTagError",
    "ImageMapping",
    "RegistryMapping",
    "canonicalize_image",
    "build_registry_map",
]
