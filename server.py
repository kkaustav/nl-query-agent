# output/server.py
import asyncio
import json

import boto3
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent import (
    run_query,
    refresh_datasets,
    get_current_datasets,
    register_dataset,
    unregister_dataset,
    clear_conversation,
    get_query_history,
)
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

@app.get("/datasets")
async def datasets():
    # Always pull fresh from S3 so the sidebar reflects reality
    refresh_datasets()
    return JSONResponse(content=sorted(get_current_datasets().keys()))

@app.post("/query")
async def query(request: QueryRequest):
    try:
        # Refresh before every query — handles uploads done in another terminal/process
        await asyncio.to_thread(refresh_datasets)
        result = await asyncio.to_thread(run_query, request.question, request.session_id)
        return JSONResponse(content={"text": result, "done": True})
    except Exception as e:
        return JSONResponse(status_code=500, content={"text": f"Error: {str(e)}", "done": True})

@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    dataset_name: str = Form(...),
):
    try:
        if not file.filename:
            return JSONResponse(status_code=400, content={"error": "No file selected."})

        ext = file.filename.rsplit(".", 1)[-1].lower()
        if ext not in ("csv", "parquet", "json"):
            return JSONResponse(
                status_code=400,
                content={"error": f"Unsupported file type: .{ext}. Use CSV, Parquet, or JSON."},
            )

        contents = await file.read()
        s3_key = f"datasets/{dataset_name}.{ext}"
        s3.put_object(Bucket=BUCKET, Key=s3_key, Body=contents)

        register_dataset(dataset_name, s3_key)
        refresh_datasets()

        return JSONResponse(
            content={
                "message": f"Uploaded and registered dataset '{dataset_name}'.",
                "dataset_name": dataset_name,
                "datasets": sorted(get_current_datasets().keys()),
            }
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/delete")
async def delete(dataset_name: str = Form(...)):
    try:
        refresh_datasets()
        datasets_map = get_current_datasets()
        key = datasets_map.get(dataset_name)

        if not key:
            return JSONResponse(status_code=404, content={"error": f"Dataset '{dataset_name}' not found."})

        s3.delete_object(Bucket=BUCKET, Key=key)
        unregister_dataset(dataset_name)
        refresh_datasets()

        return JSONResponse(
            content={
                "message": f"Deleted dataset '{dataset_name}' successfully.",
                "datasets": sorted(get_current_datasets().keys()),
            }
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/history")
async def history(session_id: str = "default"):
    return JSONResponse(content=get_query_history(session_id))

@app.post("/clear")
async def clear(session_id: str = "default"):
    clear_conversation(session_id)
    return JSONResponse(content={"message": "Conversation cleared."})

if __name__ == "__main__":
    print("\n🚀 NL Query Agent web server running")
    print("👉 Open this in your browser: http://localhost:8000\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)