"""Cron Executor Lambda — Triggered by EventBridge Scheduler.

Receives a scheduled event, invokes the user's AgentCore session,
and logs the response to CloudWatch (no channel delivery).
"""
import json
import logging
import os
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENTCORE_RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
AGENTCORE_QUALIFIER = os.environ["AGENTCORE_QUALIFIER"]
IDENTITY_TABLE_NAME = os.environ["IDENTITY_TABLE_NAME"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
LAMBDA_TIMEOUT_SECONDS = int(os.environ.get("LAMBDA_TIMEOUT_SECONDS", "600"))

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)
agentcore_client = boto3.client(
    "bedrock-agentcore",
    region_name=AWS_REGION,
    config=Config(
        read_timeout=max(LAMBDA_TIMEOUT_SECONDS - 30, 60),
        connect_timeout=10,
        retries={"max_attempts": 0},
    ),
)

WARMUP_POLL_INTERVAL_SECONDS = 15
WARMUP_MAX_WAIT_SECONDS = 300


def get_or_create_session(user_id):
    pk = f"USER#{user_id}"
    try:
        resp = identity_table.get_item(Key={"PK": pk, "SK": "SESSION"})
        if "Item" in resp:
            identity_table.update_item(
                Key={"PK": pk, "SK": "SESSION"},
                UpdateExpression="SET lastActivity = :now",
                ExpressionAttributeValues={":now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            )
            return resp["Item"]["sessionId"]
    except ClientError as e:
        logger.error("DynamoDB session lookup failed: %s", e)

    session_id = f"ses_{user_id}_{uuid.uuid4().hex[:12]}"
    while len(session_id) < 33:
        session_id += "_" + uuid.uuid4().hex[:33 - len(session_id)]
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        identity_table.put_item(Item={
            "PK": pk, "SK": "SESSION",
            "sessionId": session_id,
            "createdAt": now_iso, "lastActivity": now_iso,
        })
    except ClientError as e:
        logger.error("Failed to create session: %s", e)
    return session_id


def invoke_agentcore(session_id, action, user_id, actor_id, message=None):
    payload_dict = {"action": action, "userId": user_id, "actorId": actor_id, "channel": "lab"}
    if message:
        payload_dict["message"] = message
    try:
        resp = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            qualifier=AGENTCORE_QUALIFIER,
            runtimeSessionId=session_id,
            runtimeUserId=actor_id,
            payload=json.dumps(payload_dict).encode(),
            contentType="application/json",
            accept="application/json",
        )
        body = resp.get("response")
        if body:
            body_bytes = body.read(500_001) if hasattr(body, "read") else str(body).encode()
            body_text = body_bytes.decode("utf-8", errors="replace")[:500_000]
            try:
                return json.loads(body_text)
            except json.JSONDecodeError:
                return {"response": body_text}
        return {"response": "No response from agent."}
    except Exception as e:
        logger.error("AgentCore invocation failed: %s", e, exc_info=True)
        return {"response": f"Agent invocation failed: {e}"}


def warmup_and_wait(session_id, user_id, actor_id):
    start = time.time()
    while time.time() - start < WARMUP_MAX_WAIT_SECONDS:
        result = invoke_agentcore(session_id, "warmup", user_id, actor_id)
        status = result.get("status", "")
        logger.info("Warmup status: %s (elapsed: %.0fs)", status, time.time() - start)
        if status == "ready":
            return True
        if status != "initializing":
            return True
        time.sleep(WARMUP_POLL_INTERVAL_SECONDS)
    logger.error("Warmup timed out after %ds", WARMUP_MAX_WAIT_SECONDS)
    return False


def handler(event, context):
    """Handle EventBridge Scheduler trigger.

    Expected payload:
    {
        "userId": "user_abc123",
        "actorId": "lab:user_abc123",
        "message": "Check my tasks",
        "scheduleId": "a1b2c3d4",
        "scheduleName": "Daily check"
    }
    Response is logged to CloudWatch (no channel delivery in lab mode).
    """
    logger.info("Cron event: %s", json.dumps(event)[:500])

    user_id = event.get("userId")
    actor_id = event.get("actorId")
    message = event.get("message")
    schedule_id = event.get("scheduleId", "unknown")
    schedule_name = event.get("scheduleName", "")

    if not all([user_id, actor_id, message]):
        logger.error("Missing required fields: userId=%s actorId=%s msg=%s",
                     user_id, actor_id, bool(message))
        return {"statusCode": 400, "body": "Missing required fields"}

    # Verify schedule ownership
    try:
        cron_record = identity_table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": f"CRON#{schedule_id}"}
        ).get("Item")
        if not cron_record:
            logger.error("Schedule %s not owned by user %s", schedule_id, user_id)
            return {"statusCode": 403, "body": "Schedule ownership verification failed"}
    except Exception as e:
        logger.error("Failed to verify schedule ownership: %s", e)
        return {"statusCode": 500, "body": "Schedule ownership verification error"}

    session_id = get_or_create_session(user_id)

    if not warmup_and_wait(session_id, user_id, actor_id):
        logger.error("Warmup failed for schedule %s", schedule_id)
        return {"statusCode": 503, "body": "Warmup timeout"}

    cron_message = f"[Scheduled: {schedule_name or schedule_id}] {message}"
    result = invoke_agentcore(session_id, "cron", user_id, actor_id, cron_message)
    response_text = result.get("response", "No response.")

    # Lab mode: log response to CloudWatch instead of delivering to a channel
    logger.info("Cron response for schedule=%s user=%s (len=%d): %s",
                schedule_id, user_id, len(response_text), response_text[:500])

    return {"statusCode": 200, "body": "OK"}
