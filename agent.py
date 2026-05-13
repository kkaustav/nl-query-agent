# agent.py
# RUN THIS DAILY: python agent.py

import re
import boto3
import pandas as pd
from io import StringIO
from strands import Agent, tool

from athena_helper import run_query as athena_query
from logger import (setup_logging, log_query, log_response,
                    log_guardrail, log_error, archive_logs_to_s3)
from config import (BUCKET, REGION, DATASETS, PANDAS_THRESHOLD,
                    MODEL_ID, ATHENA_DB, GUARDRAIL_ID, GUARDRAIL_VERSION)

s3              = boto3.client("s3",              region_name=REGION)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION)

MAX_ROWS_IN_RESPONSE = 100   # cap rows returned by Athena to avoid max_tokens loops

# Tracks how many times Athena has been called per table for the current question
_athena_attempts: dict[str, int] = {}


# ── Guardrail Check ───────────────────────────────────────────────────────────
def check_guardrail(text: str, direction: str) -> tuple[bool, str]:
    """
    Checks text against the Bedrock Guardrail.
    direction = 'INPUT'  → checks the user's question before sending to agent
    direction = 'OUTPUT' → checks the agent's response before showing to user
    Returns (is_safe, message)
    """
    if not GUARDRAIL_ID:
        return True, text   # Skip if guardrail not yet configured

    try:
        response = bedrock_runtime.apply_guardrail(
            guardrailIdentifier = GUARDRAIL_ID,
            guardrailVersion    = GUARDRAIL_VERSION,
            source              = direction,
            content             = [{"text": {"text": text}}]
        )
        action = response.get("action", "NONE")

        if action == "GUARDRAIL_INTERVENED":
            reason      = str(response.get("assessments", "policy violation"))
            blocked_msg = (response["outputs"][0]["text"]
                           if response.get("outputs")
                           else "⛔ Blocked by guardrail.")
            log_guardrail(direction, "BLOCKED", reason)
            return False, blocked_msg

        log_guardrail(direction, "ALLOWED")
        return True, text

    except Exception as e:
        log_error(str(e), context=f"Guardrail {direction} check")
        return True, text   # Fail open — don't break the app on guardrail errors


# ── Utility ───────────────────────────────────────────────────────────────────
def load_df(dataset_name: str) -> pd.DataFrame:
    obj = s3.get_object(Bucket=BUCKET, Key=DATASETS[dataset_name])
    return pd.read_csv(StringIO(obj["Body"].read().decode("utf-8")))


# ── Tools ─────────────────────────────────────────────────────────────────────

@tool
def list_datasets() -> str:
    """Lists all available datasets with their row and column counts.
    Call this when the user asks what data is available.
    """
    results = []
    for name in DATASETS:
        df = load_df(name)
        results.append(f"• {name}: {len(df):,} rows, {len(df.columns)} columns")
    return "Available datasets:\n" + "\n".join(results)


@tool
def get_schema(dataset_name: str) -> str:
    """Returns column names, data types, and 3 sample rows.
    ALWAYS call this first before answering any question about a dataset.

    Args:
        dataset_name: one of the available dataset names e.g. 'spotify' or 'farmers_market'
    """
    if dataset_name not in DATASETS:
        return f"Unknown dataset '{dataset_name}'. Available: {list(DATASETS.keys())}"
    df     = load_df(dataset_name)
    schema = f"Dataset: {dataset_name} | {len(df):,} rows\nColumns & types:\n"
    for col in df.columns:
        schema += f"  • {col} ({df[col].dtype})\n"
    schema += f"\nSample (3 rows):\n{df.head(3).to_string(index=False)}"
    return schema


@tool
def pandas_query(dataset_name: str, question: str) -> str:
    """Answers questions using pandas on the full in-memory CSV.
    Use for: overviews, row-level filters, statistics, any dataset under PANDAS_THRESHOLD rows.
    Prefer this over athena_sql_query for small datasets or when Athena fails.

    Args:
        dataset_name: Name of the dataset
        question:     The specific question to answer about the data
    """
    if dataset_name not in DATASETS:
        return f"Unknown dataset '{dataset_name}'."
    df = load_df(dataset_name)

    if len(df) > PANDAS_THRESHOLD:
        return (f"⚠️ Dataset has {len(df):,} rows — "
                f"use athena_sql_query for accurate results on this dataset.")

    context  = f"Dataset: {dataset_name} | {len(df):,} rows\n"
    context += f"Columns: {list(df.columns)}\n"
    context += f"Statistical summary:\n{df.describe(include='all').to_string()}\n"
    context += f"First 20 rows:\n{df.head(20).to_string(index=False)}\n"
    context += f"\nAnswer this question using the data above: {question}"
    return context


