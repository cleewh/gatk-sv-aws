"""Public API stubs for component (h) — Cost Optimizer.

These stubs raise ``NotImplementedError`` so the Task 2.x property-based
tests collect cleanly while remaining RED. Real implementation arrives in
Task 3.8.2 (``recommend``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Recommendation(BaseModel):
    """Single CPU / memory right-sizing recommendation (Req 9.2, 9.3).

    Produced by :func:`recommend` after a task has been observed in at
    least three cohort-scale runs. Recommendations that reduce CPU or
    memory by ≥25% have ``surface == True`` so the operator sees them
    immediately (Req 9.3).
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the WDL task this recommendation applies to.",
    )
    current_cpu: int = Field(
        ...,
        ge=1,
        description="Currently declared CPU count.",
    )
    current_memory_gib: int = Field(
        ...,
        ge=1,
        description="Currently declared memory allocation in GiB.",
    )
    recommended_cpu: int = Field(
        ...,
        ge=1,
        description="Recommended CPU count = ceil(max(observed_peak) * (1+headroom)) snapped to tier.",
    )
    recommended_memory_gib: int = Field(
        ...,
        ge=1,
        description="Recommended memory in GiB = ceil(max(observed_peak) * (1+headroom)) snapped to tier.",
    )
    reduction_pct: float = Field(
        ...,
        description="Largest reduction percent (CPU or memory) relative to current.",
    )
    surface: bool = Field(
        ...,
        description="True iff the reduction ≥25% threshold is met (Req 9.3).",
    )


def recommend(
    task_stats: list[Any], headroom: float = 0.20
) -> list[Recommendation]:
    """Produce right-sizing recommendations from observed task stats (stub).

    Implemented by Task 3.8.2. Raises ``NotImplementedError`` so the
    property-based tests in Task 2 fail RED until the real implementation
    lands.
    """

    raise NotImplementedError(
        "TODO: cost.recommend implemented by Task 3.8.2"
    )


__all__ = [
    "Recommendation",
    "recommend",
]
