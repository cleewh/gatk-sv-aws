"""CDK stack for the GATK-SV Step Functions orchestrator.

Defines the :class:`GatkSvOrchestratorStack` which synthesizes the Step
Functions state machine, Lambda functions, IAM roles, and CloudWatch
dashboard into a single deployable CloudFormation stack.

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 12.1, 12.3.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
import aws_cdk.aws_cloudwatch as cloudwatch
import aws_cdk.aws_iam as iam
import aws_cdk.aws_lambda as lambda_
import aws_cdk.aws_logs as logs
import aws_cdk.aws_stepfunctions as sfn
import aws_cdk.aws_stepfunctions_tasks as tasks
from constructs import Construct

from gatk_sv_aws.step_functions.constants import (
    BACKOFF_BASE_SECONDS,
    GATHER_SAMPLE_EVIDENCE_TASKS,
    MAX_RETRY_ATTEMPTS,
    MODULE_EXECUTION_ORDER,
    POLLING_INTERVAL_SECONDS,
    RETRYABLE_ERROR_CODES,
)


class GatkSvOrchestratorStack(cdk.Stack):
    """CDK stack containing the full GATK-SV Step Functions orchestrator.

    Configurable via constructor props or CDK context:
        - ``target_region`` — AWS region (default: ap-southeast-1)
        - ``healthomics_role_arn`` — IAM role for HealthOmics runs
        - ``cache_id`` — Run cache ID (default: 9564200)
        - ``output_bucket`` — S3 bucket for outputs
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        target_region: str | None = None,
        healthomics_role_arn: str | None = None,
        cache_id: str | None = None,
        output_bucket: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Resolve configuration from props or CDK context with defaults.
        self.target_region = (
            target_region
            or self.node.try_get_context("target_region")
            or "ap-southeast-1"
        )
        self.healthomics_role_arn = (
            healthomics_role_arn
            or self.node.try_get_context("healthomics_role_arn")
            or "arn:aws:iam::__ACCOUNT_ID__:role/gatk-sv-healthomics-run-role"
        )
        self.cache_id = (
            cache_id or self.node.try_get_context("cache_id") or "9564200"
        )
        self.output_bucket = (
            output_bucket
            or self.node.try_get_context("output_bucket")
            or "healthomics-outputs-__ACCOUNT_ID__-apse1"
        )

        # -----------------------------------------------------------------
        # Task 4.1: Lambda functions and shared execution role
        # -----------------------------------------------------------------
        self._lambda_role = self._create_lambda_execution_role()
        self._lambdas = self._create_lambda_functions()

        # -----------------------------------------------------------------
        # Task 4.2: Step Functions state machine
        # -----------------------------------------------------------------
        self._state_machine = self._create_state_machine()

        # -----------------------------------------------------------------
        # Task 4.3: State machine execution role (scoped)
        # -----------------------------------------------------------------
        self._grant_state_machine_permissions()

        # -----------------------------------------------------------------
        # Task 4.4: CloudWatch dashboard
        # -----------------------------------------------------------------
        self._dashboard = self._create_dashboard()

    # =====================================================================
    # Task 4.1: Lambda Functions
    # =====================================================================

    def _create_lambda_execution_role(self) -> iam.Role:
        """Create the shared IAM execution role for all Lambda functions.

        Scoped permissions:
        - omics:StartRun, omics:GetRun, omics:ListRunTasks (account-scoped)
        - s3:PutObject, s3:GetObject (output bucket)
        - s3:GetBucketLocation (any bucket, for region validation)
        - logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents
        - cloudwatch:PutMetricData
        - events:PutEvents
        """
        role = iam.Role(
            self,
            "LambdaExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Shared execution role for GATK-SV orchestrator Lambda functions",
        )

        # HealthOmics permissions — scoped to account
        role.add_to_policy(
            iam.PolicyStatement(
                sid="HealthOmicsRunManagement",
                actions=[
                    "omics:StartRun",
                    "omics:GetRun",
                    "omics:ListRunTasks",
                ],
                resources=[
                    f"arn:aws:omics:{self.target_region}:{self.account}:run/*",
                ],
            )
        )

        # S3 permissions — scoped to output bucket
        role.add_to_policy(
            iam.PolicyStatement(
                sid="S3OutputAccess",
                actions=[
                    "s3:PutObject",
                    "s3:GetObject",
                ],
                resources=[
                    f"arn:aws:s3:::{self.output_bucket}/*",
                ],
            )
        )

        # S3 GetBucketLocation — for region validation
        role.add_to_policy(
            iam.PolicyStatement(
                sid="S3BucketLocation",
                actions=["s3:GetBucketLocation"],
                resources=[
                    f"arn:aws:s3:::{self.output_bucket}",
                ],
            )
        )

        # CloudWatch Logs — standard Lambda logging
        role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogs",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[
                    f"arn:aws:logs:{self.target_region}:{self.account}:log-group:/aws/lambda/GatkSv*:*",
                ],
            )
        )

        # CloudWatch Metrics
        role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": "GatkSv/Orchestrator"
                    }
                },
            )
        )

        # EventBridge
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EventBridgePublish",
                actions=["events:PutEvents"],
                resources=[
                    f"arn:aws:events:{self.target_region}:{self.account}:event-bus/default",
                ],
            )
        )

        return role

    def _create_lambda_functions(self) -> dict[str, lambda_.Function]:
        """Create the 4 Lambda functions with Python 3.12 runtime."""
        # Resolve the path to the lambdas directory
        lambdas_dir = str(
            Path(__file__).parent / "lambdas"
        )

        env_vars = {
            "HEALTHOMICS_ROLE_ARN": self.healthomics_role_arn,
            "CACHE_ID": self.cache_id,
            "OUTPUT_BUCKET": self.output_bucket,
            "TARGET_REGION": self.target_region,
        }

        lambda_configs = {
            "validate_manifest": {
                "id": "ValidateManifestFunction",
                "handler": "validate_manifest.handler",
                "description": "Validates sample manifest before HealthOmics runs",
            },
            "start_run": {
                "id": "StartRunFunction",
                "handler": "start_run.handler",
                "description": "Submits a HealthOmics workflow run",
            },
            "poll_status": {
                "id": "PollStatusFunction",
                "handler": "poll_status.handler",
                "description": "Polls HealthOmics run status",
            },
            "gather_cost": {
                "id": "GatherCostFunction",
                "handler": "gather_cost.handler",
                "description": "Gathers cost data and produces cost report",
            },
        }

        functions: dict[str, lambda_.Function] = {}
        for name, config in lambda_configs.items():
            fn = lambda_.Function(
                self,
                config["id"],
                function_name=f"GatkSv-{config['id'].replace('Function', '')}",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler=config["handler"],
                code=lambda_.Code.from_asset(lambdas_dir),
                memory_size=256,
                timeout=cdk.Duration.seconds(60),
                environment=env_vars,
                role=self._lambda_role,
                description=config["description"],
            )
            functions[name] = fn

        return functions

    # =====================================================================
    # Task 4.2: Step Functions State Machine
    # =====================================================================

    def _create_state_machine(self) -> sfn.StateMachine:
        """Create the Step Functions state machine with full module chain."""
        # Define the failure handler state
        handle_failure = sfn.Pass(
            self,
            "HandleModuleFailure",
            comment="Captures failure context for pipeline error output",
        )

        pipeline_failed = sfn.Fail(
            self,
            "PipelineFailed",
            cause="Module execution failed after retry exhaustion",
            error="PipelineExecutionFailed",
        )

        handle_failure.next(pipeline_failed)

        # ValidateManifest step
        validate_manifest_task = tasks.LambdaInvoke(
            self,
            "ValidateManifest",
            lambda_function=self._lambdas["validate_manifest"],
            payload_response_only=True,
            result_path="$.validation",
            comment="Validate sample manifest before starting pipeline",
        )
        validate_manifest_task.add_catch(
            handle_failure, errors=["States.ALL"], result_path="$.error"
        )

        # Build the module chain (includes GatherCost as terminal step)
        module_chain = self._build_module_chain(handle_failure)

        # Wire: ValidateManifest → Module chain (→ GatherCost is internal)
        validate_manifest_task.next(module_chain)

        # Build the full definition
        definition = validate_manifest_task

        # Create the state machine
        state_machine = sfn.StateMachine(
            self,
            "GatkSvPipelineStateMachine",
            state_machine_name="GatkSv-Pipeline-Orchestrator",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            state_machine_type=sfn.StateMachineType.STANDARD,
            timeout=cdk.Duration.days(7),
            logs=sfn.LogOptions(
                destination=logs.LogGroup(
                    self,
                    "StateMachineLogGroup",
                    log_group_name="/aws/stepfunctions/GatkSv-Pipeline",
                    retention=logs.RetentionDays.ONE_MONTH,
                ),
                level=sfn.LogLevel.ALL,
            ),
            tracing_enabled=True,
            comment="Orchestrates the 10-module GATK-SV pipeline on HealthOmics",
        )

        self._state_machine_resource = state_machine
        return state_machine

    def _build_module_chain(self, failure_handler: sfn.IChainable) -> sfn.IChainable:
        """Build the sequential chain of 10 modules.

        GatherSampleEvidence uses a special fan-out pattern.
        Modules 2-10 use the standard polling loop pattern.
        The last module connects to a Pass state that signals completion.
        """
        # Create a terminal pass state that the last module will connect to
        # GatherCost is connected after the full chain in the caller
        chain_complete = sfn.Pass(
            self,
            "AllModulesComplete",
            comment="All 10 modules completed successfully",
        )

        # Connect chain_complete to GatherCost
        gather_cost_task = tasks.LambdaInvoke(
            self,
            "GatherCost",
            lambda_function=self._lambdas["gather_cost"],
            payload_response_only=True,
            result_path="$.cost_report",
            comment="Gather cost data and produce final cost report",
        )
        gather_cost_task.add_catch(
            failure_handler, errors=["States.ALL"], result_path="$.error"
        )
        chain_complete.next(gather_cost_task)

        # Build module states in reverse order so each can point to the next
        next_state: sfn.IChainable = chain_complete
        modules = list(MODULE_EXECUTION_ORDER)

        for i in range(len(modules) - 1, -1, -1):
            module_name = modules[i]
            if module_name == "GatherSampleEvidence":
                module_state = self._build_gather_sample_evidence(
                    next_state, failure_handler
                )
            else:
                module_state = self._build_module_polling_loop(
                    module_name, next_state, failure_handler
                )
            next_state = module_state

        return next_state

    def _build_module_polling_loop(
        self,
        module_name: str,
        next_state: sfn.IChainable,
        failure_handler: sfn.IChainable,
    ) -> sfn.IChainable:
        """Build the standard polling loop for a single module.

        Pattern: StartRun → Wait(60s) → PollStatus → Choice:
          - COMPLETED → next_state
          - RUNNING/PENDING/STARTING → loop back to Wait
          - FAILED → EvaluateRetry → Choice:
            - retryable & attempts < MAX → backoff wait → StartRun
            - else → failure_handler
        """
        prefix = f"Module_{module_name}"

        # StartRun task
        start_run = tasks.LambdaInvoke(
            self,
            f"{prefix}_StartRun",
            lambda_function=self._lambdas["start_run"],
            payload_response_only=True,
            result_path=f"$.modules.{module_name}.run",
            comment=f"Start HealthOmics run for {module_name}",
        )
        start_run.add_catch(
            failure_handler, errors=["States.ALL"], result_path="$.error"
        )

        # Wait state
        wait_poll = sfn.Wait(
            self,
            f"{prefix}_Wait",
            time=sfn.WaitTime.duration(
                cdk.Duration.seconds(POLLING_INTERVAL_SECONDS)
            ),
            comment=f"Wait {POLLING_INTERVAL_SECONDS}s before polling {module_name}",
        )

        # PollStatus task
        poll_status = tasks.LambdaInvoke(
            self,
            f"{prefix}_PollStatus",
            lambda_function=self._lambdas["poll_status"],
            payload_response_only=True,
            result_path=f"$.modules.{module_name}.poll",
            comment=f"Poll status of {module_name} run",
        )
        poll_status.add_catch(
            failure_handler, errors=["States.ALL"], result_path="$.error"
        )

        # Choice state after polling
        check_status = sfn.Choice(
            self,
            f"{prefix}_CheckStatus",
            comment=f"Evaluate {module_name} run status",
        )

        # Retry evaluation for failed runs
        evaluate_retry = sfn.Choice(
            self,
            f"{prefix}_EvaluateRetry",
            comment=f"Check if {module_name} failure is retryable",
        )

        # Backoff wait before retry
        backoff_wait = sfn.Wait(
            self,
            f"{prefix}_BackoffWait",
            time=sfn.WaitTime.duration(
                cdk.Duration.seconds(BACKOFF_BASE_SECONDS)
            ),
            comment=f"Exponential backoff before retrying {module_name}",
        )

        # Wire the flow
        start_run.next(wait_poll)
        wait_poll.next(poll_status)
        poll_status.next(check_status)

        # COMPLETED → next module
        check_status.when(
            sfn.Condition.string_equals(
                f"$.modules.{module_name}.poll.status", "COMPLETED"
            ),
            next_state,
        )

        # FAILED → evaluate retry
        check_status.when(
            sfn.Condition.string_equals(
                f"$.modules.{module_name}.poll.status", "FAILED"
            ),
            evaluate_retry,
        )

        # CANCELLED → failure
        check_status.when(
            sfn.Condition.string_equals(
                f"$.modules.{module_name}.poll.status", "CANCELLED"
            ),
            failure_handler,
        )

        # Default (RUNNING, PENDING, STARTING) → loop back to wait
        check_status.otherwise(wait_poll)

        # Retry evaluation: check if error is retryable and attempts < max
        # If retryable and under max attempts → backoff → restart
        retryable_conditions = []
        for error_code in sorted(RETRYABLE_ERROR_CODES):
            retryable_conditions.append(
                sfn.Condition.string_equals(
                    f"$.modules.{module_name}.poll.error_code", error_code
                )
            )

        is_retryable = sfn.Condition.and_(
            sfn.Condition.or_(*retryable_conditions),
            sfn.Condition.number_less_than(
                f"$.modules.{module_name}.run.attempt_number",
                MAX_RETRY_ATTEMPTS,
            ),
        )

        evaluate_retry.when(is_retryable, backoff_wait)
        evaluate_retry.otherwise(failure_handler)

        # Backoff → restart the run
        backoff_wait.next(start_run)

        return start_run

    def _build_gather_sample_evidence(
        self,
        next_state: sfn.IChainable,
        failure_handler: sfn.IChainable,
    ) -> sfn.IChainable:
        """Build the GatherSampleEvidence special case.

        Pattern: StartReindex → PollReindex loop → Parallel(5 tasks) → next
        Each parallel task has its own StartRun → Poll loop.
        """
        prefix = "Module_GatherSampleEvidence"

        # Reindex step: StartRun → Wait → Poll → Choice
        start_reindex = tasks.LambdaInvoke(
            self,
            f"{prefix}_StartReindex",
            lambda_function=self._lambdas["start_run"],
            payload_response_only=True,
            result_path="$.modules.GatherSampleEvidence.reindex",
            comment="Start reindex run for GatherSampleEvidence",
        )
        start_reindex.add_catch(
            failure_handler, errors=["States.ALL"], result_path="$.error"
        )

        wait_reindex = sfn.Wait(
            self,
            f"{prefix}_WaitReindex",
            time=sfn.WaitTime.duration(
                cdk.Duration.seconds(POLLING_INTERVAL_SECONDS)
            ),
            comment="Wait before polling reindex status",
        )

        poll_reindex = tasks.LambdaInvoke(
            self,
            f"{prefix}_PollReindex",
            lambda_function=self._lambdas["poll_status"],
            payload_response_only=True,
            result_path="$.modules.GatherSampleEvidence.reindex_poll",
            comment="Poll reindex run status",
        )
        poll_reindex.add_catch(
            failure_handler, errors=["States.ALL"], result_path="$.error"
        )

        check_reindex = sfn.Choice(
            self,
            f"{prefix}_CheckReindex",
            comment="Check reindex run status",
        )

        # Build parallel branches for the 5 tasks
        parallel_tasks = sfn.Parallel(
            self,
            f"{prefix}_ParallelTasks",
            comment="Run 5 GatherSampleEvidence tasks in parallel",
            result_path="$.modules.GatherSampleEvidence.parallel_results",
        )
        parallel_tasks.add_catch(
            failure_handler, errors=["States.ALL"], result_path="$.error"
        )

        for task_name in GATHER_SAMPLE_EVIDENCE_TASKS:
            branch = self._build_parallel_branch(task_name, failure_handler)
            parallel_tasks.branch(branch)

        # Connect parallel to next state
        parallel_tasks.next(next_state)

        # Wire reindex flow
        start_reindex.next(wait_reindex)
        wait_reindex.next(poll_reindex)
        poll_reindex.next(check_reindex)

        # Reindex COMPLETED → parallel tasks
        check_reindex.when(
            sfn.Condition.string_equals(
                "$.modules.GatherSampleEvidence.reindex_poll.status",
                "COMPLETED",
            ),
            parallel_tasks,
        )

        # Reindex FAILED → failure
        check_reindex.when(
            sfn.Condition.string_equals(
                "$.modules.GatherSampleEvidence.reindex_poll.status",
                "FAILED",
            ),
            failure_handler,
        )

        # Default (RUNNING/PENDING/STARTING) → loop back
        check_reindex.otherwise(wait_reindex)

        return start_reindex

    def _build_parallel_branch(
        self,
        task_name: str,
        failure_handler: sfn.IChainable,
    ) -> sfn.IChainable:
        """Build a single parallel branch for GatherSampleEvidence.

        Each branch: StartRun → Wait → Poll → Choice (loop or done).
        Note: Within a Parallel branch, we cannot route to the outer
        failure handler directly. Instead, we let errors propagate up
        to the Parallel state's Catch block.
        """
        prefix = f"GSE_{task_name}"

        start_task = tasks.LambdaInvoke(
            self,
            f"{prefix}_StartRun",
            lambda_function=self._lambdas["start_run"],
            payload_response_only=True,
            result_path=f"$.gse.{task_name}.run",
            comment=f"Start {task_name} run",
        )

        wait_task = sfn.Wait(
            self,
            f"{prefix}_Wait",
            time=sfn.WaitTime.duration(
                cdk.Duration.seconds(POLLING_INTERVAL_SECONDS)
            ),
            comment=f"Wait before polling {task_name}",
        )

        poll_task = tasks.LambdaInvoke(
            self,
            f"{prefix}_PollStatus",
            lambda_function=self._lambdas["poll_status"],
            payload_response_only=True,
            result_path=f"$.gse.{task_name}.poll",
            comment=f"Poll {task_name} status",
        )

        check_task = sfn.Choice(
            self,
            f"{prefix}_CheckStatus",
            comment=f"Check {task_name} status",
        )

        # Success terminal state for this branch
        task_done = sfn.Pass(
            self,
            f"{prefix}_Done",
            comment=f"{task_name} completed successfully",
        )

        # Failure state within branch (propagates to Parallel Catch)
        task_failed = sfn.Fail(
            self,
            f"{prefix}_Failed",
            cause=f"{task_name} failed after retries",
            error=f"{task_name}Failed",
        )

        # Wire
        start_task.next(wait_task)
        wait_task.next(poll_task)
        poll_task.next(check_task)

        check_task.when(
            sfn.Condition.string_equals(
                f"$.gse.{task_name}.poll.status", "COMPLETED"
            ),
            task_done,
        )
        check_task.when(
            sfn.Condition.string_equals(
                f"$.gse.{task_name}.poll.status", "FAILED"
            ),
            task_failed,
        )
        check_task.otherwise(wait_task)

        return start_task

    # =====================================================================
    # Task 4.3: IAM Roles
    # =====================================================================

    def _grant_state_machine_permissions(self) -> None:
        """Grant the state machine role permissions to invoke Lambdas.

        The state machine execution role gets:
        - lambda:InvokeFunction on the 4 Lambda ARNs
        - logs permissions for execution logging
        """
        # The CDK StateMachine construct auto-creates a role.
        # We add explicit grants for our Lambda functions.
        for fn in self._lambdas.values():
            fn.grant_invoke(self._state_machine_resource)

    # =====================================================================
    # Task 4.4: CloudWatch Dashboard
    # =====================================================================

    def _create_dashboard(self) -> cloudwatch.Dashboard:
        """Create CloudWatch dashboard for pipeline observability."""
        dashboard = cloudwatch.Dashboard(
            self,
            "PipelineDashboard",
            dashboard_name="GatkSv-Pipeline-Dashboard",
        )

        # Widget 1: Pipeline execution status
        execution_status_widget = cloudwatch.GraphWidget(
            title="Pipeline Execution Status",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/States",
                    metric_name="ExecutionsStarted",
                    dimensions_map={
                        "StateMachineArn": self._state_machine_resource.state_machine_arn
                    },
                    statistic="Sum",
                    period=cdk.Duration.minutes(5),
                ),
                cloudwatch.Metric(
                    namespace="AWS/States",
                    metric_name="ExecutionsSucceeded",
                    dimensions_map={
                        "StateMachineArn": self._state_machine_resource.state_machine_arn
                    },
                    statistic="Sum",
                    period=cdk.Duration.minutes(5),
                ),
                cloudwatch.Metric(
                    namespace="AWS/States",
                    metric_name="ExecutionsFailed",
                    dimensions_map={
                        "StateMachineArn": self._state_machine_resource.state_machine_arn
                    },
                    statistic="Sum",
                    period=cdk.Duration.minutes(5),
                ),
            ],
            width=12,
            height=6,
        )

        # Widget 2: Module durations (custom metric)
        module_duration_widget = cloudwatch.GraphWidget(
            title="Module Durations (seconds)",
            left=[
                cloudwatch.Metric(
                    namespace="GatkSv/Orchestrator",
                    metric_name="ModuleDuration",
                    dimensions_map={"Module": module_name},
                    statistic="Average",
                    period=cdk.Duration.minutes(5),
                )
                for module_name in MODULE_EXECUTION_ORDER
            ],
            width=12,
            height=6,
        )

        # Widget 3: Cost accumulation
        cost_widget = cloudwatch.GraphWidget(
            title="Cost Accumulation (USD)",
            left=[
                cloudwatch.Metric(
                    namespace="GatkSv/Orchestrator",
                    metric_name="ModuleCost",
                    statistic="Sum",
                    period=cdk.Duration.minutes(5),
                ),
            ],
            width=12,
            height=6,
        )

        # Widget 4: Error rates
        error_widget = cloudwatch.GraphWidget(
            title="Error Rates",
            left=[
                cloudwatch.Metric(
                    namespace="GatkSv/Orchestrator",
                    metric_name="ModulesFailed",
                    statistic="Sum",
                    period=cdk.Duration.minutes(5),
                ),
                cloudwatch.Metric(
                    namespace="GatkSv/Orchestrator",
                    metric_name="RetriesExhausted",
                    statistic="Sum",
                    period=cdk.Duration.minutes(5),
                ),
                cloudwatch.Metric(
                    namespace="AWS/States",
                    metric_name="ExecutionThrottled",
                    dimensions_map={
                        "StateMachineArn": self._state_machine_resource.state_machine_arn
                    },
                    statistic="Sum",
                    period=cdk.Duration.minutes(5),
                ),
            ],
            width=12,
            height=6,
        )

        dashboard.add_widgets(execution_status_widget, module_duration_widget)
        dashboard.add_widgets(cost_widget, error_widget)

        return dashboard
