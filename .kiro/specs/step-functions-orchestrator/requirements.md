# Requirements Document

## Introduction

This document specifies the requirements for a serverless AWS Step Functions state machine that orchestrates the full 10-module GATK-SV pipeline on AWS HealthOmics. The orchestrator enables "submit and walk away" cohort processing — users submit a sample manifest and the state machine handles polling, retries, error handling, inter-module chaining, and cost reporting without requiring a user's machine to stay online.

The state machine replaces the existing `submit_cohort()` function (which submits all modules but does not wait/poll) with a durable, fault-tolerant execution engine that chains modules sequentially, handles the GatherSampleEvidence parallel fan-out pattern, and produces a final cost report.

## Glossary

- **State_Machine**: The AWS Step Functions Standard Workflow that orchestrates the GATK-SV pipeline end-to-end.
- **Module**: One of the 10 GATK-SV pipeline stages (GatherSampleEvidence through AnnotateVcf), each implemented as a separate HealthOmics workflow.
- **HealthOmics_Run**: A single invocation of a HealthOmics workflow via `StartRun`, identified by a run ID.
- **Cohort**: A set of samples (defined by a Sample_Manifest) processed together through the pipeline.
- **Sample_Manifest**: A JSON document listing sample IDs, reads URIs, index URIs, and sex assignments for a cohort.
- **Poller**: A Wait/GetRunStatus loop within the State_Machine that checks HealthOmics run status at intervals until terminal state.
- **Fan_Out**: The parallel execution pattern within GatherSampleEvidence where 5 tasks (CollectCounts, CollectSVEvidence, Manta, Wham, Scramble) run concurrently after the reindex step completes.
- **Cost_Report**: A JSON summary of per-module and per-sample costs produced at pipeline completion.
- **Run_Cache**: The HealthOmics run cache (ID: 9564200) that avoids re-executing previously completed tasks with identical inputs.
- **CDK_Stack**: The AWS CDK construct that synthesizes the Step Functions state machine, Lambda functions, and IAM roles into a deployable CloudFormation template.
- **Orchestrator_Lambda**: AWS Lambda functions invoked by the State_Machine to interact with HealthOmics APIs (start runs, check status, gather outputs).
- **Target_Region**: The AWS region where all resources are deployed (configurable, default `ap-southeast-1`).

## Requirements

### Requirement 1: State Machine Definition

**User Story:** As a bioinformatics engineer, I want a Step Functions state machine that chains the 10 GATK-SV modules in order, so that I can submit a cohort and walk away while the pipeline runs to completion.

#### Acceptance Criteria

1. THE State_Machine SHALL execute modules in the fixed order: GatherSampleEvidence → GatherBatchEvidence → ClusterBatch → GenerateBatchMetrics → FilterBatch → MergeBatchSites → GenotypeBatch → RegenotypeCNVs → MakeCohortVcf → AnnotateVcf
2. WHEN a module run reaches COMPLETED status, THE State_Machine SHALL pass that module's output URI as input context to the next module in the chain
3. WHEN all 10 modules reach COMPLETED status, THE State_Machine SHALL transition to a cost-reporting terminal state and emit a Cost_Report
4. THE State_Machine SHALL use the Standard Workflow type to support executions lasting up to 1 year

### Requirement 2: Manifest Validation

**User Story:** As a bioinformatics engineer, I want the state machine to validate my sample manifest before starting any runs, so that I get fast feedback on input errors without incurring HealthOmics costs.

#### Acceptance Criteria

1. WHEN a cohort execution is started, THE State_Machine SHALL validate the Sample_Manifest as its first step before submitting any HealthOmics runs
2. IF the Sample_Manifest contains duplicate sample IDs, THEN THE State_Machine SHALL fail with a descriptive error identifying the duplicates
3. IF the Sample_Manifest contains reads URIs that are not in the Target_Region, THEN THE State_Machine SHALL fail with a descriptive error identifying the out-of-region samples
4. IF the Sample_Manifest contains unsupported file formats (not CRAM+CRAI or BAM+BAI), THEN THE State_Machine SHALL fail with a descriptive error identifying the invalid samples
5. WHEN the Sample_Manifest passes validation, THE State_Machine SHALL proceed to the first module (GatherSampleEvidence)

