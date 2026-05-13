# 🥕🎵 NL Query Agent

> A production-grade Natural Language Data Query Agent built on AWS — ask questions in plain English, get answers from your S3 data.

---

## 🏗️ Architecture Overview

### Mermaid diagram

```mermaid
flowchart TB
    user["User - plain English question"]
    in_guard["Bedrock Guardrail (Input)"]
    agent["Strands Agent - Nova Lite"]
    listd["list_datasets"]
    schema["get_schema"]
    pandas["pandas_query (< 10,000 rows)"]
    tableinfo["get_athena_table_info"]
    athena_tool["athena_sql_query (aggregations, large data)"]
    pandas_engine["Pandas Engine - in-memory CSV"]
    athena["Athena - SQL on S3"]
    s3["Amazon S3 - datasets and results"]
    out_guard["Bedrock Guardrail (Output)"]
    logs["CloudWatch Logs"]
    answer["User - plain English answer"]

    user --> in_guard --> agent

    subgraph Tools
        agent --> listd
        agent --> schema
        agent --> pandas
        agent --> tableinfo
        agent --> athena_tool
    end

    pandas --> pandas_engine --> s3
    tableinfo --> s3
    athena_tool --> athena --> s3

    agent --> out_guard --> answer
    agent --> logs
```

### ASCII architecture (original)

```text
┌─────────────────────────────────────────────────────────────────────┐
│                        USER (Plain English)                         │
│              "What are the top 5 selling products?"                 │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    🛡️  BEDROCK GUARDRAIL (INPUT)                    │
│         Blocks: hate, violence, prompt attacks, off-topic           │
│         Anonymizes: PII (emails, phone numbers, names)              │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ safe input only
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│              🤖  STRANDS AGENT (Orchestration Layer)                │
│                  Model: Amazon Nova Lite (Bedrock)                  │
│                                                                     │
│   Agent autonomously decides which tool to call:                    │
│                                                                     │
│   ┌─────────────────┐   ┌──────────────────┐   ┌───────────────┐  │
│   │  list_datasets  │   │   get_schema     │   │ pandas_query  │  │
│   │  What data is   │   │  Column names,   │   │ In-memory     │  │
│   │  available?     │   │  types, samples  │   │ < 10,000 rows │  │
│   └─────────────────┘   └──────────────────┘   └───────────────┘  │
│                                                                     │
│   ┌──────────────────────────┐   ┌──────────────────────────────┐  │
│   │  get_athena_table_info   │   │     athena_sql_query         │  │
│   │  Exact column names for  │   │  Real SQL on S3 via Athena   │  │
│   │  SQL query building      │   │  Aggregations, filters, TOP N│  │
│   └──────────────────────────┘   └──────────────────────────────┘  │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                    ┌───────────┴────────────┐
                    │                        │
                    ▼                        ▼
   ┌─────────────────────┐     ┌─────────────────────────────┐
   │   🐼 PANDAS ENGINE  │     │     🔍 AMAZON ATHENA        │
   │   For small/med     │     │     For large data / SQL    │
   │   < 10,000 rows     │     │     > 10,000 rows           │
   │   Loaded into RAM   │     │     Serverless SQL on S3    │
   └──────────┬──────────┘     └──────────────┬──────────────┘
              │                               │
              └───────────────┬───────────────┘
                              │ reads from / writes to
                              ▼
             ┌────────────────────────────────┐
             │         ☁️  AMAZON S3          │
             │   s3://nl-query-agent-<you>   │
             │                                │
             │   datasets/farmers_market.csv  │
             │   datasets/spotify.csv         │
             │   athena-results/  (temp)      │
             │   logs/  (archived sessions)   │
             └────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    🛡️  BEDROCK GUARDRAIL (OUTPUT)                   │
│         Checks agent response before showing to user                │
│         Grounding threshold: 0.7 | Relevance threshold: 0.7         │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ safe response only
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   📊  CLOUDWATCH LOGS                               │
│   Logs every event: USER_QUERY | AGENT_RESPONSE | GUARDRAIL_EVENT   │
│   Retention: 90 days | Archive: exported to S3 on quit/archive      │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │   👤 USER RESPONSE    │
                    │   Plain English answer│
                    └───────────────────────┘
```

