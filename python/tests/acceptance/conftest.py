"""Acceptance-test skip guard.

Tests under this directory exercise the full cohort run result. They
require a completed cohort run whose artifacts are committed under
``validation-cohort/``. They skip by default.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

COHORT_ROOT = (
    Path(__file__).resolve().parents[3]
    
    / "validation-cohort"
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if os.environ.get("RUN_ACCEPTANCE_TESTS", "").lower() in {"1", "true", "yes"}:
        return
    acceptance_dir = Path(__file__).parent
    skip_marker = pytest.mark.skip(
        reason="Acceptance tests skip unless RUN_ACCEPTANCE_TESTS=1"
    )
    for item in items:
        item_path = Path(str(item.fspath)).resolve()
        try:
            item_path.relative_to(acceptance_dir)
        except ValueError:
            continue  # not in our directory; leave alone
        item.add_marker(skip_marker)
