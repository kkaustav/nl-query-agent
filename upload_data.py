# upload_data.py

import boto3
import time
import pandas as pd
from config import BUCKET, REGION, ATHENA_DB, ATHENA_OUTPUT, DATASETS

s3     = boto3.client("s3",     region_name=REGION)
athena = boto3.client("athena", region_name=REGION)


def create_bucket():
    try:
        s3.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        print(f"✅ Bucket created: {BUCKET}")
    except s3.exceptions.BucketAlreadyOwnedByYou:
        print(f"ℹ️  Bucket already exists: {BUCKET}")
    except Exception as e:
        print(f"⚠️  Bucket error (may already exist): {e}")


def upload_datasets():
    for name, key in DATASETS.items():
        local_file = f"{name}.csv"
        try:
            s3.upload_file(local_file, BUCKET, key)
            print(f"✅ Uploaded {local_file} → s3://{BUCKET}/{key}")
        except FileNotFoundError:
            print(f"❌ File not found: {local_file} — place it in the project root folder")
            raise


def run_athena_ddl(sql):
    resp    = athena.start_query_execution(
        QueryString=sql,
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT}
    )
    exec_id = resp["QueryExecutionId"]
    while True:
        status = athena.get_query_execution(QueryExecutionId=exec_id)
        state  = status["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        elif state in ("FAILED", "CANCELLED"):
            reason = status["QueryExecution"]["Status"].get("StateChangeReason", "")
            raise Exception(f"Athena DDL failed [{state}]: {reason}\nSQL: {sql}")
        time.sleep(1)


def create_athena_tables():
    run_athena_ddl(f"CREATE DATABASE IF NOT EXISTS {ATHENA_DB}")
    print(f"✅ Athena database ready: {ATHENA_DB}")

    type_map = {
        "int64":   "BIGINT",
        "float64": "DOUBLE",
        "object":  "STRING",
        "bool":    "BOOLEAN",
    }

    for name, key in DATASETS.items():
        df       = pd.read_csv(f"{name}.csv", nrows=1)
        col_defs = ",\n  ".join(
            f"`{col}` {type_map.get(str(df[col].dtype), 'STRING')}"
            for col in df.columns
        )
        folder_key  = f"datasets/{name}/{name}.csv"
        s3_location = f"s3://{BUCKET}/datasets/{name}/"

        s3.copy_object(
            CopySource={"Bucket": BUCKET, "Key": key},
            Bucket=BUCKET,
            Key=folder_key
        )

        ddl = f"""
        CREATE EXTERNAL TABLE IF NOT EXISTS {ATHENA_DB}.{name} (
          {col_defs}
        )
        ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
        WITH SERDEPROPERTIES ('skip.header.line.count'='1')
        STORED AS TEXTFILE
        LOCATION '{s3_location}';
        """
        run_athena_ddl(ddl)
        print(f"✅ Athena table created: {ATHENA_DB}.{name}")


if __name__ == "__main__":
    print("🚀 Setting up NL Query Agent infrastructure...\n")
    create_bucket()
    upload_datasets()
    create_athena_tables()
    print("\n🎉 Setup complete! Run guardrail_setup.py next.")