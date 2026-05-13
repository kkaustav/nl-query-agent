# athena_helper.py

import time
from typing import List
from io import StringIO, BytesIO

import boto3
import pandas as pd

from config import REGION, ATHENA_DB, ATHENA_OUTPUT, BUCKET

athena = boto3.client("athena", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)


# ── Core query runner ─────────────────────────────────────────────────────────
def run_query(sql: str) -> pd.DataFrame:
    resp = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": ATHENA_DB},
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
    )
    exec_id = resp["QueryExecutionId"]

    while True:
        status = athena.get_query_execution(QueryExecutionId=exec_id)
        state = status["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        elif state in ("FAILED", "CANCELLED"):
            reason = status["QueryExecution"]["Status"].get("StateChangeReason", "")
            raise Exception(f"Athena query failed [{state}]: {reason}\nSQL: {sql}")
        time.sleep(1)

    if not sql.strip().lower().startswith("select"):
        return pd.DataFrame()

    status = athena.get_query_execution(QueryExecutionId=exec_id)
    output_loc = status["QueryExecution"]["ResultConfiguration"]["OutputLocation"]

    prefix = f"s3://{BUCKET}/"
    if not output_loc.startswith(prefix):
        raise Exception(f"Unexpected OutputLocation: {output_loc}")
    result_key = output_loc[len(prefix):]

    obj = s3.get_object(Bucket=BUCKET, Key=result_key)
    body = obj["Body"].read().decode("utf-8")
    if not body.strip():
        return pd.DataFrame()

    return pd.read_csv(StringIO(body))


# ── Helper: build CREATE TABLE DDL from CSV header ────────────────────────────
def generate_create_table_sql(table_name: str, s3_prefix: str, columns: List[str]) -> str:
    sanitized_cols = []
    for col in columns:
        col_clean = col.strip().replace(" ", "_").replace("-", "_").lower()
        sanitized_cols.append(col_clean)

    cols_ddl = ",\n  ".join(f"{c} string" for c in sanitized_cols)
    prefix = s3_prefix if s3_prefix.endswith("/") or s3_prefix == "" else s3_prefix + "/"
    location = f"s3://{BUCKET}/{prefix}"

    ddl = f"""
CREATE EXTERNAL TABLE IF NOT EXISTS {table_name} (
  {cols_ddl}
)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
WITH SERDEPROPERTIES (
  'separatorChar' = ',',
  'quoteChar' = '\"',
  'escapeChar' = '\\\\'
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
    obj = s3.get_object(Bucket=BUCKET, Key=csv_key)
    body = obj["Body"].read().decode("utf-8", errors="replace")
    first_line = body.splitlines()[0]
    header_cols = [c.strip() for c in first_line.split(",")]

    if s3_prefix is None:
        s3_prefix = csv_key.rsplit("/", 1)[0] + "/" if "/" in csv_key else ""

    ddl = generate_create_table_sql(table_name=table_name, s3_prefix=s3_prefix, columns=header_cols)

    run_query(f"DROP TABLE IF EXISTS {table_name}")
    run_query(ddl)


# ── Unified file sync: CSV, Parquet, JSON ─────────────────────────────────────
def sync_table_from_file(table_name: str, s3_key: str) -> None:
    ext = s3_key.rsplit(".", 1)[-1].lower()

    if ext == "csv":
        sync_table_from_csv(table_name, s3_key)

    elif ext == "parquet":
        if "/" in s3_key:
            s3_prefix = s3_key.rsplit("/", 1)[0] + "/"
        else:
            s3_prefix = ""
        location = f"s3://{BUCKET}/{s3_prefix}"

        obj = s3.get_object(Bucket=BUCKET, Key=s3_key)
        df_sample = pd.read_parquet(BytesIO(obj["Body"].read()))

        athena_type_map = {
            "int64": "BIGINT", "int32": "INT",
            "float64": "DOUBLE", "float32": "FLOAT",
            "bool": "BOOLEAN", "object": "STRING",
            "datetime64[ns]": "TIMESTAMP",
        }
        col_defs = ",\n  ".join(
            f"`{col}` {athena_type_map.get(str(df_sample[col].dtype), 'STRING')}"
            for col in df_sample.columns
        )

        run_query(f"DROP TABLE IF EXISTS {table_name}")
        run_query(f"""CREATE EXTERNAL TABLE IF NOT EXISTS {table_name} (
  {col_defs}
)
STORED AS PARQUET
LOCATION '{location}'""")

    elif ext == "json":
        if "/" in s3_key:
            s3_prefix = s3_key.rsplit("/", 1)[0] + "/"
        else:
            s3_prefix = ""
        location = f"s3://{BUCKET}/{s3_prefix}"

        obj = s3.get_object(Bucket=BUCKET, Key=s3_key)
        body = obj["Body"].read().decode("utf-8")
        try:
            df_sample = pd.read_json(StringIO(body))
        except ValueError:
            df_sample = pd.read_json(StringIO(body), lines=True)

        col_defs = ",\n  ".join(f"`{col}` string" for col in df_sample.columns)

        run_query(f"DROP TABLE IF EXISTS {table_name}")
        run_query(f"""CREATE EXTERNAL TABLE IF NOT EXISTS {table_name} (
  {col_defs}
)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
STORED AS TEXTFILE
LOCATION '{location}'""")

    else:
        raise ValueError(f"Unsupported file type: .{ext}")