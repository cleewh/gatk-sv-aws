# Implementation Plan: Step Functions Orchestrator

## Overview

Convert the Step Functions orchestrator design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Each task builds on the previous ones and ends with wiring things together, so no hanging or orphaned code is left un-integrated. Tasks focus only on writing, modifying, or testing code.

Implementation language is **Python 3.12**, consistent with the design's Lambda runtime specification, the existing `kiro-life-sciences/` Python package, and the CDK stack (aws-cdk-lib). Property-based tests use Hypothesis with ≥100 iterations. CDK assertions use `aws_cdk.assertions`. The orchestrator reuses existing logic from `kiro_life_sciences.gatk_sv_healthomics.orchestrator` (validate_manifest, classify_retry) and `kiro_life_sciences.gatk_sv_healthomics.models`.

## Tasks

- [x] 1. Project scaffolding and CDK app structure
  - [x] 1.1 Create the CDK app directory structure under `kiro-life-sciences/src/kiro_life_sciences/gatk_sv_healthomics/step_functions/` with `__init__.py`, `app.py` (CDK app entry point), `stack.py` (GatkSvOrchestratorStack), and a `lambdas/` subdirectory containing `__init__.py`, `validate_manifest.py`, `start_run.py`, `poll_status.py`, `gather_cost.py` handler stubs
    - Each Lambda stub should define a `handler(event, context)` function that returns a placeholder response
    - Create `cdk.json` at the CDK app root pointing to `app.py`
    - _Requirements: 8.1, 8.4, 8.5_

  - [x] 1.2 Add CDK dependencies to `kiro-life-sciences/pyproject.toml`: `aws-cdk-lib>=2.150.0`, `constructs>=10.0.0`, and dev dependencies `aws-cdk.assertions` (if not already present)
    - _Requirements: 8.1_

  - [x] 1.3 Create shared types and constants in `step_functions/constants.py`: module execution order tuple, retryable error codes frozenset, backoff parameters (base=30, factor=2, cap=480, max_attempts=3), default config values (region, role ARN, cache ID, output bucket)
    - _Requirements: 1.1, 5.1, 5.2, 6.1_

  - [x] 1.4 Create data models in `step_functions/models.py` for Lambda input/output contracts: `ManifestValidationInput`, `ManifestValidationOutput`, `StartRunInput`, `StartRunOutput`, `PollStatusInput`, `PollStatusOutput`, `GatherCostInput`, `GatherCostOutput`, `CostReportEntry`, `CostReport` — using Pydantic BaseModel with strict validation
    - _Requirements: 9.1, 9.2, 9.3_

