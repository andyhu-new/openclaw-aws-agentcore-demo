# AgentCore Runtime Contract

The bridge container (`bridge/agentcore-contract.js`) runs inside an AgentCore Firecracker microVM and implements the required HTTP contract.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/ping` | Health check — returns `{"status":"Healthy"}` |
| POST | `/invocations` | All actions (chat, status, warmup, cron) |

## Invocation Payload

All requests go to `POST /invocations`. The `action` field determines behavior. If omitted, defaults to `"status"`.

### `chat` — Send a message

```json
{
  "action": "chat",
  "userId": "user123",
  "actorId": "user123",
  "channel": "webchat",
  "message": "say hello",
  "sessionId": "optional-session-id"
}
```

Required: `userId`, `actorId`, `message`

Response:
```json
{
  "response": "Hello! How can I help you?",
  "userId": "user123",
  "sessionId": "optional-session-id"
}
```

On init failure:
```json
{
  "response": "I'm having trouble starting up. Please try again in a moment.",
  "userId": "user123",
  "sessionId": null,
  "status": "error"
}
```

### `status` — Container diagnostics

```json
{
  "action": "status"
}
```

Response:
```json
{
  "response": "{\"buildVersion\":\"v35\",\"uptime_seconds\":46,\"currentUserId\":null,\"openclawReady\":false,\"proxyReady\":false,\"secretsReady\":true,...}"
}
```

Note: `response` is a JSON-encoded string.

### `warmup` — Trigger init without waiting for a chat response

```json
{
  "action": "warmup",
  "userId": "user123",
  "actorId": "user123",
  "channel": "webchat"
}
```

Response: `{"status":"ready"}` or `{"status":"initializing"}`

### `cron` — Scheduled task (blocks until init completes)

```json
{
  "action": "cron",
  "userId": "user123",
  "actorId": "user123",
  "channel": "webchat",
  "message": "run my scheduled task",
  "sessionId": "optional-session-id"
}
```

Required: `userId`, `actorId`, `message`

Response: same shape as `chat`.

## Testing via AWS CLI

`runtimeSessionId` must be ≥ 33 characters.

```bash
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "arn:aws:bedrock-agentcore:us-west-2:ACCOUNT:runtime/RUNTIME_ID" \
  --runtime-session-id "test-session-webchat-user1-abc123def456789" \
  --content-type "application/json" \
  --accept "application/json" \
  --payload '{"action":"chat","userId":"user1","actorId":"user1","channel":"webchat","message":"hello"}' \
  --region us-west-2 \
  output.json && cat output.json
```

## Environment Variables

The bridge container expects these env vars (set via `create-agent-runtime --environment-variables`):

| Variable | Required | Description |
|---|---|---|
| `AWS_REGION` | Yes | AWS region |
| `STACK_NAME` | Yes | CloudFormation stack name |
| `S3_BUCKET` | Yes | Tenant workspace S3 bucket |
| `BEDROCK_MODEL_ID` | Yes | Bedrock model ID |
| `GATEWAY_TOKEN_SECRET_ID` | Yes | Secrets Manager secret ARN/name for gateway token |
| `S3_USER_FILES_BUCKET` | Yes | S3 bucket for per-user file storage skill |
| `EXECUTION_ROLE_ARN` | Yes | AgentCore execution role ARN (for scoped S3 credentials) |
| `COGNITO_PASSWORD_SECRET_ID` | No | Secrets Manager secret for Cognito password |
| `COGNITO_USER_POOL_ID` | No | Cognito user pool ID |
| `COGNITO_CLIENT_ID` | No | Cognito client ID |
| `BROWSER_IDENTIFIER` | No | AgentCore browser session identifier |
| `SUBAGENT_BEDROCK_MODEL_ID` | No | Model ID for subagent routing |

## Architecture Flow

```
Browser → EC2 OpenClaw UI (18789)
            └─ Bedrock API → bedrock_proxy_h2.js (8091)
                  └─ tenant_router.py (8090)
                        └─ AgentCore Runtime API
                              └─ agentcore-contract.js (8080 in microVM)
                                    ├─ lightweight-agent.js (immediate, ~10s)
                                    └─ OpenClaw (full, ~1-2min startup)
                                          └─ Bedrock API (real)
```

The bridge uses a hybrid init strategy: the lightweight agent handles messages immediately while OpenClaw starts up inside the microVM. Once OpenClaw is ready, messages route through it via WebSocket.
