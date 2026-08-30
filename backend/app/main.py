from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import settings
from .agent import Agent
from .leadership_report import LeadershipReportGenerator
from .tools import DataStore
from .monday_client import MondayAPIError

app = FastAPI(title="Skylark Drones BI Agent", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_agent = Agent()
_leadership_gen = LeadershipReportGenerator()


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


class ChatResponse(BaseModel):
    reply: str
    tool_trace: list[dict]


class LeadershipRequest(BaseModel):
    focus: str | None = None


class LeadershipResponse(BaseModel):
    report_markdown: str
    tool_trace: list[dict]


@app.get("/health")
def health():
    problems = settings.validate()
    return {
        "status": "ok" if not problems else "misconfigured",
        "problems": problems,
    }


@app.get("/health/monday")
def health_monday():
    """Verifies the monday.com API key + board IDs actually work."""
    try:
        store = DataStore()
        data = store.load(force_refresh=True)
        return {
            "status": "ok",
            "work_orders_rows": len(data.work_orders),
            "deals_rows": len(data.deals),
        }
    except MondayAPIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages must not be empty")
    conversation = [{"role": m.role, "content": m.content} for m in req.messages]
    try:
        result = _agent.respond(conversation)
    except MondayAPIError as e:
        raise HTTPException(status_code=502, detail=f"monday.com error: {e}") from e
    return ChatResponse(**result)


@app.post("/leadership-report", response_model=LeadershipResponse)
def leadership_report(req: LeadershipRequest):
    try:
        result = _leadership_gen.generate(focus=req.focus)
    except MondayAPIError as e:
        raise HTTPException(status_code=502, detail=f"monday.com error: {e}") from e
    return LeadershipResponse(**result)