---

## 🗂️ Project Structure

```text
nl-query-agent/
│
├── config.py              ← ⚙️  All settings — edit BUCKET name here (once)
├── requirements.txt       ← 📦 Python dependencies
│
├── upload_data.py         ← 🚀 STEP 1: Creates S3 bucket + uploads CSVs
├── guardrail_setup.py     ← 🛡️  STEP 2: Creates Bedrock Guardrail
│
├── athena_helper.py       ← 🔧 Helper: Athena async runner + table sync
├── logger.py              ← 🔧 Helper: CloudWatch logging + S3 archival
│
├── agent.py               ← 🤖 Terminal agent (run in shell)
├── server.py              ← 🌐 FastAPI web server (browser UI)
├── templates/
│   └── index.html         ← 💬 Single-page chat UI for the agent
│
├── farmers_market.csv     ← 📊 Local copy of dataset (uploaded by upload_data.py)
├── spotify.csv            ← 📊 Local copy of dataset (uploaded by upload_data.py)
└── README.md              ← 📖 This file
```

### File Responsibilities

| File | Run It? | Purpose |
|------|---------|---------|
| `config.py` | ❌ Edit once | Single source of truth for BUCKET, REGION, MODEL_ID, thresholds |
| `requirements.txt` | ❌ Used by pip | Lists all Python packages needed |
| `upload_data.py` | ✅ Run once | Creates S3 bucket, uploads CSVs, registers initial Athena DB |
| `guardrail_setup.py` | ✅ Run once | Creates Bedrock Guardrail + auto-writes ID to `config.py` |
| `athena_helper.py` | ❌ Never directly | Runs Athena queries + can resync Athena tables from CSV |
| `logger.py` | ❌ Never directly | Structured CloudWatch logging + S3 log archival |
| `agent.py` | ✅ Optional | Main interactive agent loop in the terminal |
| `server.py` | ✅ Optional | FastAPI server exposing the agent at `http://localhost:8000` |
| `templates/index.html` | ❌ | Frontend chat UI rendered by the browser |

---

## ☁️ AWS Services Used

| Service | Role in This Project | Why |
|---------|---------------------|-----|
| **Amazon S3** | Stores CSV datasets, Athena query results, and archived logs | Cheap, durable, serverless storage |
| **Amazon Athena** | Runs SQL directly on S3 CSV files via external tables | No database to manage — query data where it lives |
| **AWS Glue Data Catalog** | Holds Athena table definitions for your CSVs | Athena uses Glue under the hood |
| **Amazon Bedrock** | Hosts Nova Lite LLM that powers the agent brain | Managed AI inference, no GPU needed |
| **Bedrock Guardrails** | Safety layer on inputs + outputs | Blocks harmful content, PII, off-topic queries |
| **Amazon CloudWatch Logs** | Logs every agent interaction with 90-day retention | Audit trail, debugging, session history |

---

## 🧠 How the Agent Decides What To Do

The agent uses **Amazon Nova Lite (apac.amazon.nova-lite-v1:0)** as its brain, orchestrated via the Strands Agents SDK. It reads your question, chooses tools, and returns a plain-English answer.

### Tool Selection Logic

```text
list_datasets
  → When user asks what data is available.

get_schema
  → ALWAYS called first for a dataset question to see columns + sample rows.

pandas_query
  → Overviews, stats, filters, and any dataset under PANDAS_THRESHOLD rows.

get_athena_table_info
  → Called before writing SQL to confirm exact column names for Athena.

athena_sql_query
  → COUNT, SUM, AVG, GROUP BY, ORDER BY, TOP N, filters on large datasets.
  → At most 2 calls per table per question; on repeated errors it falls
    back to pandas_query where possible.
```

### Query Routing (Pandas vs Athena)

```text
Dataset size < 10,000 rows?
    YES → pandas_query  (fast, in-memory CSV)
    NO  → athena_sql_query  (serverless SQL, handles larger data)
```

---

## 🛡️ Guardrail Configuration

The Bedrock Guardrail sits on both input and output.

### Content Filters (examples)

