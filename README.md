# OpenClaw on AWS Bedrock AgentCore — Lab

Deploy OpenClaw as a multi-tenant AI platform on AWS Bedrock AgentCore with a single CloudFormation template. Lab users only need a browser — no local tools required.

## Architecture

```
Browser → OpenClaw Gateway (EC2, port 18789)
            └── Bedrock H2 Proxy (port 8091)
                  └── Tenant Router (port 8090)
                        └── AgentCore Runtime (Firecracker microVM per user)
                              └── Bedrock Nova / Claude
```

Each user gets an isolated Firecracker microVM via AgentCore Runtime. The EC2 instance hosts the gateway and routing layer; the agent container runs serverlessly inside AgentCore.

## Deploy Flow

The deployment is split into two layers:

1. **`template.yaml`** (hand-crafted CFN) — deploys base infrastructure: VPC, EC2, IAM roles, ECR, S3, DynamoDB. Upload this to CloudFormation and you're done — no local tools needed.

2. **EC2 UserData** — once the instance boots, it automatically:
   - Installs Docker, Node.js, AWS CDK
   - Clones this repo
   - Runs `cdk deploy AppStack` to deploy Lambda functions and configure CloudWatch
   - Builds and pushes the agent container to ECR
   - Creates the AgentCore Runtime
   - Starts all services

### Prerequisites

- AWS account with Bedrock model access enabled ([enable here](https://console.aws.amazon.com/bedrock/home#/modelaccess))
- Bedrock AgentCore available in your region (us-west-2 recommended)

### Step 1 — Deploy via AWS Console

1. Go to **CloudFormation → Create stack → With new resources**
2. Upload `template.yaml` from this repo
3. Set parameters (defaults work for most labs):

| Parameter | Default | Notes |
|---|---|---|
| `OpenClawModel` | `global.amazon.nova-2-lite-v1:0` | Bedrock model ID |
| `InstanceType` | `c7g.large` | Graviton ARM64 recommended |
| `RepoUrl` | *(this repo)* | Leave as-is |
| `AllowedCIDR` | `0.0.0.0/0` | Restrict to your IP for production |

4. Acknowledge IAM capabilities → **Create stack**
5. Wait ~35–45 minutes for the stack to reach `CREATE_COMPLETE`

> The EC2 UserData automatically: installs Docker + Node.js + CDK, clones the repo, runs `cdk deploy AppStack`, builds the agent container, pushes to ECR, creates the AgentCore Runtime, and starts all services.

### Step 2 — Access the Gateway

1. Go to **CloudFormation → Outputs** tab
2. Copy the `GatewayURL` value (e.g. `http://1.2.3.4:18789`)
3. Go to **SSM Parameter Store** → find `/openclaw/<stack-name>/gateway-token` → **Show decrypted value**
4. Open `http://<GatewayURL>?token=<token>` in your browser

That's it — OpenClaw is running.

## What Gets Deployed

### Via CloudFormation (`template.yaml`)

| Resource | Details |
|---|---|
| EC2 (c7g.large) | OpenClaw Gateway + Bedrock H2 Proxy + Tenant Router |
| ECR repository | Agent container image |
| S3 bucket | Per-tenant workspace sync (SOUL.md, MEMORY.md) |
| DynamoDB (2 tables) | Identity/session tracking + token usage records |
| IAM roles | EC2 instance role + AgentCore execution role |

### Via CDK (`AppStack`, deployed from EC2)

| Resource | Details |
|---|---|
| AgentCore Runtime | Serverless Firecracker microVM per tenant |
| Lambda (2 functions) | Token metrics + cron executor |
| EventBridge Scheduler | Scheduled task support |
| CloudWatch | Bedrock invocation logs + token usage dashboard |

## Cost Estimate

| Component | ~Cost/month |
|---|---|
| EC2 c7g.large | ~$50 |
| EBS 30GB gp3 | ~$2.40 |
| AgentCore Runtime | Pay-per-invocation |
| Bedrock Nova 2 Lite | $0.30/$2.50 per 1M tokens |
| Other (S3, DynamoDB, Lambda) | ~$2 |

**Total for a lab**: ~$55/month + usage. Terminate the stack when done.

## Cleanup

```bash
# Delete AgentCore Runtime first (not managed by CFN)
RUNTIME_ID=$(aws ssm get-parameter \
  --name "/openclaw/<stack-name>/runtime-id" \
  --query Parameter.Value --output text --region us-west-2)
aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id $RUNTIME_ID --region us-west-2

# Delete the CDK AppStack
aws cloudformation delete-stack --stack-name AppStack --region us-west-2

# Then delete the base stack
aws cloudformation delete-stack --stack-name <stack-name> --region us-west-2
```

Or delete via the AWS Console (CloudFormation → Delete).

> Note: S3 bucket and DynamoDB tables have `DeletionPolicy: Retain` — delete them manually if needed.

## Repo Structure

```
template.yaml             # Hand-crafted CFN: base infra (VPC, EC2, IAM, ECR, S3, DynamoDB)
stacks/app_stack.py       # CDK stack: Lambda + CloudWatch (deployed from EC2 UserData)
src/gateway/              # Bedrock H2 Proxy + Tenant Router (runs on EC2)
bridge/                   # Node.js bridge (runs inside agent container)
lambda/token_metrics/     # Token usage tracking Lambda
lambda/cron/              # Scheduled task executor Lambda
```

## Source

Built by merging:
- [sample-host-openclaw-on-amazon-bedrock-agentcore](https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore) — CDK patterns, bridge, token monitoring
- [sample-OpenClaw-on-AWS-with-Bedrock](https://github.com/aws-samples/sample-OpenClaw-on-AWS-with-Bedrock) — EC2 gateway, agent container, multi-tenant routing
