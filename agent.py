# output/agent.py
import re
import json
import ast
import boto3
import pandas as pd
import datetime
from io import StringIO, BytesIO
from strands import Agent, tool

from athena_helper import run_query as athena_query
from logger import (
    setup_logging, log_query, log_response,
    log_guardrail, log_error, archive_logs_to_s3
)
from config import (
    BUCKET, REGION, DATASETS, PANDAS_THRESHOLD,
    MODEL_ID, ATHENA_DB, GUARDRAIL_ID, GUARDRAIL_VERSION
)

s3 = boto3.client("s3", region_name=REGION)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION)

MAX_ROWS_IN_RESPONSE = 100
_athena_attempts: dict[str, int] = {}
_conversation_history: list[dict] = []
_dynamic_datasets: dict[str, str] = {}
_query_history: dict[str, list[dict]] = {}

def refresh_datasets() -> dict[str, str]:
    current = dict(DATASETS)
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET, Prefix="datasets/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                ext = key.rsplit(".", 1)[-1].lower()
                if ext not in ("csv", "parquet", "json"):
                    continue
                filename = key.split("/")[-1]
                name = filename.rsplit(".", 1)[0].lower().replace("-", "_").replace(" ", "_")
                current[name] = key
    except Exception as e:
        print(f"⚠️ Dataset refresh failed: {e}")
    _dynamic_datasets.clear()
    _dynamic_datasets.update(current)
    return dict(_dynamic_datasets)

def get_current_datasets() -> dict[str, str]:
    if not _dynamic_datasets:
        refresh_datasets()
    return dict(_dynamic_datasets)

def _init_datasets() -> None:
    refresh_datasets()
    print(f"✅ Datasets loaded: {list(_dynamic_datasets.keys())}")

_init_datasets()

def strip_thinking(text: str) -> str:
    return re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL).strip()

def check_guardrail(text: str, direction: str) -> tuple[bool, str]:
    if not GUARDRAIL_ID:
        return True, text
    try:
        response = bedrock_runtime.apply_guardrail(
            guardrailIdentifier=GUARDRAIL_ID,
            guardrailVersion=GUARDRAIL_VERSION,
            source=direction,
            content=[{"text": {"text": text}}]
        )
        if response.get("action") == "GUARDRAIL_INTERVENED":
            blocked_msg = response["outputs"][0]["text"] if response.get("outputs") else "⛔ Blocked by guardrail."
            log_guardrail(direction, "BLOCKED", str(response.get("assessments", "policy violation")))
            return False, blocked_msg
        log_guardrail(direction, "ALLOWED")
        return True, text
    except Exception as e:
        log_error(str(e), context=f"Guardrail {direction} check")
        return True, text

def load_df(dataset_name: str) -> pd.DataFrame:
    datasets = refresh_datasets()
    if dataset_name not in datasets:
        raise KeyError(f"Unknown dataset '{dataset_name}'")
    key = datasets[dataset_name]
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    body = obj["Body"].read()
    ext = key.rsplit(".", 1)[-1].lower()

    if ext == "csv":
        return pd.read_csv(StringIO(body.decode("utf-8")))
    if ext == "parquet":
        return pd.read_parquet(BytesIO(body))
    if ext == "json":
        decoded = body.decode("utf-8").strip()
        try:
            parsed = json.loads(decoded)
            if isinstance(parsed, dict):
                return pd.DataFrame([parsed])
            if isinstance(parsed, list):
                return pd.DataFrame(parsed)
        except Exception:
            pass
        try:
            return pd.read_json(StringIO(decoded), lines=True)
        except Exception:
            pass
        try:
            parsed = ast.literal_eval(decoded)
            if isinstance(parsed, dict):
                return pd.DataFrame([parsed])
            if isinstance(parsed, list):
                return pd.DataFrame(parsed)
        except Exception:
            pass
        raise ValueError(f"Could not parse JSON dataset '{dataset_name}'")

    raise ValueError(f"Unsupported file type: .{ext} for dataset '{dataset_name}'")

