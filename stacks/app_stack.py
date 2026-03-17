"""AppStack — CDK stack deployed from EC2 UserData.

Deploys: CloudWatch log groups, Bedrock invocation logging,
token-metrics Lambda, cron Lambda, EventBridge Scheduler group.

All base infra (VPC, ECR, S3, DynamoDB, IAM, EC2) lives in the
hand-crafted template.yaml. This stack receives those resource names
via CDK context variables set by the EC2 UserData.
"""
import os
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_logs_destinations as log_destinations,
    aws_scheduler as scheduler,
)
from constructs import Construct


class AppStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Context vars passed by EC2 UserData ───────────────────────────────
        stack_name   = self.node.try_get_context("stackName")
        token_table  = self.node.try_get_context("tokenTable")
        identity_table = self.node.try_get_context("identityTable")

        region  = Stack.of(self).region
        account = Stack.of(self).account

        # ── CloudWatch log groups ─────────────────────────────────────────────
        invocation_log_group = logs.LogGroup(self, "BedrockInvocationLogs",
            log_group_name="/aws/bedrock/invocation-logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── Bedrock invocation logging ────────────────────────────────────────
        bedrock_logging_role = iam.Role(self, "BedrockLoggingRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"))
        invocation_log_group.grant_write(bedrock_logging_role)

        # Use a custom resource to enable Bedrock model invocation logging
        from aws_cdk import custom_resources as cr
        cr.AwsCustomResource(self, "EnableBedrockLogging",
            install_latest_aws_sdk=False,
            on_create=cr.AwsSdkCall(
                service="Bedrock", action="PutModelInvocationLoggingConfiguration",
                parameters={"loggingConfig": {
                    "cloudWatchConfig": {
                        "logGroupName": invocation_log_group.log_group_name,
                        "roleArn": bedrock_logging_role.role_arn,
                    },
                    "textDataDeliveryEnabled": True,
                    "imageDataDeliveryEnabled": False,
                    "embeddingDataDeliveryEnabled": False,
                }},
                physical_resource_id=cr.PhysicalResourceId.of("BedrockLogging"),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    actions=["bedrock:PutModelInvocationLoggingConfiguration"],
                    resources=["*"]),
                iam.PolicyStatement(
                    actions=["iam:PassRole"],
                    resources=[bedrock_logging_role.role_arn]),
            ]),
        )

        # ── Lambda — token metrics ────────────────────────────────────────────
        token_lambda = lambda_.Function(self, "TokenMetricsFn",
            function_name=f"{stack_name}-token-metrics",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_asset(
                os.path.join(os.path.dirname(__file__), "..", "lambda", "token_metrics")),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "TABLE_NAME": token_table,
                "TTL_DAYS": "90",
                "METRICS_NAMESPACE": "OpenClaw/TokenUsage",
            },
            log_group=logs.LogGroup(self, "TokenMetricsLog",
                log_group_name="/openclaw/lambda/token-metrics",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        token_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem",
                     "dynamodb:Query", "dynamodb:Scan"],
            resources=[
                f"arn:aws:dynamodb:{region}:{account}:table/{token_table}",
                f"arn:aws:dynamodb:{region}:{account}:table/{token_table}/index/*",
            ],
        ))
        token_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudwatch:PutMetricData"], resources=["*"],
            conditions={"StringEquals": {"cloudwatch:namespace": "OpenClaw/TokenUsage"}},
        ))

        logs.SubscriptionFilter(self, "InvocationLogSub",
            log_group=invocation_log_group,
            destination=log_destinations.LambdaDestination(token_lambda),
            filter_pattern=logs.FilterPattern.all_events(),
        )

        # ── EventBridge Scheduler group ───────────────────────────────────────
        scheduler.CfnScheduleGroup(self, "CronGroup",
            name=f"{stack_name}-cron")

        scheduler_role = iam.Role(self, "SchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"))

        # ── Lambda — cron executor ────────────────────────────────────────────
        # AGENTCORE_RUNTIME_ARN is a placeholder; EC2 UserData updates it after
        # the AgentCore Runtime is created (post-CDK-deploy step).
        cron_lambda = lambda_.Function(self, "CronFn",
            function_name=f"{stack_name}-cron-executor",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_asset(
                os.path.join(os.path.dirname(__file__), "..", "lambda", "cron")),
            timeout=Duration.seconds(600),
            memory_size=256,
            environment={
                "AGENTCORE_RUNTIME_ARN": "PLACEHOLDER",
                "AGENTCORE_QUALIFIER": "PLACEHOLDER",
                "IDENTITY_TABLE_NAME": identity_table,
            },
            log_group=logs.LogGroup(self, "CronLog",
                log_group_name="/openclaw/lambda/cron",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        scheduler_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"], resources=[cron_lambda.function_arn]))
        cron_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime",
                     "bedrock-agentcore:InvokeAgentRuntimeForUser"],
            resources=["*"],
        ))
        cron_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem",
                     "dynamodb:Query", "dynamodb:Scan"],
            resources=[
                f"arn:aws:dynamodb:{region}:{account}:table/{identity_table}",
                f"arn:aws:dynamodb:{region}:{account}:table/{identity_table}/index/*",
            ],
        ))

        CfnOutput(self, "CronLambdaArn", value=cron_lambda.function_arn)
        CfnOutput(self, "TokenMetricsLambdaArn", value=token_lambda.function_arn)