- [x] 2. Core Lambda implementations
  - [x] 2.1 Implement `validate_manifest.py` handler
    - Parse input event into `ManifestValidationInput`
    - Reuse validation logic from existing `kiro_life_sciences.gatk_sv_healthomics.orchestrator.validate_manifest()` for duplicate IDs, format checks, and region checks
    - Support both inline manifest objects and S3 URI resolution (fetch from S3 if URI provided)
    - Return `ManifestValidationOutput` with validation_status, sample_count, errors list, and resolved manifest
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [ ]* 2.2 Write property tests for validate-manifest Lambda (Properties 1 and 2)
    - **Property 1: Manifest validation catches all invalid inputs**
    - **Property 2: Valid manifests pass validation**
    - Generate random manifests with/without duplicate IDs, out-of-region URIs, unsupported formats
    - Assert invalid manifests produce non-empty error lists identifying specific violations
    - Assert valid manifests produce empty error lists
    - **Validates: Requirements 2.2, 2.3, 2.4, 2.5**

  - [x] 2.3 Implement `start_run.py` handler
    - Parse input event into `StartRunInput`
    - Read `HEALTHOMICS_ROLE_ARN` and `CACHE_ID` from environment variables (never from input)
    - Build HealthOmics StartRun parameters: workflow_id, role_arn, output_uri, parameters, storageType=DYNAMIC, cacheId, cacheBehavior=CACHE_ALWAYS
    - Apply cost-tracking tags: `gatk-sv:cohort-id`, `gatk-sv:workflow-version`, `gatk-sv:module`, `gatk-sv:sample-count`
    - Call `omics_client.start_run()` and return `StartRunOutput` with run_id, arn, status, module, attempt_number
    - _Requirements: 6.1, 6.2, 7.3, 12.4_

  - [ ]* 2.4 Write property test for start-run Lambda (Property 5)
    - **Property 5: Run submission always includes cache and tags**
    - Generate random module names, cohort_ids, workflow_versions, sample_counts
    - Assert produced parameters always include cache_id, cache_behavior=CACHE_ALWAYS, and all four cost-tracking tags
    - **Validates: Requirements 6.1, 7.3**

  - [x] 2.5 Implement `poll_status.py` handler
    - Parse input event into `PollStatusInput`
    - Call `omics_client.get_run(id=run_id)` to retrieve current status
    - Extract status, output_uri, failure_reason, cache status from response
    - Compute duration_seconds from run start time
    - Determine `is_terminal` flag (COMPLETED, FAILED, CANCELLED are terminal)
    - On terminal states: emit CloudWatch custom metric, publish EventBridge event
    - Return `PollStatusOutput` with run_id, status, output_uri, is_terminal, is_cache_hit, failure_reason, error_code, duration_seconds
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 10.1, 10.2_

  - [ ]* 2.6 Write property test for poll-status retry classification (Property 3)
    - **Property 3: Retry classification correctness**
    - Generate random error codes and attempt numbers
    - Assert should_retry=True iff error_code ∈ {InternalServerError, Throttling, ServiceUnavailable} AND attempt_number < 3
    - **Validates: Requirements 5.1, 5.3**

  - [ ]* 2.7 Write property test for exponential backoff (Property 4)
    - **Property 4: Exponential backoff formula**
    - Generate attempt numbers in {1, 2, 3}
    - Assert delay == min(30 * 2^(attempt-1), 480) seconds
    - Assert delay never exceeds 480 seconds for any attempt number
    - **Validates: Requirements 5.2**

  - [x] 2.8 Implement `gather_cost.py` handler
    - Parse input event into `GatherCostInput`
    - For each module run, call `omics_client.get_run(id=run_id)` to retrieve billing metadata
    - Compute per-module cost, total cost, per-sample cost (total / sample_count)
    - Build cost report JSON with cohort_id, sample_count, total_cost_usd, per_sample_cost_usd, modules array, generated_at timestamp
    - Write cost-report.json to `{output_uri}/cost-report.json` via S3 PutObject
    - Return `GatherCostOutput` with full cost report and S3 URI
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

  - [ ]* 2.9 Write property test for cost report arithmetic (Property 6)
    - **Property 6: Cost report arithmetic**
    - Generate random non-empty lists of module costs and positive sample counts
    - Assert total_cost_usd == sum(module_costs) and per_sample_cost_usd == total_cost_usd / sample_count
    - Assert per_sample_cost_usd is always non-negative
    - **Validates: Requirements 7.2**

  - [ ]* 2.10 Write property test for cost report path construction (Property 7)
    - **Property 7: Cost report path construction**
    - Generate random S3 URIs with and without trailing slashes
    - Assert cost report key is `{output_uri}/cost-report.json` with exactly one slash separator
    - **Validates: Requirements 7.4**

- [x] 3. Checkpoint - Ensure all Lambda implementations and property tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. CDK stack implementation
  - [x] 4.1 Implement `GatkSvOrchestratorStack` in `stack.py` with constructor props for target_region, healthomics_role_arn, cache_id, output_bucket
    - Create the 4 Lambda functions with Python 3.12 runtime, 256 MB memory, 60s timeout
    - Set environment variables on each Lambda: HEALTHOMICS_ROLE_ARN, CACHE_ID, OUTPUT_BUCKET, TARGET_REGION
    - Create shared Lambda execution IAM role with scoped permissions (omics:StartRun, omics:GetRun, omics:ListRunTasks, s3:PutObject, s3:GetObject, s3:GetBucketLocation, logs:*, cloudwatch:PutMetricData, events:PutEvents)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 12.1, 12.3_

  - [x] 4.2 Define the Step Functions state machine ASL in `stack.py`
    - Create the state machine using `aws_cdk.aws_stepfunctions` constructs
    - Implement the ValidateManifest → Module chain (10 modules in order) → GatherCost flow
    - For each module: StartRun → Wait(60s) → PollStatus → Choice (COMPLETED→next, RUNNING→wait loop, FAILED→evaluate retry)
    - Implement GatherSampleEvidence special case: reindex first, then 5 parallel tasks (CollectCounts, CollectSVEvidence, Manta, Wham, Scramble)
    - Add Catch blocks on every Task state routing to HandleModuleFailure
    - Configure retry: MaxAttempts=3, IntervalSeconds=30, BackoffRate=2, MaxDelaySeconds=480
    - Set state machine type to STANDARD
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 5.1, 5.2, 11.4_

  - [x] 4.3 Implement IAM roles in the CDK stack
    - Create state machine execution role with lambda:InvokeFunction on the 4 Lambda ARNs and logs permissions
    - Scope Lambda execution role to specific resource ARNs (no wildcards)
    - Ensure the Lambda does NOT have sts:AssumeRole for the HealthOmics run role — it passes role_arn as a parameter to StartRun
    - _Requirements: 12.1, 12.2, 12.3, 12.4_

  - [x] 4.4 Implement CloudWatch dashboard in the CDK stack
    - Create dashboard with widgets for: pipeline execution status, module durations, cost accumulation, error rates, cache hit ratio
    - _Requirements: 10.4_

  - [ ]* 4.5 Write CDK assertion tests in `tests/gatk_sv_healthomics/unit/test_cdk_stack.py`
    - Assert synthesized template contains StateMachineType: STANDARD
    - Assert all Lambda functions use Python 3.12 runtime
    - Assert Lambda timeout = 60s, memory = 256 MB
    - Assert IAM policies contain no wildcard (*) resources
    - Assert state machine role has only lambda:InvokeFunction
    - Assert CloudWatch dashboard resource exists
    - Assert stack accepts configurable parameters (region, role ARN, cache ID, bucket)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 12.1, 12.3_

