# agent.py

import re
import uuid
import boto3
import pandas as pd
import datetime
from io import StringIO, BytesIO
from strands import Agent, tool

from athena_helper import run_query as athena_query, sync_table_from_file
from logger import (setup_logging, log_query, log_response,
                    log_guardrail, log_error, archive_logs_to_s3)
from config import (BUCKET, REGION, DATASETS, PANDAS_THRESHOLD,
                    MODEL_ID, ATHENA_DB, GUARDRAIL_ID, GUARDRAIL_VERSION)

s3 = boto3.client("s3", region_name=REGION)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION)

MAX_ROWS_IN_RESPONSE = 100

_athena_attempts: dict[str, int] = {}

# Per-session conversation history: { session_id: [{"question":..,"answer":..}] }
_sessions: dict[str, list[dict]] = {}

# Dynamic dataset registry — starts from config, grows with uploads
_dynamic_datasets: dict[str, str] = dict(DATASETS)  # name -> s3_key

# Per-session query history for the UI: { session_id: [{"q":..,"ts":..}] }
_query_history: dict[str, list[dict]] = {}


# ── Strip <thinking> tags ─────────────────────────────────────────────────────
def strip_thinking(text: str) -> str:
    """Removes <thinking>...</thinking> blocks from model output."""
    return re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL).strip()


# ── Guardrail Check ───────────────────────────────────────────────────────────
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
        action = response.get("action", "NONE")
        if action == "GUARDRAIL_INTERVENED":
            reason = str(response.get("assessments", "policy violation"))
            blocked_msg = (response["outputs"][0]["text"]
                           if response.get("outputs") else "⛔ Blocked by guardrail.")
            log_guardrail(direction, "BLOCKED", reason)
            return False, blocked_msg
        log_guardrail(direction, "ALLOWED")
        return True, text
    except Exception as e:
        log_error(str(e), context=f"Guardrail {direction} check")
        return True, text


# ── Utility: Multi-format loader ──────────────────────────────────────────────
def load_df(dataset_name: str) -> pd.DataFrame:
    """Loads CSV, Parquet, or JSON from S3 based on file extension."""
    key = _dynamic_datasets[dataset_name]
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    body = obj["Body"].read()
    ext = key.rsplit(".", 1)[-1].lower()

    if ext == "csv":
        return pd.read_csv(StringIO(body.decode("utf-8")))
    elif ext == "parquet":
        return pd.read_parquet(BytesIO(body))
    elif ext == "json":
        try:
            return pd.read_json(StringIO(body.decode("utf-8")))
        except ValueError:
            return pd.read_json(StringIO(body.decode("utf-8")), lines=True)
    else:
        raise ValueError(f"Unsupported file type: .{ext} for dataset '{dataset_name}'")


# ── Dynamic dataset registration ──────────────────────────────────────────────
def register_dataset(name: str, s3_key: str) -> None:
    """Registers a newly uploaded dataset so all tools can see it."""
    _dynamic_datasets[name] = s3_key


# ── Query history helpers ─────────────────────────────────────────────────────
def get_query_history(session_id: str) -> list[dict]:
    return _query_history.get(session_id, [])


def clear_conversation(session_id: str | None = None) -> None:
    if session_id:
        _sessions.pop(session_id, None)
        _query_history.pop(session_id, None)
    else:
        _sessions.clear()
        _query_history.clear()


# ── Tools ─────────────────────────────────────────────────────────────────────

@tool
def list_datasets() -> str:
    """Lists all available datasets (including user-uploaded ones) with row/column counts."""
    results = []
    for name in _dynamic_datasets:
        try:
            df = load_df(name)
            results.append(f"• {name}: {len(df):,} rows, {len(df.columns)} columns")
        except Exception as e:
            results.append(f"• {name}: (error loading — {e})")
    return "Available datasets:\n" + "\n".join(results)


@tool
def get_schema(dataset_name: str) -> str:
    """Returns column names, data types, and 3 sample rows.
    ALWAYS call this first before answering any question about a dataset.

    Args:
        dataset_name: one of the available dataset names
    """
    if dataset_name not in _dynamic_datasets:
        return f"Unknown dataset '{dataset_name}'. Available: {list(_dynamic_datasets.keys())}"
    df = load_df(dataset_name)
    schema = f"Dataset: {dataset_name} | {len(df):,} rows\nColumns & types:\n"
    for col in df.columns:
        schema += f"  • {col} ({df[col].dtype})\n"
    schema += f"\nSample (3 rows):\n{df.head(3).to_string(index=False)}"
    return schema


@tool
def pandas_query(dataset_name: str, question: str) -> str:
    """Answers questions using pandas on the full in-memory dataset.
    Supports CSV, Parquet, and JSON datasets.
    Use for: overviews, row-level filters, statistics, any dataset under PANDAS_THRESHOLD rows.

    Args:
        dataset_name: Name of the dataset
        question: The specific question to answer about the data
    """
    if dataset_name not in _dynamic_datasets:
        return f"Unknown dataset '{dataset_name}'."
    df = load_df(dataset_name)
    if len(df) > PANDAS_THRESHOLD:
        return (f"⚠️ Dataset has {len(df):,} rows — "
                f"use athena_sql_query for accurate results on this dataset.")
    context = f"Dataset: {dataset_name} | {len(df):,} rows\n"
    context += f"Columns: {list(df.columns)}\n"
    context += f"Statistical summary:\n{df.describe(include='all').to_string()}\n"
    context += f"First 20 rows:\n{df.head(20).to_string(index=False)}\n"
    context += f"\nAnswer this question using the data above: {question}"
    return context


