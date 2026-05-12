# logger.py
# DO NOT RUN DIRECTLY — imported automatically by agent.py

import boto3
import json
import time
import datetime
from config import REGION, CW_LOG_GROUP, CW_LOG_STREAM, BUCKET, LOG_ARCHIVE_PREFIX

logs = boto3.client("logs", region_name=REGION)
s3   = boto3.client("s3",   region_name=REGION)

_sequence_token = None


def setup_logging():
    global _sequence_token
    try:
        logs.create_log_group(logGroupName=CW_LOG_GROUP)
        logs.put_retention_policy(logGroupName=CW_LOG_GROUP, retentionInDays=90)
        print(f"✅ CloudWatch log group created: {CW_LOG_GROUP} (90-day retention)")
    except logs.exceptions.ResourceAlreadyExistsException:
        print(f"ℹ️  CloudWatch log group exists: {CW_LOG_GROUP}")

    try:
        logs.create_log_stream(logGroupName=CW_LOG_GROUP, logStreamName=CW_LOG_STREAM)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass


def log_event(event_type: str, data: dict):
    global _sequence_token

    payload = {
        "timestamp":  datetime.datetime.utcnow().isoformat() + "Z",
        "event_type": event_type,
        **data
    }

    kwargs = {
        "logGroupName":  CW_LOG_GROUP,
        "logStreamName": CW_LOG_STREAM,
        "logEvents":     [{"timestamp": int(time.time() * 1000), "message": json.dumps(payload)}],
    }
    if _sequence_token:
        kwargs["sequenceToken"] = _sequence_token

    try:
        response        = logs.put_log_events(**kwargs)
        _sequence_token = response.get("nextSequenceToken")
    except logs.exceptions.InvalidSequenceTokenException as e:
        _sequence_token         = str(e).split("The next expected sequenceToken is: ")[-1].strip()
        kwargs["sequenceToken"] = _sequence_token
        response                = logs.put_log_events(**kwargs)
        _sequence_token         = response.get("nextSequenceToken")
    except Exception as e:
        print(f"[Logger Warning] CloudWatch write failed: {e}")


def log_query(question: str):
    log_event("USER_QUERY", {"question": question})

def log_response(question: str, response: str, mode: str):
    log_event("AGENT_RESPONSE", {
        "question":         question,
        "response_preview": response[:500],
        "query_mode":       mode,
    })

def log_guardrail(direction: str, action: str, reason: str = ""):
    log_event("GUARDRAIL_EVENT", {
        "direction": direction,
        "action":    action,
        "reason":    reason,
    })

def log_error(error: str, context: str = ""):
    log_event("ERROR", {"error": str(error), "context": context})


def archive_logs_to_s3():
    """Fetches all CloudWatch logs and saves to S3 as NDJSON.
    Called automatically when you type 'quit' or 'archive' in agent.py.
    """
    today  = datetime.datetime.utcnow()
    s3_key = f"{LOG_ARCHIVE_PREFIX}/{today.strftime('%Y/%m/%d')}/agent-session-{int(time.time())}.json"

    events = []
    kwargs = {
        "logGroupName":  CW_LOG_GROUP,
        "logStreamName": CW_LOG_STREAM,
        "limit":         10000,
    }

    prev_token = None
    while True:
        response   = logs.get_log_events(**kwargs)
        batch      = response.get("events", [])
        for e in batch:
            try:
                events.append(json.loads(e["message"]))
            except json.JSONDecodeError:
                events.append({"raw": e["message"]})
        next_token = response.get("nextForwardToken")
        if not batch or next_token == prev_token:
            break
        prev_token          = next_token
        kwargs["nextToken"] = next_token

    if not events:
        print("ℹ️  No logs to archive.")
        return

    ndjson = "\n".join(json.dumps(e) for e in events)
    s3.put_object(
        Bucket      = BUCKET,
        Key         = s3_key,
        Body        = ndjson.encode("utf-8"),
        ContentType = "application/json"
    )
    print(f"✅ {len(events)} log events archived → s3://{BUCKET}/{s3_key}")