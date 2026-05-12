# athena_helper.py
# DO NOT RUN DIRECTLY — imported automatically by agent.py
#
# Provides:
#   - run_query(sql) → DataFrame
#   - generate_create_table_sql(table_name, s3_prefix, columns) → DDL string
#   - sync_table_from_csv(table_name, csv_key, s3_prefix=None) → drop + recreate table from CSV header

import time
from typing import List
from io import StringIO

import boto3
import pandas as pd

from config import REGION, ATHENA_DB, ATHENA_OUTPUT, BUCKET

athena = boto3.client("athena", region_name=REGION)
s3     = boto3.client("s3",     region_name=REGION)


# ── Core query runner ─────────────────────────────────────────────────────────
def run_query(sql: str) -> pd.DataFrame:
    """Submits SQL to Athena, waits for result.

    - For SELECT queries: returns a DataFrame of results.
    - For non-SELECT (DDL/DML): waits for success and returns an empty DataFrame.
    """

    # Submit query to Athena
    resp    = athena.start_query_execution(
        QueryString           = sql,
        QueryExecutionContext = {"Database": ATHENA_DB},
        ResultConfiguration   = {"OutputLocation": ATHENA_OUTPUT},
    )
    exec_id = resp["QueryExecutionId"]

    # Poll every second until Athena finishes (it's async)
    while True:
        status = athena.get_query_execution(QueryExecutionId=exec_id)
        state  = status["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        elif state in ("FAILED", "CANCELLED"):
            reason = status["QueryExecution"]["Status"].get("StateChangeReason", "")
            raise Exception(f"Athena query failed [{state}]: {reason}\nSQL: {sql}")
        time.sleep(1)

    # If it's not a SELECT, Athena may not write a result CSV, or it may be empty.
    # In that case, just return an empty DataFrame.
    if not sql.strip().lower().startswith("select"):
        return pd.DataFrame()

    # For SELECTs, Athena writes output CSV to S3 — get the actual output location
    status     = athena.get_query_execution(QueryExecutionId=exec_id)
    output_loc = status["QueryExecution"]["ResultConfiguration"]["OutputLocation"]
    # Example: s3://nl-query-agent-kaustubh/athena-results/8677-...-3fe4.csv

    prefix = f"s3://{BUCKET}/"
    if not output_loc.startswith(prefix):
        raise Exception(f"Unexpected OutputLocation: {output_loc}")
    result_key = output_loc[len(prefix):]

    obj  = s3.get_object(Bucket=BUCKET, Key=result_key)
    body = obj["Body"].read().decode("utf-8")
    if not body.strip():
        # Empty file → treat as empty result
        return pd.DataFrame()

    df = pd.read_csv(StringIO(body))
    return df


# ── Helper: build CREATE TABLE DDL from CSV header ────────────────────────────
def generate_create_table_sql(
    table_name: str,
    s3_prefix: str,
    columns: List[str],
) -> str:
    """
    Generates a CREATE EXTERNAL TABLE statement for a CSV in S3, using:
      - all columns as string
      - OpenCSVSerde for robust CSV parsing
      - skip.header.line.count='1' to ignore header row

    Args:
        table_name: Athena table name (e.g., 'spotify' or 'farmers_market')
        s3_prefix:  S3 prefix where CSVs live, e.g. 'datasets/spotify/'
                    or 'datasets/' — must be a prefix, not a single file
        columns:    Column names (from CSV header) in order
    """
    # Sanitize column names for Athena: lowercase and replace spaces/dashes with underscores
    sanitized_cols = []
    for col in columns:
        col_clean = col.strip()
        col_clean = col_clean.replace(" ", "_").replace("-", "_")
        col_clean = col_clean.lower()
        sanitized_cols.append(col_clean)

    cols_ddl = ",\n  ".join(f"{c} string" for c in sanitized_cols)

    # Ensure prefix has trailing slash, and build LOCATION
    prefix = s3_prefix if s3_prefix.endswith("/") or s3_prefix == "" else s3_prefix + "/"
    location = f"s3://{BUCKET}/{prefix}"

    ddl = f"""
CREATE EXTERNAL TABLE IF NOT EXISTS {table_name} (
  {cols_ddl}
)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
WITH SERDEPROPERTIES (
  'separatorChar' = ',',
  'quoteChar'     = '\"',
  'escapeChar'    = '\\\\'
)
STORED AS TEXTFILE
LOCATION '{location}'
TBLPROPERTIES (
  'skip.header.line.count' = '1'
)
"""
    return ddl.strip()


# ── High-level: drop + recreate table from CSV in S3 ─────────────────────────
def sync_table_from_csv(table_name: str, csv_key: str, s3_prefix: str | None = None) -> None:
    """
    Drops and recreates an Athena EXTERNAL TABLE for the given CSV.

    - Reads the header line from s3://BUCKET/csv_key to get column names
    - Assumes all columns are string (safe for mixed / NaN content)
    - Uses OpenCSVSerde and skip.header.line.count='1'

    Args:
        table_name:  Target Athena table name (e.g., 'spotify')
        csv_key:     S3 key of the CSV (relative to BUCKET), e.g. 'datasets/spotify.csv'
        s3_prefix:   S3 prefix to use as LOCATION. If None, uses the directory of csv_key.
                     Example: csv_key='datasets/spotify.csv' → prefix 'datasets/'
    """
    # 1. Read just the header row from S3
    obj = s3.get_object(Bucket=BUCKET, Key=csv_key)
    body = obj["Body"].read().decode("utf-8", errors="replace")
    first_line = body.splitlines()[0]
    header_cols = [c.strip() for c in first_line.split(",")]

    # 2. Derive prefix if not provided
    if s3_prefix is None:
        if "/" in csv_key:
            s3_prefix = csv_key.rsplit("/", 1)[0] + "/"
        else:
            s3_prefix = ""  # CSV at bucket root

    # 3. Generate DDL
    ddl = generate_create_table_sql(
        table_name=table_name,
        s3_prefix=s3_prefix,
        columns=header_cols,
    )

    # 4. Drop existing table (if any) and then create the new one
    drop_sql = f"DROP TABLE IF EXISTS {table_name}"
    run_query(drop_sql)  # NO-OP if table does not exist

    run_query(ddl)