@tool
def get_athena_table_info() -> str:
    """Returns exact table names and column names registered in Athena (all datasets)."""
    info = f"Athena Database: {ATHENA_DB}\nTables:\n"
    for name in _dynamic_datasets:
        try:
            df = load_df(name)
            cols = ", ".join(df.columns.tolist())
            info += f"\n• {name}\n  Columns: {cols}\n"
        except Exception as e:
            info += f"\n• {name}\n  (error: {e})\n"
    return info


@tool
def athena_sql_query(sql: str) -> str:
    """Runs a SQL SELECT query on Athena against S3 data.
    Use for: aggregations (COUNT/SUM/AVG/GROUP BY), TOP N ranking, large dataset filters.

    IMPORTANT RULES:
    - If this returns STATUS: ERROR, call get_athena_table_info, fix the SQL, then retry ONCE.
    - Never call this tool more than 2 times per table for the same user question.

    Args:
        sql: A valid SQL SELECT query
    """
    table_match = re.search(r'\bfrom\s+([a-zA-Z_][\w]*)', sql, re.IGNORECASE)
    table_key = table_match.group(1).lower() if table_match else "unknown"

    current_count = _athena_attempts.get(table_key, 0)
    if current_count >= 2:
        return (
            "ATHENA_RESULT:\nSTATUS: LIMIT_REACHED\n"
            f"TABLE: {table_key}\n"
            "MESSAGE: Athena query limit reached. You MUST now use pandas_query."
        )
    _athena_attempts[table_key] = current_count + 1

    try:
        df = athena_query(sql)
        if df.empty:
            return ("ATHENA_RESULT:\nSTATUS: EMPTY\n"
                    f"SQL: {sql}\nMESSAGE: Query returned no rows.")
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


# ── System Prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a friendly and accurate data analyst agent.
You have access to datasets stored in AWS S3 and answer questions in plain English.
Datasets include CSV, Parquet, and JSON files.

TOOLS AND WHEN TO USE THEM:
list_datasets → user asks what data is available
get_schema → ALWAYS call first on any question to understand columns and sample data
pandas_query → row-level filters, overviews, statistics, or datasets under PANDAS_THRESHOLD rows
get_athena_table_info → ALWAYS call before writing SQL to confirm exact column names
athena_sql_query → aggregations (COUNT/SUM/AVG/GROUP BY), sorting, TOP N on large datasets

CONVERSATION MEMORY:
- A summary of recent conversation turns may appear after the question.
- Use that context ONLY if the new question references prior results.
- If the new question is independent, answer it fresh.

STRICT WORKFLOW FOR EVERY QUESTION:
Step 1: call get_schema to understand the dataset columns and types
Step 2: pick the right tool (pandas_query for small/filter, athena_sql_query for aggregations)
Step 3: on Athena error → call get_athena_table_info, fix SQL, retry ONCE
Step 4: on LIMIT_REACHED or repeated error → use pandas_query
Step 5: NEVER dump full tables; always summarize, limit to ~20 rows
Step 6: Respond in plain English — no code, no SQL, no jargon

RESPONSE STYLE:
- For tables: summarize and show only a small sample (~20 rows max).
- For stats: give clear plain-English summary with key numbers.
- For errors: explain simply and try pandas_query as fallback.
- Never mention tool names, AWS, Athena, or pandas to the user.
- Never include <thinking> or any internal reasoning in your response.
"""

# ── Agent ─────────────────────────────────────────────────────────────────────
agent = Agent(
    model=MODEL_ID,
    system_prompt=SYSTEM_PROMPT,
    tools=[list_datasets, get_schema, pandas_query,
           get_athena_table_info, athena_sql_query],
)


# ── Web-facing run_query with session memory ──────────────────────────────────
def run_query(question: str, session_id: str | None = None) -> str:
    """Used by server.py. Supports per-session multi-turn memory."""
    if session_id is None:
        session_id = "default"

    _athena_attempts.clear()

    if not question.strip():
        return "Please ask a non-empty question."

    try:
        is_safe, checked_input = check_guardrail(question, "INPUT")
        if not is_safe:
            return checked_input

        log_query(question)

        # Build memory context from last 5 turns of THIS session
        history = _sessions.get(session_id, [])
        memory_context = ""
        if history:
            recent = history[-5:]
            memory_context = "\n\nConversation so far:\n"
            for turn in recent:
                memory_context += f"User: {turn['question']}\nAgent: {turn['answer']}\n"
            memory_context += "\nUse this context only if it is relevant to the new question."

        full_prompt = checked_input + memory_context
        result = agent(full_prompt)
        response_text = strip_thinking(str(result))

        is_safe, final_output = check_guardrail(response_text, "OUTPUT")
        if not is_safe:
            log_response(question, "BLOCKED_BY_OUTPUT_GUARDRAIL", "GUARDRAIL")
            return final_output

        mode = "ATHENA" if "ATHENA_RESULT" in response_text else "PANDAS"
        log_response(question, response_text, mode)

        # Store in per-session conversation memory
        if session_id not in _sessions:
            _sessions[session_id] = []
        _sessions[session_id].append({"question": question, "answer": final_output})

        # Store in per-session query history for UI panel
        if session_id not in _query_history:
            _query_history[session_id] = []
        _query_history[session_id].append({
            "q": question,
            "ts": datetime.datetime.utcnow().strftime("%H:%M:%S")
        })

        return final_output

    except Exception as e:
        log_error(str(e), context=f"run_query input: {question}")
        return f"[Error] {e}"


# ── Terminal loop ─────────────────────────────────────────────────────────────
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
            result = run_query(user_input, session_id=session_id)
            print(result)

        except KeyboardInterrupt:
            archive_logs_to_s3()
            print("\nGoodbye!")
            break
        except Exception as e:
            log_error(str(e), context=f"Main loop — input: {user_input}")
            print(f"\n[Error] {e}")


if __name__ == "__main__":
    run()