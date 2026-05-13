import asyncio
import json
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from agent import run_query   # the function you just added

app = FastAPI(title="NL Query Agent Web")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class QueryRequest(BaseModel):
    question: str

@app.get("/", response_class=HTMLResponse)
async def home():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/query")
async def query(request: QueryRequest):
    async def stream_response():
        try:
            result = await asyncio.to_thread(run_query, request.question)
            yield f"data: {json.dumps({'text': result, 'done': False})}\n\n"
            yield f"data: {json.dumps({'text': '', 'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'text': f'Error: {str(e)}', 'done': True})}\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")

if __name__ == "__main__":
    print("\n🚀 NL Query Agent web server running")
    print("👉 Open this in your browser: http://localhost:8000\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)