| Type | Input | Output |
|------|-------|--------|
| Hate speech | HIGH | HIGH |
| Insults | HIGH | HIGH |
| Violence | MEDIUM | MEDIUM |
| Misconduct | HIGH | HIGH |
| Prompt attacks | HIGH | NONE |

### Denied Topics

Any question not related to the configured datasets (farmers markets / Spotify) or data analysis is blocked.

### PII Protection

| Data Type | Action |
|-----------|--------|
| Email addresses | Anonymized |
| Phone numbers | Anonymized |
| Names | Anonymized |
| AWS Access Keys | Blocked |
| Passwords | Blocked |

### Grounding Check

- Grounding threshold: `0.7` — response must be grounded in actual data.  
- Relevance threshold: `0.7` — response must be relevant to the question.

---

## 🛠️ Setup Order (Start to Finish)

### Step 0 — Local setup

```bash
cd ~/Documents
# Clone or copy the repo here

# Create venv (recommended)
python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt  # if present
pip install strands-agents boto3 pandas fastapi uvicorn
```

Configure AWS CLI:

```bash
aws configure
# Region: ap-south-1
# Output: json
```

In the AWS Console, enable **Nova Lite** in Bedrock Model Access for `ap-south-1`. Attach an IAM policy with S3, Athena, Bedrock, and CloudWatch permissions.

---

### Step 1 — Upload data / initial tables

```bash
python3 upload_data.py
```

This:

- Creates S3 bucket `nl-query-agent-<you>` (or your configured BUCKET).
- Uploads:

  - `farmers_market.csv` → `s3://BUCKET/datasets/farmers_market.csv`  
  - `spotify.csv`        → `s3://BUCKET/datasets/spotify.csv`

- Creates Athena DB `nl_query_db` and initial table definitions.

---

### Step 2 — Guardrail setup

```bash
python3 guardrail_setup.py
```

This:

- Creates a Bedrock Guardrail.
- Writes its ID and version into `config.py` (`GUARDRAIL_ID`, `GUARDRAIL_VERSION`).

---

### Step 3 — (Optional) Resync Athena tables from CSV

If Athena schemas are ever wrong or drift out of sync, recreate them directly from the CSV header:

```bash
cd ~/Documents/nl-query-agent

# Recreate spotify table from datasets/spotify.csv
python3 -c "from athena_helper import sync_table_from_csv; sync_table_from_csv('spotify', 'datasets/spotify.csv')"

# Recreate farmers_market table from datasets/farmers_market.csv
python3 -c "from athena_helper import sync_table_from_csv; sync_table_from_csv('farmers_market', 'datasets/farmers_market.csv')"
```

This will:

- Drop the existing table (if it exists).
- Create a new external table with:

  - Columns from the CSV header, sanitized to lowercase.  
  - All columns as `string`.  
  - OpenCSVSerde and `skip.header.line.count='1'`.

---

### Step 4a — Run the Terminal Agent

```bash
cd ~/Documents/nl-query-agent
python3 agent.py
```

You’ll see the banner and can start asking questions in plain English from your terminal.

---

### Step 4b — 🖥️ Web UI (FastAPI + Browser)

In addition to the terminal agent, you can run a browser-based UI.

#### Start the Web Server (Local)

From your terminal:

```bash
cd ~/Documents/nl-query-agent && source .venv/bin/activate && python server.py
```

You should see:

```text
🚀 NL Query Agent web server running
👉 Open this in your browser: http://localhost:8000
```

Open that URL in your browser to chat with the agent.

#### Web UI Features

- Single-page HTML/JS frontend in `templates/index.html`.
- Streaming responses via Server-Sent Events (SSE) (`text/event-stream`).
- Same guardrails, logging, and query routing (Pandas vs Athena) as the terminal agent.
- No Node/React — just FastAPI + vanilla HTML/JS.

#### Optional: Public HTTPS URL with ngrok

To demo the agent from other devices or share a temporary link:

```bash
# Install once (macOS)
brew install ngrok

# In a second terminal, while server.py is running:
ngrok http 8000
```

ngrok will print a URL like:

```text
Forwarding  https://abcd-1234.ngrok-free.app -> http://localhost:8000
```

Open the `https://...ngrok...` link in any browser to access your NL Query Agent securely over HTTPS.