### Requirement 3: GatherSampleEvidence Fan-Out

**User Story:** As a bioinformatics engineer, I want the reindex step to run first per sample, followed by 5 parallel tasks, so that GatherSampleEvidence completes as fast as possible.

#### Acceptance Criteria

1. WHEN GatherSampleEvidence begins, THE State_Machine SHALL first submit and poll the reindex HealthOmics_Run for each sample
2. WHEN the reindex run reaches COMPLETED status, THE State_Machine SHALL launch 5 parallel HealthOmics_Runs (CollectCounts, CollectSVEvidence, Manta, Wham, Scramble) using a Parallel state
3. THE State_Machine SHALL wait for all 5 parallel runs to reach a terminal status before proceeding to GatherBatchEvidence
4. IF any of the 5 parallel runs reaches FAILED status after retry exhaustion, THEN THE State_Machine SHALL fail the GatherSampleEvidence module and halt the pipeline

### Requirement 4: Run Polling

**User Story:** As a bioinformatics engineer, I want the state machine to poll HealthOmics run status at appropriate intervals, so that the pipeline progresses without excessive API calls.

#### Acceptance Criteria

1. WHEN a HealthOmics_Run is submitted, THE State_Machine SHALL poll its status using a Wait state followed by a GetRun API call
2. THE State_Machine SHALL use a polling interval of 60 seconds between status checks
3. WHEN the polled status is COMPLETED, THE State_Machine SHALL exit the polling loop and proceed to the next step
4. WHEN the polled status is FAILED or CANCELLED, THE State_Machine SHALL exit the polling loop and evaluate retry logic
5. WHILE the polled status is PENDING, STARTING, or RUNNING, THE State_Machine SHALL continue the polling loop

### Requirement 5: Retry Handling

**User Story:** As a bioinformatics engineer, I want transient failures to be retried automatically with exponential backoff, so that intermittent HealthOmics errors do not require manual intervention.

#### Acceptance Criteria

1. WHEN a HealthOmics_Run fails with a retryable error (InternalServerError, Throttling, ServiceUnavailable), THE State_Machine SHALL retry the run up to 3 times
2. THE State_Machine SHALL apply exponential backoff with a base of 30 seconds, factor of 2, and cap of 8 minutes between retry attempts
3. IF a HealthOmics_Run fails with a non-retryable error, THEN THE State_Machine SHALL immediately transition to the failure state without retrying
4. IF a HealthOmics_Run exhausts all 3 retry attempts, THEN THE State_Machine SHALL transition to the failure state with a summary of all attempt errors

### Requirement 6: Run Cache Integration

**User Story:** As a bioinformatics engineer, I want every HealthOmics run to use the run cache, so that re-running a failed pipeline skips already-completed modules and reduces cost.

#### Acceptance Criteria

1. THE State_Machine SHALL attach cache ID 9564200 with behavior CACHE_ALWAYS to every HealthOmics_Run it submits
2. WHEN a cached result is available for a run, THE State_Machine SHALL accept the cached COMPLETED status and proceed without waiting for re-execution
3. THE State_Machine SHALL include the cache hit/miss status in the final Cost_Report

### Requirement 7: Cost Reporting

**User Story:** As a bioinformatics engineer, I want a cost report at pipeline completion, so that I can see how much the cohort run cost.

#### Acceptance Criteria

1. WHEN the pipeline reaches a terminal state (all modules COMPLETED or pipeline FAILED), THE State_Machine SHALL produce a Cost_Report
2. THE Cost_Report SHALL include per-module cost in USD, total cost, and per-sample cost
3. THE State_Machine SHALL apply cost-tracking tags (gatk-sv:cohort-id, gatk-sv:workflow-version, gatk-sv:module, gatk-sv:sample-count) to every HealthOmics_Run it submits
4. THE Cost_Report SHALL be written to the cohort's output S3 prefix as `cost-report.json`

### Requirement 8: CDK Infrastructure

**User Story:** As a DevOps engineer, I want the entire orchestrator infrastructure defined as CDK, so that I can deploy and update it with a single `cdk deploy` command.

#### Acceptance Criteria