def register_dataset(name: str, s3_key: str) -> None:
    _dynamic_datasets[name] = s3_key

def unregister_dataset(name: str) -> None:
    _dynamic_datasets.pop(name, None)

def get_query_history(session_id: str) -> list[dict]:
    return _query_history.get(session_id, [])

def clear_conversation(session_id: str | None = None) -> None:
    if session_id:
        _conversation_history.clear()
        _query_history.pop(session_id, None)
    else:
        _conversation_history.clear()
        _query_history.clear()

@tool
def list_datasets() -> str:
    # Always refresh from S3 before listing so additions/deletions are reflected
    datasets = refresh_datasets()
    results = []
    for name in sorted(datasets.keys()):
        try:
            df = load_df(name)
            results.append(f"• {name}: {len(df):,} rows, {len(df.columns)} columns")
        except Exception as e:
            results.append(f"• {name}: Error loading — {e}")
    return "Available datasets:\n" + "\n".join(results)

@tool
def get_schema(dataset_name: str) -> str:
    datasets = refresh_datasets()
    if dataset_name not in datasets:
        return f"Unknown dataset '{dataset_name}'. Available: {list(datasets.keys())}"
    df = load_df(dataset_name)
    schema = f"Dataset: {dataset_name} | {len(df):,} rows\nColumns & types:\n"
    for col in df.columns:
        schema += f"  • {col} ({df[col].dtype})\n"
    schema += f"\nSample (3 rows):\n{df.head(3).to_string(index=False)}"
    return schema

@tool
def pandas_query(dataset_name: str, question: str) -> str:
    datasets = refresh_datasets()
    if dataset_name not in datasets:
        return f"Unknown dataset '{dataset_name}'."
    df = load_df(dataset_name)
    if len(df) > PANDAS_THRESHOLD:
        return f"⚠️ Dataset has {len(df):,} rows — use athena_sql_query for accurate results on this dataset."
    context = f"Dataset: {dataset_name} | {len(df):,} rows\n"
    context += f"Columns: {list(df.columns)}\n"
    context += f"Statistical summary:\n{df.describe(include='all').to_string()}\n"
    context += f"First 20 rows:\n{df.head(20).to_string(index=False)}\n"
    context += f"\nAnswer this question using the data above: {question}"
    return context

@tool
def get_athena_table_info() -> str:
    datasets = refresh_datasets()
    info = f"Athena Database: {ATHENA_DB}\nTables:\n"
    for name in sorted(datasets.keys()):
        try:
            df = load_df(name)
            cols = ", ".join(df.columns.tolist())
            info += f"\n• {name}\n  Columns: {cols}\n"
        except Exception as e:
            info += f"\n• {name}\n  (error: {e})\n"
    return info

@tool
def athena_sql_query(sql: str) -> str:
    table_match = re.search(r'\bfrom\s+([a-zA-Z_][\w]*)', sql, re.IGNORECASE)
    table_key = table_match.group(1).lower() if table_match else "unknown"

    current_count = _athena_attempts.get(table_key, 0)
    if current_count >= 2:
        return (
            "ATHENA_RESULT:\nSTATUS: LIMIT_REACHED\n"
            f"TABLE: {table_key}\n"
            "MESSAGE: Athena query limit reached. Use pandas_query now."
        )

    _athena_attempts[table_key] = current_count + 1

    try:
        df = athena_query(sql)
        if df.empty:
            return "ATHENA_RESULT:\nSTATUS: EMPTY\n" f"SQL: {sql}\nMESSAGE: Query returned no rows."

        total_rows = len(df)
        truncated = total_rows > MAX_ROWS_IN_RESPONSE
        display_df = df.head(MAX_ROWS_IN_RESPONSE) if truncated else df
        note = f"\n[Showing first {MAX_ROWS_IN_RESPONSE} of {total_rows} rows]" if truncated else ""
        _athena_attempts[table_key] = 0

        return (
            "ATHENA_RESULT:\nSTATUS: OK\n"
            f"ROWS: {total_rows}\nSQL: {sql}\n"
            f"DATA:\n{display_df.to_string(index=False)}{note}"
        )
    except Exception as e:
        log_error(str(e), context=f"Athena SQL: {sql}")
        return (
            "ATHENA_RESULT:\nSTATUS: ERROR\n"
            f"SQL: {sql}\nERROR: {str(e)}\n"
            "ACTION: Call get_athena_table_info once, fix the SQL, and retry ONCE."
        )