@tool
def get_athena_table_info() -> str:
    """Returns the exact table names and column names registered in Athena.
    ALWAYS call this before writing any SQL query to get the exact column names.
    """
    info = f"Athena Database: {ATHENA_DB}\nTables:\n"
    for name in DATASETS:
        df   = load_df(name)
        cols = ", ".join(df.columns.tolist())
        info += f"\n• {name}\n  Columns: {cols}\n"
    return info


@tool
def athena_sql_query(sql: str) -> str:
    """Runs a SQL SELECT query on Athena against S3 data.
    Use for: aggregations (COUNT/SUM/AVG/GROUP BY), TOP N ranking, large dataset filters.
    Tables available: farmers_market, spotify.

    IMPORTANT RULES:
    - If this returns STATUS: ERROR, call get_athena_table_info, fix the SQL, then retry ONCE.
    - If it still returns STATUS: ERROR after the retry, STOP and use pandas_query instead.
    - Never call this tool more than 2 times per table for the same user question.

    Args:
        sql: A valid SQL SELECT query
    """
    # Extract table name from SQL to use as key
    table_match = re.search(r'\bfrom\s+([a-zA-Z_][\w]*)', sql, re.IGNORECASE)
    table_key   = table_match.group(1).lower() if table_match else "unknown"

    current_count = _athena_attempts.get(table_key, 0)
    if current_count >= 2:
        return (
            "ATHENA_RESULT:\n"
            "STATUS: LIMIT_REACHED\n"
            f"TABLE: {table_key}\n"
            "MESSAGE: Athena query limit reached for this table on this question (max 2 attempts). "
            "You MUST now use pandas_query to answer this question. Do NOT call athena_sql_query again."
        )

    _athena_attempts[table_key] = current_count + 1

    try:
        df = athena_query(sql)

        if df.empty:
            return (
                "ATHENA_RESULT:\n"
                "STATUS: EMPTY\n"
                f"SQL: {sql}\n"
                "MESSAGE: Query returned no rows. Try broadening your filter or checking exact values."
            )

        total_rows = len(df)
        truncated  = total_rows > MAX_ROWS_IN_RESPONSE
        display_df = df.head(MAX_ROWS_IN_RESPONSE) if truncated else df
        note       = f"\n[Showing first {MAX_ROWS_IN_RESPONSE} of {total_rows} rows]" if truncated else ""

        # On success we can reset the per-table counter for this question
        _athena_attempts[table_key] = 0

        return (
            "ATHENA_RESULT:\n"
            "STATUS: OK\n"
            f"ROWS: {total_rows}\n"
            f"SQL: {sql}\n"
            f"DATA:\n{display_df.to_string(index=False)}"
            f"{note}"
        )

    except Exception as e:
        log_error(str(e), context=f"Athena SQL: {sql}")
        return (
            "ATHENA_RESULT:\n"
            "STATUS: ERROR\n"
            f"SQL: {sql}\n"
            f"ERROR: {str(e)}\n"
            "ACTION: Call get_athena_table_info once, fix the SQL, and retry ONCE. "
            "If already retried, use pandas_query instead."
        )


# ── System Prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a friendly and accurate data analyst agent.
You have access to datasets stored in AWS S3 and answer questions in plain English.

TOOLS AND WHEN TO USE THEM:
  list_datasets         → user asks what data is available
  get_schema            → ALWAYS call first on any question to understand columns and sample data
  pandas_query          → row-level filters, overviews, statistics, or datasets under PANDAS_THRESHOLD rows
  get_athena_table_info → ALWAYS call before writing SQL to confirm exact column names
  athena_sql_query      → aggregations (COUNT/SUM/AVG/GROUP BY), sorting, TOP N on large datasets

