BUCKET = "nl-query-agent-kaustubh"
REGION = "ap-south-1"
ATHENA_DB = "nl_query_db"
ATHENA_OUTPUT = f"s3://{BUCKET}/athena-results/"
MODEL_ID = "apac.amazon.nova-lite-v1:0"
PANDAS_THRESHOLD = 10_000

DATASETS = {
    "farmers_market": "datasets/farmers_market.csv",
    "spotify":        "datasets/spotify.csv",
    "output":         "datasets/output/output.json",
}

CW_LOG_GROUP = "/nl-query-agent"
CW_LOG_STREAM = "agent-sessions"

GUARDRAIL_ID = "9iwaukwehxwu"
GUARDRAIL_VERSION = "1"

LOG_ARCHIVE_PREFIX = "logs"