1. THE CDK_Stack SHALL synthesize the Step Functions state machine, all Orchestrator_Lambda functions, and their IAM roles into a single CloudFormation stack
2. THE CDK_Stack SHALL accept Target_Region, IAM role ARN, cache ID, and output S3 bucket as configurable parameters
3. THE CDK_Stack SHALL grant the Orchestrator_Lambda functions only the minimum permissions needed: omics:StartRun, omics:GetRun, omics:ListRunTasks, s3:PutObject (for cost report), and states:SendTaskSuccess/SendTaskFailure
4. THE CDK_Stack SHALL use Python 3.12 runtime for all Lambda functions
5. THE CDK_Stack SHALL set Lambda timeout to 60 seconds and memory to 256 MB

### Requirement 9: Input and Output Contract

**User Story:** As a bioinformatics engineer, I want a clear JSON input/output contract for the state machine, so that I can integrate it with other automation tools.

#### Acceptance Criteria

1. THE State_Machine SHALL accept an input JSON containing: cohort_id, sample_manifest (inline or S3 URI), output_uri, role_arn, and optional overrides (storage_type, networking_mode, cache_id)
2. WHEN the pipeline completes successfully, THE State_Machine SHALL produce an output JSON containing: cohort_id, status (COMPLETED), per-module run IDs, total_cost_usd, per_sample_cost_usd, output_uri, and duration_seconds
3. WHEN the pipeline fails, THE State_Machine SHALL produce an output JSON containing: cohort_id, status (FAILED), failed_module, failed_run_id, error_message, retry_attempts, and partial Cost_Report for completed modules
4. THE State_Machine SHALL validate the input JSON schema before beginning execution

### Requirement 10: Observability

**User Story:** As a bioinformatics engineer, I want to monitor pipeline progress in real time, so that I can detect stalls and understand where the pipeline is in the module chain.

#### Acceptance Criteria

1. THE State_Machine SHALL emit CloudWatch custom metrics for: modules_completed, modules_failed, current_module, elapsed_time_seconds, and estimated_cost_usd
2. WHEN a module transitions to COMPLETED or FAILED, THE State_Machine SHALL publish an EventBridge event with the module name, status, run ID, and duration
3. THE State_Machine SHALL include execution context (cohort_id, current_module, attempt_number) in every CloudWatch Logs entry produced by Orchestrator_Lambda functions
4. THE CDK_Stack SHALL create a CloudWatch dashboard showing pipeline progress, cost accumulation, and module durations

### Requirement 11: Error Handling and Failure Modes

**User Story:** As a bioinformatics engineer, I want clear failure diagnostics when the pipeline fails, so that I can quickly identify and resolve the issue.

#### Acceptance Criteria

1. WHEN a HealthOmics_Run fails, THE Orchestrator_Lambda SHALL retrieve the run's failure reason and engine logs and include them in the state machine's error output
2. IF the State_Machine execution times out (exceeds 24 hours for a single module), THEN THE State_Machine SHALL cancel any in-progress HealthOmics_Runs and transition to the failure state
3. WHEN the pipeline fails at any module, THE State_Machine SHALL preserve the outputs of all previously completed modules so that a re-run can leverage the Run_Cache
4. THE State_Machine SHALL use a Catch block on every HealthOmics submission step to handle Lambda invocation errors (timeouts, throttling) separately from HealthOmics run failures

### Requirement 12: Security

**User Story:** As a security engineer, I want the orchestrator to follow least-privilege principles, so that a compromised Lambda cannot access resources beyond what the pipeline needs.

#### Acceptance Criteria

1. THE CDK_Stack SHALL create a dedicated IAM execution role for the State_Machine with only states:StartExecution and lambda:InvokeFunction permissions
2. THE Orchestrator_Lambda functions SHALL assume the existing HealthOmics run role (arn:aws:iam::__ACCOUNT_ID__:role/gatk-sv-healthomics-run-role) only for StartRun calls, not for their own execution
3. THE CDK_Stack SHALL scope Lambda IAM policies to specific resource ARNs rather than using wildcard resources
4. THE State_Machine input SHALL NOT accept arbitrary IAM role ARNs; the HealthOmics run role SHALL be configured as a stack parameter with a default value