STRICT WORKFLOW FOR EVERY QUESTION:
  Step 1: call get_schema to understand the dataset columns and types
  Step 2: pick the right tool:
          - Simple filter / overview / small data  → pandas_query
          - Aggregation / ranking / large data      → athena_sql_query (after get_athena_table_info)
  Step 3: if athena_sql_query returns STATUS: ERROR
          → call get_athena_table_info once, rewrite the SQL, retry athena_sql_query ONCE
  Step 4: if athena_sql_query returns STATUS: LIMIT_REACHED or still returns STATUS: ERROR after retry
          → IMMEDIATELY use pandas_query — do NOT call athena_sql_query again
  Step 5: if athena_sql_query returns STATUS: EMPTY
          → do NOT retry endlessly; tell the user no matching data was found
  Step 6: NEVER call athena_sql_query more than 2 times per table for the same user question
  Step 7: NEVER dump a full 500-row table at the user; always summarize or limit to about 20 rows in your final answer
  Step 8: Respond in plain English — no code, no SQL, no jargon; be concise and accurate

RESPONSE STYLE:
  - For tables: summarize the result, and if needed, show only a small sample of rows (no more than about 20).
  - For stats: give a clear plain-English summary with the key numbers.
  - For errors: explain simply what happened and then try pandas_query as fallback when possible.
  - Never mention tool names, AWS, Athena, or pandas in your response to the user.
"""


# ── Agent ─────────────────────────────────────────────────────────────────────
agent = Agent(
    model         = MODEL_ID,
    system_prompt = SYSTEM_PROMPT,
    tools         = [list_datasets, get_schema, pandas_query,
                     get_athena_table_info, athena_sql_query],
)


# ── Main Interactive Loop ─────────────────────────────────────────────────────
def run():
    setup_logging()   # Creates CloudWatch log group + stream (first run only)

    print("=" * 62)
    print("  🥕🎵  NL Data Query Agent")
    print("  Bedrock + Strands + Athena + Guardrails + CloudWatch")
    print("=" * 62)
    if not GUARDRAIL_ID:
        print("  ⚠️  Guardrail not active. Run guardrail_setup.py first.\n")
    else:
        print(f"  🛡️  Guardrail active: {GUARDRAIL_ID}\n")

    print("  • Type any question in plain English")
    print("  • Type 'archive' → export logs to S3 now")
    print("  • Type 'quit'    → archive logs and exit\n")
    print("-" * 62)

    while True:
        try:
            user_input = input("\nYou: ").strip()

            # Reset Athena attempt counters for each new user question
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

            # 1. Check input through guardrail BEFORE sending to agent
            is_safe, checked_input = check_guardrail(user_input, "INPUT")
            if not is_safe:
                print(f"\nAgent: {checked_input}")
                continue

            # 2. Log the question to CloudWatch
            log_query(user_input)

            # 3. Send to agent — it autonomously picks the right tools
            print("\nAgent: ", end="", flush=True)
            result        = agent(user_input)
            response_text = str(result)

            # 4. Check agent response through guardrail BEFORE showing to user
            is_safe, final_output = check_guardrail(response_text, "OUTPUT")
            if not is_safe:
                print(final_output)
                log_response(user_input, "BLOCKED_BY_OUTPUT_GUARDRAIL", "GUARDRAIL")
            else:
                mode = "ATHENA" if "ATHENA_RESULT" in response_text else "PANDAS"
                log_response(user_input, response_text, mode)

        except KeyboardInterrupt:
            print("\n\nInterrupted — archiving logs...")
            archive_logs_to_s3()
            print("Goodbye!")
            break
        except Exception as e:
            log_error(str(e), context=f"Main loop — input: {user_input}")
            print(f"\n[Error] {e}")


# ── Web-facing helper for FastAPI ─────────────────────────────────────────────
def run_query(question: str) -> str:
    """Used by server.py — takes a question string and returns the agent's answer."""
    _athena_attempts.clear()

    if not question.strip():
        return "Please ask a non-empty question."

    try:
        is_safe, checked_input = check_guardrail(question, "INPUT")
        if not is_safe:
            return checked_input

        log_query(question)

        result = agent(checked_input)
        response_text = str(result)

        is_safe, final_output = check_guardrail(response_text, "OUTPUT")
        if not is_safe:
            log_response(question, "BLOCKED_BY_OUTPUT_GUARDRAIL", "GUARDRAIL")
            return final_output

        mode = "ATHENA" if "ATHENA_RESULT" in response_text else "PANDAS"
        log_response(question, response_text, mode)
        return final_output

    except Exception as e:
        log_error(str(e), context=f"run_query input: {question}")
        return f"[Error] {e}"


if __name__ == "__main__":
    run()