---

## 💬 Usage Examples

```text
You: Show me songs by Diljit Dosanjh.
Agent: Lists songs from the spotify dataset matching artist = 'Diljit Dosanjh'.

You: Which state has the most farmers markets?
Agent: Uses aggregation (COUNT + GROUP BY) and answers e.g. "California with 1,528 markets."

You: Top 5 states by farmers market count.
Agent: Returns top 5 states with counts.

You: Average user_rating by subscription_type in the spotify dataset.
Agent: Computes average rating for Family / Free / Premium using pandas or Athena depending on context.

You: archive
Agent: Archives logs to S3 immediately.

You: quit
Agent: Archives logs and exits.
```

The same questions work in the terminal **and** in the browser UI.

---

## 📊 CloudWatch Log Events

Every interaction is logged as structured JSON in CloudWatch and archived into S3:

```json
{"timestamp": "2026-05-12T10:30:00Z", "event_type": "USER_QUERY",      "question": "top 5 states by markets?"}
{"timestamp": "2026-05-12T10:30:02Z", "event_type": "AGENT_RESPONSE",  "query_mode": "ATHENA", "response_preview": "..."}
{"timestamp": "2026-05-12T10:30:03Z", "event_type": "GUARDRAIL_EVENT", "direction": "OUTPUT", "action": "ALLOWED"}
```

Archived in S3 as:

```text
s3://nl-query-agent-<you>/logs/YYYY/MM/DD/agent-session-{timestamp}.json
```

---

## ⚙️ Configuration Reference (`config.py`)

| Variable | Example | Description |
|----------|---------|-------------|
| `BUCKET` | `nl-query-agent-yourname` | Globally unique S3 bucket name |
| `REGION` | `ap-south-1` | AWS region |
| `ATHENA_DB` | `nl_query_db` | Athena database name |
| `ATHENA_OUTPUT` | `s3://nl-query-agent-yourname/athena-results/` | Where Athena writes query results |
| `MODEL_ID` | `apac.amazon.nova-lite-v1:0` | Bedrock model (Nova Lite) |
| `PANDAS_THRESHOLD` | `10_000` | Max rows for pandas_query |
| `DATASETS` | `{"farmers_market": "datasets/farmers_market.csv", "spotify": "datasets/spotify.csv"}` | S3 keys for CSVs |
| `GUARDRAIL_ID` | e.g. `9iwaukwehxwu` | Guardrail ID (set by `guardrail_setup.py`) |
| `GUARDRAIL_VERSION` | `"1"` | Guardrail version |

---

## 🔧 Troubleshooting

| Symptom | Cause | Fix |
|--------|-------|-----|
| `NoCredentialsError` | AWS not configured locally | Run `aws configure` |
| `AccessDeniedException` | Missing IAM permissions | Attach IAM policy with S3, Athena, Bedrock, CloudWatch access |
| `BucketAlreadyExists` | Bucket name taken | Change `BUCKET` in `config.py` and rerun `python3 upload_data.py` |
| `COLUMN_NOT_FOUND` in Athena | Table schema drift | Run `sync_table_from_csv('...', 'datasets/....csv')` again |
| Agent loops on Athena errors | Too many Athena retries | Logic caps at 2 attempts and falls back to pandas |
| Agent says “Athena query limit reached” | Athena still failing after 2 attempts | Ask a pandas-style question or resync the table from CSV |
| Web UI 404 on `/` | `templates/index.html` missing | Ensure `templates/index.html` exists and `server.py` runs from project root |

---

## 💰 AWS Cost Estimate

| Service | Usage | Estimated Cost (dev scale) |
|---------|-------|---------------------------|
| S3 | ~10MB data + logs | < $0.01/month |
| Athena | Per query scanned | Tiny queries ≈ $0.00005 each |
| Bedrock (Nova Lite) | Per token | Well under a few dollars/month for light dev |
| CloudWatch Logs | 90-day retention | Well under 1 GB, so ≪ $0.50/month |

For normal development usage this project should be well under **$1/month**.

---

*Built using AWS Strands Agents SDK, Amazon Bedrock, Athena, and boto3 — now with both terminal and browser UIs.*