# Task 6.5 Checkpoint — SCAFFOLD report

This directory is a **schema-only scaffold** committed to satisfy the
Task 6.5 checkpoint declared in
[`.kiro/specs/gatk-sv-healthomics-migration/tasks.md`](../../../.kiro/specs/gatk-sv-healthomics-migration/tasks.md).
**No real cohort has run against it yet.** The two acceptance tests
([Task 6.3](../../../python/tests/acceptance/test_validation_concordance.py)
and [Task 6.4](../../../python/tests/acceptance/test_per_sample_cost.py))
do not assert PASSED against this directory; they skip cleanly because
`status = "SCAFFOLD"` and `RUN_ACCEPTANCE_TESTS` is not set.

## Why a scaffold and not a real report?

Two preconditions for a real cohort run are still pending:

1. **Expected Cohort_VCF is not yet produced.** See
   [`../../expected/expected-metadata.json`](../../expected/expected-metadata.json),
   which currently records
   `"status": "PENDING_FIRST_VALIDATION_RUN"`. The expected VCF must be
   produced upstream on Terra/Cromwell-on-GCP per
   [`docs/validation-runbook.md`](../../../docs/validation-runbook.md)
   Step 1 before the strict and fuzzy concordance comparisons in
   `compare_cohort_vcf`/`compare_cohort_vcf_fuzzy` can run.
2. **Phase 8 supersedes Section 6 in the flight order.** Per
   [`tasks.md`](../../../.kiro/specs/gatk-sv-healthomics-migration/tasks.md)
   Phase 8 (the v1.0 pipeline completeness amendment), the actual
   end-to-end smoke test is **Task 8.10** — a 10-sample run against
   `validation-cohort/inputs/manifest-gatk-sv-156.json`. The user has
   explicitly directed work to flow through Phase 8 first; the
   instruction "DO NOT run the 156 samples until everything is done"
   gates the full 156-sample cohort behind Task 8.11.

Therefore this checkpoint is a **scaffolded** checkpoint: it commits
the report-directory schema and the destination layout. The real
PASSED status of Task 6.5 becomes visible only after Task 8.10
produces its real run report at
`validation-cohort/reports/2026-05-26-amendment-smoke/`. At that
point, the Task 8.10 report's `cost-report.json` and
`concordance-report.json` carry `"status": "REAL"` and the two
acceptance tests assert PASSED against them when invoked with
`RUN_ACCEPTANCE_TESTS=1`.

## Files in this directory

| File | Status | Purpose |
| --- | --- | --- |
| `cost-report.json` | `SCAFFOLD` | Schema placeholder for the per-sample cost report consumed by [`test_per_sample_cost.py`](../../../python/tests/acceptance/test_per_sample_cost.py). Schema source: Design §Data Models → "Cost Report"; field semantics from `gatk_sv_aws.validation.ValidationCostReport`. |
| `concordance-report.json` | `SCAFFOLD` | Schema placeholder for the per-SV-type concordance report. Schema source: `gatk_sv_aws.validation.ConcordanceReport` (Task 3.10.1). Both strict and fuzzy join blocks are present so downstream tooling can lint against either gate. |
| `run-finished-events.jsonl` | `SCAFFOLD` | One illustrative `run_finished` event line documenting the schema emitted by [`gatk_sv_aws.monitoring.emit_run_finished`](../../../python/src/gatk_sv_aws/monitoring/__init__.py). The real file accumulates one line per module run (10 lines for a v1.0 cohort, 19 lines for the v1.0 amendment cohort). |
| `README.md` | — | This file. |

All three JSON/JSONL artifacts carry a top-level `"status": "SCAFFOLD"`
so any downstream test, lint, or audit step can distinguish placeholder
data from real measurements without parsing the values.

## Schema versions

| Artifact | `schema_version` |
| --- | --- |
| `cost-report.json` | `1.0.0` |
| `concordance-report.json` | `1.0.0` |
| `run-finished-events.jsonl` | each event carries `schema_version = "1.0.0"` (single per-line) |

When the schema evolves (e.g. a new tag key is added to Property 10, or
the GQ_Recalibrator chain forces a new per-phase concordance breakdown),
bump the `schema_version` in lockstep across the three artifacts and
update the readers in `python/tests/acceptance/`.

## What gets replaced when a real run report is committed

Every `null` and every `[]` in `cost-report.json` and
`concordance-report.json`. Every `"status": "SCAFFOLD"` flips to
`"status": "REAL"`. The `run-finished-events.jsonl` file stops carrying
the illustrative event and is rewritten with one real event per module
run (status, wall-clock seconds, and measured cost), in run-completion
order.
