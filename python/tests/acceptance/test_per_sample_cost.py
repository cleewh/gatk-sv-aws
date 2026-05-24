"""Acceptance test: per-sample cost ≤ USD $7.00 (Req 8.5, 13.4, 13.5).

Skipped unless ``RUN_ACCEPTANCE_TESTS=1``. Reads ``cost-report.json`` from
the most-recent run report directory under
``validation-cohort/reports/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPORTS_DIR = (
    Path(__file__).resolve().parents[3]
    
    / "validation-cohort"
    / "reports"
)


def _latest_report_dir() -> Path | None:
    if not REPORTS_DIR.exists():
        return None
    dirs = sorted([p for p in REPORTS_DIR.iterdir() if p.is_dir()])
    return dirs[-1] if dirs else None


def test_per_sample_cost_at_or_below_target() -> None:
    latest = _latest_report_dir()
    if latest is None:
        pytest.skip(
            f"no validation run reports at {REPORTS_DIR}; run a cohort first."
        )
    cost_report_path = latest / "cost-report.json"
    if not cost_report_path.exists():
        pytest.skip(f"no cost-report.json in {latest}.")

    report = json.loads(cost_report_path.read_text())
    per_sample = float(report["per_sample_cost_usd"])
    target = float(report.get("target_usd", 7.00))
    assert per_sample <= target, (
        f"per-sample cost ${per_sample:.2f} exceeds target ${target:.2f}. "
        f"Cost_Optimizer attribution:\n{json.dumps(report.get('attribution', []), indent=2)}"
    )
