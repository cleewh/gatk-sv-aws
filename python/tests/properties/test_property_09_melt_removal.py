# Feature: gatk-sv-healthomics-migration, Property 9: MELT removal
"""Property 9 — MELT removal.

For any migrated WDL bundle produced by the packager, no ``task``
declaration, no ``call`` statement, no ``runtime.docker`` value, and no
input-file path SHALL contain the substring ``MELT`` or ``melt``
(case-insensitive match on the token); and for any upstream bundle
containing N MELT-referencing tasks, the divergence log for the packaged
output SHALL contain N entries whose ``change_kind`` is ``remove_caller``
and whose ``reason`` references MELT.

See design §Correctness Properties → Property 9 and §Components.a.

**Validates: Requirements 2a.3, 2a.4**

This test is RED until Task 3.1.2 implements ``strip_melt``.
"""

from __future__ import annotations

import re

from hypothesis import given, strategies as st

from gatk_sv_aws.models import ChangeKind
from gatk_sv_aws.packager import WdlTree, strip_melt

_MELT_TOKEN = re.compile(r"\bmelt\b", re.IGNORECASE)

_ident_nonmelt = st.from_regex(r"\A[A-Za-z_][A-Za-z0-9_]{0,15}\Z", fullmatch=True).filter(
    lambda s: not _MELT_TOKEN.search(s)
)
_ident_melt = st.sampled_from(
    ["MELT", "melt", "RunMELT", "melt_task", "MeltCaller", "call_melt"]
)


@st.composite
def wdl_tree_with_melt(draw: st.DrawFn) -> tuple[WdlTree, int]:
    """Generate a WdlTree with a known count of MELT-referencing items.

    Returns (tree, expected_removal_count).
    """
    n_melt_tasks = draw(st.integers(min_value=0, max_value=3))
    n_melt_calls = draw(st.integers(min_value=0, max_value=3))
    n_melt_docker = draw(st.integers(min_value=0, max_value=2))
    n_melt_inputs = draw(st.integers(min_value=0, max_value=2))

    clean_tasks = draw(st.lists(_ident_nonmelt, min_size=1, max_size=4, unique=True))
    melt_tasks = [draw(_ident_melt) for _ in range(n_melt_tasks)]

    clean_calls = draw(st.lists(_ident_nonmelt, min_size=1, max_size=4, unique=True))
    melt_calls = [f"call_{draw(_ident_melt)}" for _ in range(n_melt_calls)]

    clean_docker = ["quay.io/biocontainers/samtools:1.17", "broadinstitute/gatk:4.5.0.0"]
    melt_docker = [
        f"us.gcr.io/broad-dsde-methods/melt:{i}.0" for i in range(n_melt_docker)
    ]

    clean_inputs = ["ref.fa", "sample.cram"]
    melt_inputs = [f"MELT_ref_{i}.bed" for i in range(n_melt_inputs)]

    tree = WdlTree(
        source="(synthetic)",
        tasks=clean_tasks + melt_tasks,
        calls=clean_calls + melt_calls,
        docker_refs=clean_docker + melt_docker,
        input_paths=clean_inputs + melt_inputs,
    )
    return tree, n_melt_tasks + n_melt_calls + n_melt_docker + n_melt_inputs


@given(data=wdl_tree_with_melt())
def test_property_09_melt_removal(data) -> None:  # type: ignore[no-untyped-def]
    tree, expected_removals = data

    stripped, divergences = strip_melt(tree)

    for field_name in ("tasks", "calls", "docker_refs", "input_paths"):
        for item in getattr(stripped, field_name):
            assert not _MELT_TOKEN.search(item), (
                f"MELT token leaked through in {field_name}: {item!r}"
            )

    melt_divergences = [
        d for d in divergences
        if d.change_kind == ChangeKind.REMOVE_CALLER and "melt" in d.reason.lower()
    ]
    assert len(melt_divergences) == expected_removals, (
        f"expected {expected_removals} MELT removal divergences, got {len(melt_divergences)}"
    )
