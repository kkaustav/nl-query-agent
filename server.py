# server.py

import asyncio
import json
import uuid
import os
from fastapi import FastAPI, UploadFile, File, Form, Cookie
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import boto3
import uvicorn

from agent import run_query, register_dataset, get_query_history, clear_conversation
from athena_helper import sync_table_from_file
from config import BUCKET, REGION

s3 = boto3.client("s3", region_name=REGION)

app = FastAPI(title="NL Query Agent Web")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class QueryRequest(BaseModel):
    question: str
    session_id: str | None = None


@app.get("/", response_class=HTMLResponse)
async def home():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.post("/query")
async def query(request: QueryRequest):
    session_id = request.session_id or "default"

    async def stream_response():
        try:
            result = await asyncio.to_thread(run_query, request.question, session_id)
            yield f"data: {json.dumps({'text': result, 'done': False})}\n\n"
            yield f"data: {json.dumps({'text': '', 'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'text': f'Error: {str(e)}', 'done': True})}\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")


@app.post("/upload")
async def upload_dataset(
    file: UploadFile = File(...),
    table_name: str = Form(...)
):
    """
    Accepts CSV, Parquet, or JSON uploads.
    Uploads to S3, registers in the agent, syncs Athena table.
    """
    allowed_exts = {"csv", "parquet", "json"}
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""

    if ext not in allowed_exts:
        return JSONResponse(
            status_code=400,
            content={"error": f"Unsupported file type '.{ext}'. Allowed: csv, parquet, json"}
        )

    # Sanitize table name
    safe_name = "".join(c if c.isalnum() or c == "_" else "_" for c in table_name.lower())
    s3_key = f"datasets/{safe_name}/{safe_name}.{ext}"

    try:
        contents = await file.read()
        s3.put_object(Bucket=BUCKET, Key=s3_key, Body=contents)

        # Register dataset in agent memory
        register_dataset(safe_name, s3_key)

        # Sync Athena external table
        await asyncio.to_thread(sync_table_from_file, safe_name, s3_key)

        return JSONResponse(content={
            "success": True,
            "message": f"✅ Dataset '{safe_name}' uploaded and registered ({ext.upper()}, {len(contents):,} bytes).",
            "table_name": safe_name,
            "s3_key": s3_key
        })

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/history")
async def history(session_id: str = "default"):
    """Returns the query history list for a given session."""
    return JSONResponse(content={"history": get_query_history(session_id)})


@app.post("/clear")
async def clear(session_id: str = "default"):
    """Clears conversation memory and query history for a session."""
    clear_conversation(session_id)
    return JSONResponse(content={"success": True})


if __name__ == "__main__":
    print("\n🚀 NL Query Agent web server running")
    print("👉 Open this in your browser: http://localhost:8000\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)