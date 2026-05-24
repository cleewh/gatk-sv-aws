# Feature: gatk-sv-healthomics-migration, Property 5: Container registry map is closed under migrated WDL references
"""Property 5 — Container registry map is closed under migrated WDL references.

For any set of packaged migrated WDL bundles ``B`` and the
Container_Registry_Map ``M`` produced for ``B``, every
``runtime { docker: ... }`` reference appearing in any task in any bundle
in ``B`` SHALL be resolvable through ``M`` — either via an
``imageMappings`` entry whose ``sourceImage`` equals the reference, or via
a ``registryMappings`` entry whose ``upstreamRegistryUrl`` /
``upstreamRepositoryPrefix`` prefix-matches the reference.

See design §Correctness Properties → Property 5 and §Components.b.

**Validates: Requirements 3.1, 3.3, 3.4**

This test is RED until Task 3.2.1 implements ``canonicalize_image`` and
Task 3.2.2 implements ``build_registry_map``.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given, strategies as st

from gatk_sv_aws.models import (
    MIGRATED_MODULES,
    ModuleName,
    PackagedBundle,
)
from gatk_sv_aws.registry import build_registry_map

from tests.properties.test_property_04_no_floating_tags import (
    good_image_reference,
)


def _resolvable(ref: str, registry_map) -> bool:  # type: ignore[no-untyped-def]
    """True iff ``ref`` is covered by an imageMappings exact or a registryMappings prefix."""
    for im in registry_map.imageMappings:
        if im.sourceImage == ref:
            return True
    for rm in registry_map.registryMappings:
        upstream_prefix = rm.upstreamRegistryUrl
        if rm.upstreamRepositoryPrefix:
            upstream_prefix = f"{upstream_prefix}/{rm.upstreamRepositoryPrefix}"
        if ref.startswith(upstream_prefix):
            return True
    return False


_module = st.sampled_from(list(MIGRATED_MODULES))


@st.composite
def packaged_bundle_strategy(draw: st.DrawFn) -> PackagedBundle:
    """Generate a PackagedBundle with 1-6 image references recorded on the bundle.

    Since the real bundle ZIP contents are out of scope for this test, we
    attach the generated refs by abusing the ``divergence`` field's ``reason``
    string with a structured prefix. The real ``build_registry_map``
    (Task 3.2.2) will walk ZIP contents; for Property 5 we test the
    contract over a simpler in-memory bundle shape.
    """
    # Property 5 as written in the design describes closure over the set of
    # runtime.docker references. The Pydantic ``PackagedBundle`` doesn't carry
    # a refs-list field directly, so we pass refs in via a sidecar attribute
    # using ``model_copy`` with extras-forbid disabled. Simpler: attach a
    # separate refs list alongside the bundle in the strategy tuple.
    module: ModuleName = draw(_module)
    refs = draw(st.lists(good_image_reference(), min_size=1, max_size=6, unique=True))
    bundle = PackagedBundle(
        zip_path=Path(f"/tmp/{module}-bundle.zip"),
        main_wdl_path=f"{module}.wdl",
        module=module,
        upstream_commit="deadbeefcafebabe",
        divergence=[],
        lint_report=None,
    )
    # Smuggle refs via a tuple return — the Property 5 test expects them as a
    # second element. This keeps PackagedBundle pristine for type-checking.
    return (bundle, tuple(refs))  # type: ignore[return-value]


@given(
    bundles=st.lists(packaged_bundle_strategy(), min_size=1, max_size=4)
)
def test_property_05_registry_closure(bundles) -> None:  # type: ignore[no-untyped-def]
    """Every image reference in any bundle resolves through the emitted map."""

    pkg_bundles = [b for (b, _refs) in bundles]
    all_refs = [ref for (_b, refs) in bundles for ref in refs]

    # build_registry_map inspects the bundles' WDL contents; here we exercise
    # the API contract. The real impl (Task 3.2.2) will also consume an
    # explicit per-bundle refs list derived from WDL parsing.
    registry_map = build_registry_map(pkg_bundles)

    for ref in all_refs:
        assert _resolvable(ref, registry_map), (
            f"image reference {ref!r} not covered by the emitted ContainerRegistryMap"
        )
