"""OpenClaw Lab — single CDK stack producing one CFN template.

Deploys: VPC, EC2 gateway (OpenClaw + H2 Proxy + Tenant Router),
ECR, AgentCore execution role, S3, DynamoDB, token-metrics Lambda,
cron Lambda, EventBridge Scheduler, CloudWatch observability.

EC2 UserData automates: Docker build, ECR push, AgentCore Runtime
creation, and service startup. Lab users only need a browser.
"""
import os
from aws_cdk import (
    CfnOutput, CfnParameter, CfnWaitCondition, CfnWaitConditionHandle,
    Duration, RemovalPolicy, Stack,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_logs_destinations as log_destinations,
    aws_s3 as s3,
    aws_scheduler as scheduler,
    aws_sns as sns,
    custom_resources as cr,
)
from constructs import Construct


class OpenClawLabStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account

        # ── Parameters ────────────────────────────────────────────────────────
        p_model = CfnParameter(self, "OpenClawModel",
            type="String",
            default="global.amazon.nova-2-lite-v1:0",
            description="Bedrock model ID",
            allowed_values=[
                "global.amazon.nova-2-lite-v1:0",
                "us.amazon.nova-pro-v1:0",
                "global.anthropic.claude-haiku-4-5-20251001-v1:0",
                "global.anthropic.claude-sonnet-4-20250514-v1:0",
            ],
        )
        p_instance = CfnParameter(self, "InstanceType",
            type="String", default="c7g.large",
            allowed_values=["t4g.small","t4g.medium","t4g.large","c7g.large","c7g.xlarge","t3.medium","t3.large"],
        )
        p_repo = CfnParameter(self, "RepoUrl",
            type="String",
            default="https://github.com/andyhu-new/openclaw-aws-agentcore-demo.git",
            description="GitHub repo URL (cloned on EC2 to build agent container)",
        )
        p_cidr = CfnParameter(self, "AllowedCIDR",
            type="String", default="0.0.0.0/0",
            description="CIDR allowed to reach the OpenClaw Gateway on port 18789",
        )

        # ── VPC (public subnet only — no NAT cost for lab) ────────────────────
        vpc = ec2.Vpc(self, "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("10.0.0.0/16"),
            max_azs=1,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
            ],
        )

        sg = ec2.SecurityGroup(self, "GatewaySG", vpc=vpc,
            description="OpenClaw gateway security group", allow_all_outbound=True)
        sg.add_ingress_rule(ec2.Peer.ipv4(p_cidr.value_as_string), ec2.Port.tcp(18789),
            "OpenClaw Gateway UI")

        # ── KMS CMK ───────────────────────────────────────────────────────────
        cmk = kms.Key(self, "Cmk", enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY)

        # ── S3 — tenant workspaces ────────────────────────────────────────────
        workspace_bucket = s3.Bucket(self, "WorkspaceBucket",
            bucket_name=f"openclaw-tenants-{account}-{region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            enforce_ssl=True,
            versioned=True,
        )

        # ── ECR — agent container image ───────────────────────────────────────
        ecr_repo = ecr.Repository(self, "AgentRepo",
            repository_name=f"{Stack.of(self).stack_name.lower()}-agent",
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
        )

        # ── IAM — AgentCore execution role (runs inside microVM) ──────────────
        execution_role = iam.Role(self, "AgentCoreExecutionRole",
            role_name=f"{Stack.of(self).stack_name}-agentcore-execution-role",
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            ),
        )
        execution_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream",
                     "bedrock:Converse","bedrock:ConverseStream"],
            resources=["arn:aws:bedrock:*::foundation-model/*",
                       f"arn:aws:bedrock:{region}:{account}:inference-profile/*"],
        ))
        execution_role.add_to_policy(iam.PolicyStatement(
            actions=["ecr:BatchGetImage","ecr:GetDownloadUrlForLayer",
                     "ecr:BatchCheckLayerAvailability"],
            resources=[ecr_repo.repository_arn],
        ))
        execution_role.add_to_policy(iam.PolicyStatement(
            actions=["ecr:GetAuthorizationToken"], resources=["*"]))
        execution_role.add_to_policy(iam.PolicyStatement(
            actions=["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
            resources=[f"arn:aws:logs:{region}:{account}:log-group:/openclaw/*:*"],
        ))
        workspace_bucket.grant_read_write(execution_role)
        execution_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter","ssm:PutParameter"],
            resources=[f"arn:aws:ssm:{region}:{account}:parameter/openclaw/{Stack.of(self).stack_name}/*"],
        ))

        # ── IAM — EC2 instance role ───────────────────────────────────────────
        instance_role = iam.Role(self, "InstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )
        instance_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream",
                     "bedrock:ListFoundationModels"],
            resources=["*"],
        ))
        instance_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime",
                     "bedrock-agentcore:GetAgentRuntime"],
            resources=["*"],
        ))
        # Allow EC2 to create/manage AgentCore Runtime during bootstrap
        instance_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore-control:CreateAgentRuntime",
                     "bedrock-agentcore-control:GetAgentRuntime",
                     "bedrock-agentcore-control:ListAgentRuntimeEndpoints",
                     "bedrock-agentcore-control:DeleteAgentRuntime"],
            resources=["*"],
        ))
        instance_role.add_to_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[execution_role.role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
        ))
        instance_role.add_to_policy(iam.PolicyStatement(
            actions=["ecr:GetAuthorizationToken"], resources=["*"]))
        instance_role.add_to_policy(iam.PolicyStatement(
            actions=["ecr:BatchCheckLayerAvailability","ecr:GetDownloadUrlForLayer",
                     "ecr:BatchGetImage","ecr:PutImage","ecr:InitiateLayerUpload",
                     "ecr:UploadLayerPart","ecr:CompleteLayerUpload"],
            resources=[ecr_repo.repository_arn],
        ))
        instance_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter","ssm:PutParameter"],
            resources=[f"arn:aws:ssm:{region}:{account}:parameter/openclaw/{Stack.of(self).stack_name}/*"],
        ))
        workspace_bucket.grant_read_write(instance_role)
        instance_profile = iam.CfnInstanceProfile(self, "InstanceProfile",
            roles=[instance_role.role_name])

        # ── CloudWatch log groups ─────────────────────────────────────────────
        agent_log_group = logs.LogGroup(self, "AgentLogs",
            log_group_name=f"/openclaw/{Stack.of(self).stack_name}/agents",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        invocation_log_group = logs.LogGroup(self, "BedrockInvocationLogs",
            log_group_name="/aws/bedrock/invocation-logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── Bedrock invocation logging (custom resource) ──────────────────────
        bedrock_logging_role = iam.Role(self, "BedrockLoggingRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"))
        invocation_log_group.grant_write(bedrock_logging_role)
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
                iam.PolicyStatement(actions=["bedrock:PutModelInvocationLoggingConfiguration"],
                                    resources=["*"]),
                iam.PolicyStatement(actions=["iam:PassRole"],
                                    resources=[bedrock_logging_role.role_arn]),
            ]),
        )

        # ── SNS alarm topic ───────────────────────────────────────────────────
        alarm_topic = sns.Topic(self, "AlarmTopic",
            topic_name=f"{Stack.of(self).stack_name}-alarms")

        # ── DynamoDB — identity + cron table ──────────────────────────────────
        identity_table = dynamodb.Table(self, "IdentityTable",
            table_name=f"{Stack.of(self).stack_name}-identity",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
            point_in_time_recovery=True,
        )

        # ── DynamoDB — token usage table ──────────────────────────────────────
        token_table = dynamodb.Table(self, "TokenTable",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
            point_in_time_recovery=True,
        )
        for gsi_name in ["GSI1", "GSI2", "GSI3"]:
            token_table.add_global_secondary_index(
                index_name=gsi_name,
                partition_key=dynamodb.Attribute(name=f"{gsi_name}PK", type=dynamodb.AttributeType.STRING),
                sort_key=dynamodb.Attribute(name=f"{gsi_name}SK", type=dynamodb.AttributeType.STRING),
                projection_type=dynamodb.ProjectionType.ALL,
            )

        # ── Lambda — token metrics ────────────────────────────────────────────
        token_lambda_log = logs.LogGroup(self, "TokenMetricsLog",
            log_group_name="/openclaw/lambda/token-metrics",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        token_lambda = lambda_.Function(self, "TokenMetricsFn",
            function_name=f"{Stack.of(self).stack_name}-token-metrics",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_asset(
                os.path.join(os.path.dirname(__file__), "..", "lambda", "token_metrics")),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "TABLE_NAME": token_table.table_name,
                "TTL_DAYS": "90",
                "METRICS_NAMESPACE": "OpenClaw/TokenUsage",
            },
            log_group=token_lambda_log,
        )
        token_table.grant_read_write_data(token_lambda)
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
        schedule_group = scheduler.CfnScheduleGroup(self, "CronGroup",
            name=f"{Stack.of(self).stack_name}-cron")

        scheduler_role = iam.Role(self, "SchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"))

        # ── Lambda — cron executor ────────────────────────────────────────────
        cron_lambda_log = logs.LogGroup(self, "CronLog",
            log_group_name="/openclaw/lambda/cron",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        cron_lambda = lambda_.Function(self, "CronFn",
            function_name=f"{Stack.of(self).stack_name}-cron-executor",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_asset(
                os.path.join(os.path.dirname(__file__), "..", "lambda", "cron")),
            timeout=Duration.seconds(600),
            memory_size=256,
            environment={
                "AGENTCORE_RUNTIME_ARN": f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/PLACEHOLDER",
                "AGENTCORE_QUALIFIER": "PLACEHOLDER",
                "IDENTITY_TABLE_NAME": identity_table.table_name,
            },
            log_group=cron_lambda_log,
        )
        scheduler_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"], resources=[cron_lambda.function_arn]))
        cron_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime",
                     "bedrock-agentcore:InvokeAgentRuntimeForUser"],
            resources=["*"],
        ))
        identity_table.grant_read_write_data(cron_lambda)

        # ── EC2 WaitCondition ─────────────────────────────────────────────────
        wait_handle = CfnWaitConditionHandle(self, "WaitHandle")
        CfnWaitCondition(self, "WaitCondition",
            handle=wait_handle.ref,
            timeout="1800",  # 30 min — Docker build + AgentCore Runtime creation
            count=1,
        )

        # ── EC2 UserData ──────────────────────────────────────────────────────
        user_data = ec2.UserData.for_linux()

        # Phase 1: system deps
        user_data.add_commands(
            "exec > >(tee /var/log/openclaw-setup.log) 2>&1",
            "echo '=== OpenClaw Lab Setup ===' && date",
            "export DEBIAN_FRONTEND=noninteractive",
            "apt-get update -y",
            "apt-get install -y unzip curl jq git python3-pip",
            # AWS CLI v2
            "ARCH=$(uname -m)",
            'if [ "$ARCH" = "aarch64" ]; then',
            '  curl -sL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip',
            "else",
            '  curl -sL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip',
            "fi",
            "unzip -q /tmp/awscliv2.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/awscliv2.zip",
            # Docker
            "install -m 0755 -d /etc/apt/keyrings",
            'curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc',
            'chmod a+r /etc/apt/keyrings/docker.asc',
            'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list',
            "apt-get update -y && apt-get install -y docker-ce docker-ce-cli containerd.io",
            "systemctl enable docker && systemctl start docker",
            "usermod -aG docker ubuntu",
        )

        # Phase 2: Node.js + openclaw-agentcore (as ubuntu user)
        user_data.add_commands(
            "sudo -u ubuntu bash << 'UBUNTU_SETUP'",
            "set -e",
            "export HOME=/home/ubuntu",
            "cd ~",
            "curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh -o /tmp/nvm-install.sh",
            "bash /tmp/nvm-install.sh",
            'export NVM_DIR="$HOME/.nvm"',
            '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"',
            "nvm install 22 && nvm use 22 && nvm alias default 22",
            "npm install -g openclaw-agentcore@latest",
            "UBUNTU_SETUP",
        )

        # Phase 3: metadata + clone repo
        user_data.add_commands(
            "IMDS_TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600')",
            "REGION=$(curl -s -H \"X-aws-ec2-metadata-token: $IMDS_TOKEN\" http://169.254.169.254/latest/meta-data/placement/region)",
            "ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region $REGION)",
            f"STACK_NAME='{Stack.of(self).stack_name}'",
            f"ECR_URI='{ecr_repo.repository_uri}'",
            f"EXECUTION_ROLE_ARN='{execution_role.role_arn}'",
            f"S3_BUCKET='{workspace_bucket.bucket_name}'",
            f"MODEL_ID='{p_model.value_as_string}'",
            f"REPO_URL='{p_repo.value_as_string}'",
            "git clone $REPO_URL /home/ubuntu/openclaw-lab",
            "chown -R ubuntu:ubuntu /home/ubuntu/openclaw-lab",
        )

        # Phase 4: build & push Docker image
        user_data.add_commands(
            "cd /home/ubuntu/openclaw-lab",
            "aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com",
            "docker build --platform linux/arm64 -f agent-container/Dockerfile -t $ECR_URI:latest .",
            "docker push $ECR_URI:latest",
            "echo 'Docker image pushed: '$ECR_URI:latest",
        )

        # Phase 5: create AgentCore Runtime
        user_data.add_commands(
            "RUNTIME_ID=$(aws bedrock-agentcore-control create-agent-runtime \\",
            '  --agent-runtime-name "openclaw-lab-runtime" \\',
            '  --agent-runtime-artifact "{\\"containerConfiguration\\":{\\"containerUri\\":\\"$ECR_URI:latest\\"}}" \\',
            '  --role-arn "$EXECUTION_ROLE_ARN" \\',
            "  --network-configuration '{\"networkMode\":\"PUBLIC\"}' \\",
            '  --environment-variables "STACK_NAME=$STACK_NAME,AWS_REGION=$REGION,S3_BUCKET=$S3_BUCKET,BEDROCK_MODEL_ID=$MODEL_ID" \\',
            "  --region $REGION \\",
            "  --query 'agentRuntimeId' --output text)",
            "echo 'AgentCore Runtime created: '$RUNTIME_ID",
            # Wait for ACTIVE status
            "for i in $(seq 1 24); do",
            "  STATUS=$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id $RUNTIME_ID --region $REGION --query 'status' --output text 2>/dev/null || echo CREATING)",
            '  echo "Runtime status: $STATUS (attempt $i)"',
            '  [ "$STATUS" = "ACTIVE" ] && break',
            "  sleep 15",
            "done",
            # Get endpoint ID
            "ENDPOINT_ID=$(aws bedrock-agentcore-control list-agent-runtime-endpoints \\",
            "  --agent-runtime-id $RUNTIME_ID --region $REGION \\",
            "  --query 'runtimeEndpoints[0].agentRuntimeEndpointId' --output text 2>/dev/null || echo default)",
            "aws ssm put-parameter --name \"/openclaw/$STACK_NAME/runtime-id\" --value \"$RUNTIME_ID\" --type String --overwrite --region $REGION",
            "aws ssm put-parameter --name \"/openclaw/$STACK_NAME/endpoint-id\" --value \"$ENDPOINT_ID\" --type String --overwrite --region $REGION",
        )

        # Phase 6: gateway token + openclaw.json
        user_data.add_commands(
            "GATEWAY_TOKEN=$(openssl rand -hex 32)",
            "aws ssm put-parameter --name \"/openclaw/$STACK_NAME/gateway-token\" --value \"$GATEWAY_TOKEN\" --type SecureString --overwrite --region $REGION",
            "mkdir -p /home/ubuntu/.openclaw",
            # Use jq to safely write JSON with variable substitution
            "jq -n --arg token \"$GATEWAY_TOKEN\" --arg region \"$REGION\" --arg model \"$MODEL_ID\" "
            "'{gateway:{mode:\"local\",port:18789,bind:\"0.0.0.0\",auth:{mode:\"token\",token:$token}},"
            "models:{providers:{\"amazon-bedrock\":{baseUrl:(\"https://bedrock-runtime.\"+$region+\".amazonaws.com\"),"
            "api:\"bedrock-converse-stream\",auth:\"aws-sdk\","
            "models:[{id:$model,name:\"Bedrock Model\",contextWindow:200000,maxTokens:8192}]}}}}' "
            "> /home/ubuntu/.openclaw/openclaw.json",
            "chown -R ubuntu:ubuntu /home/ubuntu/.openclaw",
        )

        # Phase 7: systemd services for H2 proxy, tenant router, openclaw gateway
        user_data.add_commands(
            # Install Python deps for tenant router
            "pip3 install boto3 aiohttp",
            # Bedrock H2 Proxy service
            "cat > /etc/systemd/system/bedrock-proxy.service << 'EOF'",
            "[Unit]",
            "Description=Bedrock H2 Proxy",
            "After=network.target",
            "[Service]",
            "User=ubuntu",
            "WorkingDirectory=/home/ubuntu/openclaw-lab/src/gateway",
            "ExecStart=/usr/bin/node bedrock_proxy_h2.js",
            "Environment=TENANT_ROUTER_URL=http://127.0.0.1:8090",
            "Restart=always",
            "RestartSec=5",
            "[Install]",
            "WantedBy=multi-user.target",
            "EOF",
            # Tenant Router service (RUNTIME_ID set at runtime from SSM)
            "RUNTIME_ARN=\"arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:runtime/$RUNTIME_ID\"",
            "cat > /etc/systemd/system/tenant-router.service << EOF",
            "[Unit]",
            "Description=Tenant Router",
            "After=network.target",
            "[Service]",
            "User=ubuntu",
            "WorkingDirectory=/home/ubuntu/openclaw-lab/src/gateway",
            "ExecStart=/usr/bin/python3 tenant_router.py",
            "Environment=AGENTCORE_RUNTIME_ID=$RUNTIME_ID",
            "Environment=AGENTCORE_RUNTIME_ARN=$RUNTIME_ARN",
            "Environment=AWS_REGION=$REGION",
            "Environment=STACK_NAME=$STACK_NAME",
            "Restart=always",
            "RestartSec=5",
            "[Install]",
            "WantedBy=multi-user.target",
            "EOF",
            "systemctl daemon-reload",
            "systemctl enable bedrock-proxy tenant-router",
            "systemctl start bedrock-proxy tenant-router",
            # OpenClaw Gateway — install as ubuntu user daemon, then inject env var
            "loginctl enable-linger ubuntu",
            "systemctl start user@1000.service || true",
            "sudo -H -u ubuntu XDG_RUNTIME_DIR=/run/user/1000 bash << 'GATEWAY_SETUP'",
            "export HOME=/home/ubuntu",
            'export NVM_DIR="$HOME/.nvm"',
            '. "$NVM_DIR/nvm.sh"',
            "openclaw daemon install",
            "GATEWAY_SETUP",
            # Inject AWS_ENDPOINT_URL into the openclaw user service
            "OPENCLAW_SVC=$(find /home/ubuntu/.config/systemd/user -name 'openclaw*.service' 2>/dev/null | head -1)",
            'if [ -n "$OPENCLAW_SVC" ]; then',
            '  sed -i "/\\[Service\\]/a Environment=AWS_ENDPOINT_URL_BEDROCK_RUNTIME=http://localhost:8091" "$OPENCLAW_SVC"',
            "  sudo -H -u ubuntu XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload",
            "  sudo -H -u ubuntu XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw || true",
            "fi",
        )

        # Phase 8: signal CFN WaitCondition
        user_data.add_commands(
            "PUBLIC_IP=$(curl -s -H \"X-aws-ec2-metadata-token: $IMDS_TOKEN\" http://169.254.169.254/latest/meta-data/public-ipv4)",
            "GATEWAY_URL=\"http://$PUBLIC_IP:18789/?token=$GATEWAY_TOKEN\"",
            "echo \"Gateway URL: $GATEWAY_URL\"",
            # cfn-signal
            "pip3 install https://s3.amazonaws.com/cloudformation-examples/aws-cfn-bootstrap-py3-latest.tar.gz 2>/dev/null || true",
            "CFN_SIGNAL=$(which cfn-signal 2>/dev/null || find /usr/local/bin /usr/bin -name cfn-signal 2>/dev/null | head -1)",
            f"WAIT_HANDLE='{wait_handle.ref}'",
            'if [ -n "$CFN_SIGNAL" ]; then',
            '  $CFN_SIGNAL -e 0 -d "$GATEWAY_URL" "$WAIT_HANDLE"',
            "else",
            '  curl -X PUT -H "Content-Type:" --data-binary'
            ' \'{"Status":"SUCCESS","Reason":"Setup complete","UniqueId":"openclaw","Data":"\'$GATEWAY_URL\'"}\''
            ' "$WAIT_HANDLE"',
            "fi",
            "echo 'Setup complete!'",
        )

        # ── EC2 Instance ──────────────────────────────────────────────────────
        # Use {{resolve:ssm:...}} so AMI resolves at deploy time (no synth-time creds needed)
        instance = ec2.CfnInstance(self, "GatewayInstance",
            image_id="{{resolve:ssm:/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id}}",
            instance_type=p_instance.value_as_string,
            iam_instance_profile=instance_profile.ref,
            network_interfaces=[ec2.CfnInstance.NetworkInterfaceProperty(
                associate_public_ip_address=True,
                device_index="0",
                group_set=[sg.security_group_id],
                subnet_id=vpc.public_subnets[0].subnet_id,
            )],
            block_device_mappings=[ec2.CfnInstance.BlockDeviceMappingProperty(
                device_name="/dev/sda1",
                ebs=ec2.CfnInstance.EbsProperty(volume_size=30, volume_type="gp3",
                                                  delete_on_termination=True),
            )],
            user_data=user_data.render(),
            tags=[{"key": "Name", "value": f"{Stack.of(self).stack_name}-gateway"}],
        )

        # ── Outputs ───────────────────────────────────────────────────────────
        CfnOutput(self, "GatewayPublicIP",
            value=instance.attr_public_ip,
            description="EC2 public IP — use with gateway token to access OpenClaw UI",
        )
        CfnOutput(self, "GatewayURL",
            value=f"http://{instance.attr_public_ip}:18789",
            description="OpenClaw Gateway base URL (append ?token=<value from SSM>)",
        )
        CfnOutput(self, "GatewayTokenSSMPath",
            value=f"/openclaw/{Stack.of(self).stack_name}/gateway-token",
            description="SSM SecureString path — get token from SSM Parameter Store console",
        )
        CfnOutput(self, "ECRRepositoryUri",
            value=ecr_repo.repository_uri,
            description="ECR repository URI for agent container",
        )
        CfnOutput(self, "AgentCoreExecutionRoleArn",
            value=execution_role.role_arn,
            description="AgentCore execution role ARN",
        )
        CfnOutput(self, "WorkspaceBucketName",
            value=workspace_bucket.bucket_name,
            description="S3 bucket for tenant workspaces",
        )
        CfnOutput(self, "SetupLog",
            value=f"ssh ubuntu@{instance.attr_public_ip} tail -f /var/log/openclaw-setup.log",
            description="Command to tail setup log (if SSH key configured)",
        )
