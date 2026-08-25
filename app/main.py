from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services.ai import FinOpsAI
from app.services.aws import AWSCollector
from app.services.cache import SQLiteStore
from app.services.security import SecurityService


settings = get_settings()
store = SQLiteStore(settings.database_path)
security_service = SecurityService(settings)
collector = AWSCollector(settings, store, security_service)
ai_service = FinOpsAI(settings, store, collector, security_service)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url=None,
    openapi_url=None,
    redoc_url=None,
)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

login_attempts: dict[str, deque[float]] = defaultdict(deque)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ModelConfigRequest(BaseModel):
    model: str
    temperature: float
    max_tokens: int


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class PricingRequest(BaseModel):
    service_code: str
    filters: dict[str, str] = Field(default_factory=dict)
    max_results: int = Field(default=20, ge=1, le=100)


class RecommendationTaskRequest(BaseModel):
    status: str
    owner: str = Field(default="", max_length=128)
    estimated_monthly_savings: float | None = Field(default=None, ge=0)
    actual_monthly_savings: float | None = Field(default=None, ge=0)
    note: str = Field(default="", max_length=2000)


TASK_TRANSITIONS = {
    None: {"NEW"},
    "NEW": {"NEW", "TRIAGED", "DISMISSED"},
    "TRIAGED": {"TRIAGED", "APPROVED", "DISMISSED"},
    "APPROVED": {"APPROVED", "IN_PROGRESS", "DISMISSED"},
    "IN_PROGRESS": {"IN_PROGRESS", "IMPLEMENTED", "DISMISSED"},
    "IMPLEMENTED": {"IMPLEMENTED"},
    "DISMISSED": {"DISMISSED", "TRIAGED"},
}


def current_user(request: Request) -> str:
    return security_service.require_user(request)


def default_period() -> tuple[str, str]:
    end = date.today() + timedelta(days=1)
    start = end.replace(day=1)
    return start.isoformat(), end.isoformat()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self' https://*.quicksight.amazonaws.cn https://*.amazonaws.com.cn; "
        "frame-src https://*.quicksight.amazonaws.cn https://*.amazonaws.com.cn; "
        "frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
    )
    return response


@app.exception_handler(Exception)
async def unhandled_error(_: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"status": "ERROR", "detail": security_service.redact_text(str(exc))})


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(static_dir / "index.html")


@app.post("/api/v1/auth/login")
def login(payload: LoginRequest, request: Request, response: Response):
    client = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = login_attempts[client]
    while attempts and attempts[0] < now - 300:
        attempts.popleft()
    if len(attempts) >= 10:
        raise HTTPException(status_code=429, detail="Too many login attempts; retry later")
    valid = payload.username == "finopsadmin" and security_service.verify_password(payload.password)
    if not valid:
        attempts.append(now)
        store.audit(payload.username[:64], "LOGIN_FAILED", client)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    attempts.clear()
    token = security_service.issue_session(payload.username)
    response.set_cookie(
        security_service.cookie_name,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookie,
        path="/",
    )
    store.audit(payload.username, "LOGIN_SUCCESS", client)
    return {"status": "OK", "user": payload.username, "expires_in_seconds": settings.session_ttl_seconds}


@app.post("/api/v1/auth/logout")
def logout(response: Response, user: str = Depends(current_user)):
    response.delete_cookie(security_service.cookie_name, path="/")
    store.audit(user, "LOGOUT")
    return {"status": "OK"}


@app.get("/api/v1/auth/status")
def auth_status(request: Request):
    user = security_service.session_user(request)
    return {"authenticated": bool(user), "user": user}


@app.get("/api/v1/health")
async def health(_: str = Depends(current_user)):
    accounts = await asyncio.to_thread(collector.accounts_status)
    return {"status": "OK" if accounts["status"] == "OK" else "DEGRADED", "version": settings.app_version, "accounts": accounts, "deepseek_configured": bool(settings.deepseek_api_key), "quicksight_dashboard": settings.quicksight_dashboard_id}


@app.get("/api/v1/openapi.json")
def protected_openapi(_: str = Depends(current_user)):
    return JSONResponse(app.openapi())


@app.get("/api/v1/accounts")
async def accounts(_: str = Depends(current_user)):
    return await asyncio.to_thread(collector.accounts_status)


@app.get("/api/v1/collector-runs")
def collector_runs(limit: int = Query(50, ge=1, le=200), _: str = Depends(current_user)):
    return {"status": "OK", "data": store.list_runs(limit)}


@app.get("/api/v1/tasks")
def recommendation_tasks(limit: int = Query(200, ge=1, le=1000), _: str = Depends(current_user)):
    return {"status": "OK", "data": store.list_tasks(limit)}


@app.put("/api/v1/tasks/{recommendation_id}")
def update_recommendation_task(
    recommendation_id: str,
    payload: RecommendationTaskRequest,
    user: str = Depends(current_user),
):
    requested_status = payload.status.upper()
    existing = next(
        (item for item in store.list_tasks(1000) if item["recommendation_id"] == recommendation_id),
        None,
    )
    current_status = existing["status"] if existing else None
    if requested_status not in TASK_TRANSITIONS.get(current_status, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Invalid task transition: {current_status or 'NONE'} -> {requested_status}",
        )
    task = store.upsert_task(
        recommendation_id,
        requested_status,
        payload.owner,
        payload.estimated_monthly_savings,
        payload.actual_monthly_savings,
        payload.note,
        user,
    )
    store.audit(user, "TASK_UPDATED", f"{recommendation_id}:{requested_status}")
    return {"status": "OK", "data": task}