SYSTEM_PROMPT = """You are a helpful assistant for both dataset questions and general questions.

Rules:
- If the user asks about datasets, use the tools.
- If the user asks a general question, answer normally.
- Do not mention tool names to the user.
- Keep answers short and direct when possible."""

# ── REMOVED: module-level singleton agent ─────────────────────────────────────
# The old `agent = Agent(...)` here was the bug. A singleton Agent accumulates
# conversation history and can answer "from memory" without re-invoking tools,
# meaning newly registered/deleted datasets were invisible to the LLM response.

def _make_agent() -> Agent:
    """Create a fresh Agent instance with up-to-date tool bindings."""
    return Agent(
        model=MODEL_ID,
        system_prompt=SYSTEM_PROMPT,
        tools=[list_datasets, get_schema, pandas_query, get_athena_table_info, athena_sql_query],
    )

def run_query(question: str, session_id: str | None = None) -> str:
    if session_id is None:
        session_id = "default"

    _athena_attempts.clear()
    # Always pull the latest dataset list from S3 before every query
    refresh_datasets()

    if not question.strip():
        return "Please ask a non-empty question."

    try:
        is_safe, checked_input = check_guardrail(question, "INPUT")
        if not is_safe:
            return checked_input

        log_query(question)

        # ── KEY FIX: fresh Agent per query so no stale internal state ──────────
        agent = _make_agent()
        result = agent(checked_input)
        response_text = strip_thinking(str(result))

        is_safe, final_output = check_guardrail(response_text, "OUTPUT")
        if not is_safe:
            log_response(question, "BLOCKED_BY_OUTPUT_GUARDRAIL", "GUARDRAIL")
            return final_output

        mode = "ATHENA" if "ATHENA_RESULT" in response_text else "PANDAS"
        log_response(question, response_text, mode)

        _conversation_history.append({"question": question, "answer": final_output})
        _query_history.setdefault(session_id, []).append(
            {"q": question, "ts": datetime.datetime.utcnow().strftime("%H:%M:%S")}
        )

        return final_output

    except Exception as e:
        log_error(str(e), context=f"run_query input: {question}")
        return f"[Error] {e}"

def run():
    setup_logging()
    print("=" * 62)
    print(" 🥕🎵 NL Data Query Agent")
    print(" Bedrock + Strands + Athena + Guardrails + CloudWatch")
    print("=" * 62)

    session_id = "terminal"
    while True:
        try:
            user_input = input("\nYou: ").strip()
            _athena_attempts.clear()

            if user_input.lower() in ("quit", "exit", "q"):
                archive_logs_to_s3()
                print("Goodbye! Logs archived to S3. ✅")
                break
            if user_input.lower() == "archive":
                archive_logs_to_s3()
                continue
            if not user_input:
                continue

            print("\nAgent: ", end="", flush=True)
            print(run_query(user_input, session_id=session_id))

        except KeyboardInterrupt:
            archive_logs_to_s3()
            print("\nGoodbye!")
            break
        except Exception as e:
            log_error(str(e), context=f"Main loop — input: {user_input}")
            print(f"\n[Error] {e}")

if __name__ == "__main__":
    run()