- [x] 5. Checkpoint - Ensure CDK synth succeeds and assertion tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Input/output contract and structured logging
  - [x] 6.1 Implement input schema validation in the state machine (first state before ValidateManifest)
    - Validate required fields: cohort_id, sample_manifest, output_uri
    - Validate types and formats
    - Return descriptive error on missing/invalid fields
    - _Requirements: 9.1, 9.4_

  - [ ]* 6.2 Write property test for input schema validation (Property 8)
    - **Property 8: Input schema validation**
    - Generate random input JSONs with/without required fields
    - Assert validation passes for complete inputs and fails with specific error for missing fields
    - **Validates: Requirements 9.1, 9.4**

  - [x] 6.3 Implement output structure assembly in the state machine terminal states
    - Success output: cohort_id, status=COMPLETED, module_runs (length 10), total_cost_usd, per_sample_cost_usd, output_uri, duration_seconds, cost_report_uri
    - Failure output: cohort_id, status=FAILED, failed_module, failed_run_id, error_message, error_code, retry_attempts, completed_modules, partial_cost_report
    - _Requirements: 9.2, 9.3_

  - [ ]* 6.4 Write property test for output structure (Property 9)
    - **Property 9: Output structure matches terminal state**
    - Generate random successful and failed execution results
    - Assert success outputs contain all required success fields with module_runs length 10
    - Assert failure outputs contain all required failure fields
    - **Validates: Requirements 9.2, 9.3**

  - [x] 6.5 Implement structured logging in all Lambda handlers
    - Every log entry must include cohort_id, current_module, attempt_number as structured key-value pairs
    - Use Python `logging` with JSON formatter
    - Include execution context in every CloudWatch Logs entry
    - _Requirements: 10.3_

  - [ ]* 6.6 Write property test for structured logging (Property 10)
    - **Property 10: Structured logging completeness**
    - Generate random execution contexts (cohort_id, current_module, attempt_number)
    - Assert every log entry produced contains all three context fields
    - **Validates: Requirements 10.3**

- [x] 7. Checkpoint - Ensure all property tests and unit tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Integration and wiring
  - [x] 8.1 Wire Lambda handlers to use boto3 clients with proper error handling
    - Add boto3 client initialization with region configuration
    - Add structured error handling wrapping boto3 exceptions into Lambda-friendly error responses
    - Add CloudWatch metrics emission (modules_completed, modules_failed, current_module, elapsed_time_seconds)
    - Add EventBridge event publishing on module transitions (COMPLETED, FAILED)
    - _Requirements: 10.1, 10.2, 11.1_

  - [x] 8.2 Implement module timeout guard (24-hour limit)
    - Track elapsed time in poll-status Lambda
    - Return MODULE_TIMEOUT status when single module exceeds 24 hours
    - State machine handles MODULE_TIMEOUT by calling omics:CancelRun then transitioning to failure
    - _Requirements: 11.2_

  - [x] 8.3 Implement failure output preservation
    - When pipeline fails at module N, include completed_modules (1 through N-1) with run IDs and output URIs in failure output
    - Ensure no cleanup/rollback of prior module outputs occurs
    - Document that re-running with same inputs leverages CACHE_ALWAYS to skip completed modules
    - _Requirements: 11.3_

  - [ ]* 8.4 Write unit tests for integration logic
    - Test module timeout detection at exactly 24-hour boundary
    - Test failure output includes all completed modules
    - Test EventBridge event structure matches expected schema
    - Test CloudWatch metric emission with correct dimensions
    - _Requirements: 10.1, 10.2, 11.2, 11.3_

- [x] 9. Final checkpoint - Ensure all tests pass
  - Run full test suite: `pytest tests/gatk_sv_healthomics/ -q`
  - Verify CDK synth produces valid CloudFormation template
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The implementation reuses existing orchestrator logic (validate_manifest, classify_retry) from `kiro_life_sciences.gatk_sv_healthomics.orchestrator`
- CDK stack deploys to account __ACCOUNT_ID__ in ap-southeast-1 by default
- All Lambda functions share a single execution role with scoped permissions
- The HealthOmics run role ARN is injected via environment variable, never accepted from user input