@app.get("/api/v1/cost/summary")
async def cost_summary(start: str | None = None, end: str | None = None, metric: str = "UnblendedCost", _: str = Depends(current_user)):
    default_start, default_end = default_period()
    return await asyncio.to_thread(collector.cost_summary, start or default_start, end or default_end, metric)


@app.get("/api/v1/cost/trend")
async def cost_trend(start: str | None = None, end: str | None = None, metric: str = "UnblendedCost", _: str = Depends(current_user)):
    default_start, default_end = default_period()
    return await asyncio.to_thread(collector.cost_trend, start or default_start, end or default_end, metric)


@app.get("/api/v1/cost/breakdown")
async def cost_breakdown(dimension: str = "SERVICE", start: str | None = None, end: str | None = None, metric: str = "UnblendedCost", _: str = Depends(current_user)):
    default_start, default_end = default_period()
    return await asyncio.to_thread(collector.cost_breakdown, start or default_start, end or default_end, dimension.upper(), metric)


@app.get("/api/v1/cost/compare")
async def cost_compare(start: str, end: str, previous_start: str, previous_end: str, metric: str = "UnblendedCost", _: str = Depends(current_user)):
    return await asyncio.to_thread(collector.cost_compare, start, end, previous_start, previous_end, metric)


@app.get("/api/v1/cost/forecast")
async def cost_forecast(start: str, end: str, metric: str = "UNBLENDED_COST", _: str = Depends(current_user)):
    return await asyncio.to_thread(collector.forecast, start, end, metric)


@app.get("/api/v1/cost/resources")
async def cost_resources(start: str, end: str, service: str = "Amazon Elastic Compute Cloud - Compute", _: str = Depends(current_user)):
    return await asyncio.to_thread(collector.cost_resources, start, end, service)


@app.get("/api/v1/reconciliation/ce-cur")
async def reconcile(start: str, end: str, metric: str = "UnblendedCost", _: str = Depends(current_user)):
    return await asyncio.to_thread(collector.reconciliation, start, end, metric)


@app.post("/api/v1/pricing/products")
async def pricing(payload: PricingRequest, _: str = Depends(current_user)):
    return await asyncio.to_thread(collector.pricing_products, payload.service_code, payload.filters, payload.max_results)


@app.get("/api/v1/anomalies")
async def anomalies(start: str | None = None, end: str | None = None, _: str = Depends(current_user)):
    final_end = end or date.today().isoformat()
    final_start = start or (date.today() - timedelta(days=30)).isoformat()
    return await asyncio.to_thread(collector.anomalies, final_start, final_end)


@app.get("/api/v1/anomaly-monitors")
async def anomaly_monitors(_: str = Depends(current_user)):
    final_end = date.today().isoformat()
    final_start = (date.today() - timedelta(days=30)).isoformat()
    result = await asyncio.to_thread(collector.anomalies, final_start, final_end)
    return {**result, "data": [{"account_alias": item["account_alias"], "configuration_status": item["result"].get("configuration_status"), "monitors": item["result"].get("monitors", [])} for item in result.get("data", [])]}


@app.get("/api/v1/recommendations/summary")
async def recommendation_summary(_: str = Depends(current_user)):
    return await asyncio.to_thread(collector.co_recommendations, "summary")


@app.get("/api/v1/recommendations/{recommendation_id}/evidence")
async def recommendation_evidence(recommendation_id: str, _: str = Depends(current_user)):
    try:
        return await asyncio.to_thread(collector.recommendation_evidence, recommendation_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/v1/recommendations/{resource_type}")
async def recommendations(resource_type: str, days: int = Query(14, ge=14, le=30), _: str = Depends(current_user)):
    if resource_type == "idle":
        return await asyncio.to_thread(collector.idle_recommendations, days)
    if resource_type in {"rds", "aurora"}:
        return await asyncio.to_thread(collector.database_recommendations, resource_type, days)
    if resource_type == "rightsizing":
        return await asyncio.to_thread(collector.rightsizing)
    return await asyncio.to_thread(collector.co_recommendations, resource_type)


@app.get("/api/v1/commitments/{kind}/{mode}")
async def commitments(kind: str, mode: str, start: str | None = None, end: str | None = None, _: str = Depends(current_user)):
    default_start, default_end = default_period()
    return await asyncio.to_thread(collector.commitments, kind, mode, start or default_start, end or default_end)


@app.get("/api/v1/ai/config")
def model_config(_: str = Depends(current_user)):
    return ai_service.get_config()


@app.put("/api/v1/ai/config")
def update_model_config(payload: ModelConfigRequest, _: str = Depends(current_user)):
    try:
        return ai_service.update_config(payload.model, payload.temperature, payload.max_tokens)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v1/ai/test")
async def test_ai(_: str = Depends(current_user)):
    return await asyncio.to_thread(ai_service.test_connection)


@app.post("/api/v1/ai/chat")
async def ai_chat(payload: ChatRequest, _: str = Depends(current_user)):
    try:
        return await asyncio.to_thread(ai_service.chat, payload.question)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=502 if isinstance(exc, RuntimeError) else 400, detail=str(exc)) from exc


@app.get("/api/v1/quicksight/cid/embed-url")
async def cid_embed(_: str = Depends(current_user)):
    try:
        return await asyncio.to_thread(collector.quicksight_embed_url)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
