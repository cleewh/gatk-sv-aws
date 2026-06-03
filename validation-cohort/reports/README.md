# Validation cohort run reports

This directory holds **per-cohort run reports** for the GATK-SV
HealthOmics validation harness. Each subdirectory captures the full
auditable record of one validation cohort run: cost-report,
concordance-report, and the run-finished events emitted by
[`gatk_sv_aws.monitoring.emit_run_finished`](../../python/src/gatk_sv_aws/monitoring/__init__.py).

Task 6.5 in
[`.kiro/specs/gatk-sv-healthomics-migration/tasks.md`](../../.kiro/specs/gatk-sv-healthomics-migration/tasks.md)
declares this directory as the destination for the validation cohort
checkpoint; tasks 6.3 and 6.4 (the two acceptance tests under
[`python/tests/acceptance/`](../../python/tests/acceptance/)) read from
the most-recent subdirectory here.

## Directory naming convention

```
validation-cohort/reports/<YYYY-MM-DD-cohort-id-or-purpose>/
```

The acceptance test
[`test_per_sample_cost.py`](../../python/tests/acceptance/test_per_sample_cost.py)
selects the most-recent subdirectory by lexical sort, so prefixing each
folder with `YYYY-MM-DD` keeps the latest report at the end of the
sorted list.

Examples:

| Subdirectory | Purpose |
| --- | --- |
| `2026-05-26-checkpoint-scaffold/` | **SCAFFOLD** — schema-only placeholder committed at the time of the Task 6.5 checkpoint. No real cohort run has executed against it yet. |
| `2026-05-26-amendment-smoke/` | Real run report from the Task 8.10 10-sample smoke test of the v1.0 amendment pipeline (created when 8.10 executes). |
| `2026-05-26-validation-2026q2/` | Real run report from a full 10-sample `gatk-sv-validation-2026q2` cohort (created when the validation cohort runs). |

## Files in each report directory

Every report subdirectory MUST contain these three artifacts:

| File | Schema source | Produced by |
| --- | --- | --- |
| `cost-report.json` | Design §Data Models → "Cost Report" | `gatk_sv_aws.cost.optimizer` (Task 3.8) → `validation_cost_report` (Task 3.10.5) |
| `concordance-report.json` | Derived from `gatk_sv_aws.validation.ConcordanceReport` (Task 3.10.1) | `compare_cohort_vcf` and `compare_cohort_vcf_fuzzy` (Tasks 3.10.1, 3.10.3) |
| `run-finished-events.jsonl` | `gatk_sv_aws.monitoring.emit_run_finished` (Task 3.9.1) | One JSON-line per completed module run; the orchestrator's run-finished hook appends to this file. |

A `README.md` sibling is recommended whenever the report represents a
non-trivial situation (e.g. scaffold, partial run, retry-heavy run) so
the next reader doesn't have to reverse-engineer the run from the JSON.

## Acceptance gates each report MUST satisfy when `status = "REAL"`

When `cost-report.json` and `concordance-report.json` carry
`"status": "REAL"` (i.e. they were produced by an actual cohort run, not
a scaffold), the two acceptance tests pass against the report:

- **Concordance** ([Req 13.2, 13.3](../../.kiro/specs/gatk-sv-healthomics-migration/requirements.md)):
  per-SV-type concordance ≥ 99% for DEL/DUP and ≥ 95% for INS/INV.
- **Per-sample cost** ([Req 8.5, 13.4, 13.5](../../.kiro/specs/gatk-sv-healthomics-migration/requirements.md)):
  `per_sample_cost_usd ≤ target_usd` (default target: USD 7.00). On
  overage, `attribution[]` lists the `(module, dimension)` pairs and
  `recommendations[]` enumerates the Cost_Optimizer suggestions.

## Scaffold vs. real reports

A **scaffold** report (`status = "SCAFFOLD"`) is committed before any
real cohort has run. It exists to:

1. Pin the schema versions of `cost-report.json` and
   `concordance-report.json` so downstream tooling has something to lint
   against,
2. Document the directory layout convention for future operators,
3. Satisfy the Task 6.5 checkpoint requirement that the report
   destination be present and committed.

A **real** report (`status = "REAL"`) is produced by an actual cohort
run. The acceptance tests skip when no real report exists yet (their
default behavior is to skip unless `RUN_ACCEPTANCE_TESTS=1` is set; see
[`python/tests/acceptance/conftest.py`](../../python/tests/acceptance/conftest.py)),
so committing a scaffold first does not break the test suite.

## Current state

| Report | `status` | Notes |
| --- | --- | --- |
| `2026-05-26-checkpoint-scaffold/` | `SCAFFOLD` | Schema placeholder. The first **real** report is produced by Task 8.10 (the 10-sample smoke test of the amendment pipeline); see that subdirectory's `README.md` for what's still pending. |

The first real cohort run is gated on:

- The 10-sample expected Cohort_VCF being produced and uploaded (its
  metadata file at
  [`../expected/expected-metadata.json`](../expected/expected-metadata.json)
  is currently `status: "PENDING_FIRST_VALIDATION_RUN"`).
- Task 8.10 completing successfully — Phase 8 supersedes Section 6 in
  the actual flight order; see
  [`../../.kiro/specs/gatk-sv-healthomics-migration/tasks.md`](../../.kiro/specs/gatk-sv-healthomics-migration/tasks.md)
  Phase 8 for the smoke-test entry point.
