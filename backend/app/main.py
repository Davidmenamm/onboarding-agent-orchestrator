import asyncio
import json
import re
import time
import uuid
from typing import Literal

import psycopg
import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from app import auth, db, events, pii, rag, service
from app.graph import guard
from app.logging import setup_logging
from app.settings import settings
from app.worker import enqueue

setup_logging()
log = structlog.get_logger()
app = FastAPI(title="Onboarding Orchestrator API", version="0.3.0")

HTTP_REQS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LAT = Histogram("http_request_duration_seconds", "HTTP latency", ["route"])
FINAL = {"approved", "approved_edd", "rejected", "failed", "expired"}
WAITING = {"awaiting_analyst", "awaiting_client"}


@app.middleware("http")
async def correlation_and_metrics(request: Request, call_next):
    cid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    structlog.contextvars.bind_contextvars(correlation_id=cid)
    started = time.perf_counter()
    try:
        resp = await call_next(request)
    except Exception:  # fail loud, but give the user something they can quote
        log.exception("unhandled")
        resp = JSONResponse({"detail": "internal error", "correlation_id": cid}, status_code=500)
    route = getattr(request.scope.get("route"), "path", "unmatched")
    HTTP_REQS.labels(request.method, route, resp.status_code).inc()
    HTTP_LAT.labels(route).observe(time.perf_counter() - started)
    resp.headers["x-request-id"] = cid
    structlog.contextvars.clear_contextvars()
    return resp


# ---------------------------------------------------------------- infra
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/readyz")
def readyz():
    db.fetch_one("SELECT 1")
    return {"ok": True}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------- auth (dev issuer)
class DevLogin(BaseModel):
    username: str = Field(min_length=2, max_length=40, pattern=r"^[a-zA-Z0-9._-]+$")
    role: Literal["applicant", "analyst", "admin"]


@app.post("/api/v1/auth/dev-login")
def dev_login(body: DevLogin):
    return {"access_token": auth.issue(body.username, body.role), "role": body.role, "username": body.username}


@app.get("/.well-known/jwks.json")
def jwks():
    return auth.jwks()


# ---------------------------------------------------------------- onboarding (applicant)
class StartRequest(BaseModel):
    prospect_name: str = Field(min_length=3, max_length=120)
    document_id: str
    product: str

    @field_validator("prospect_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not re.fullmatch(r"[A-Za-zÀ-ÿ' .-]+", v) or len(v.split()) < 2:
            raise ValueError("enter first and last name (letters only)")
        return v

    @field_validator("document_id")
    @classmethod
    def _doc(cls, v: str) -> str:
        v = v.strip()
        if not re.fullmatch(r"\d{10}", v):
            raise ValueError("document_id must be exactly 10 digits")
        return v


@app.get("/api/v1/products")
def products():
    return rag.products()


@app.post("/api/v1/onboarding/start", status_code=202)
async def start(
    body: StartRequest,
    wait: int = Query(0, ge=0, le=120, description="Block up to N seconds until finished or paused"),
    idempotency_key: str | None = Header(None),
):
    try:
        oid, created = service.create_onboarding(body.prospect_name, body.document_id, body.product, idempotency_key, None)
    except service.InvalidProduct:
        raise HTTPException(422, f"unknown product {body.product!r}; see GET /api/v1/products") from None
    if created:
        enqueue(oid)
    if wait:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and _status(oid) not in FINAL | WAITING:
            await asyncio.sleep(1)
    return {"onboarding_id": oid, "created": created, "events_url": f"/api/v1/onboarding/{oid}/events", **_public_view(oid)}


def _status(oid: str) -> str:
    row = db.fetch_one("SELECT status FROM app.onboardings WHERE id = %s", oid)
    if not row:
        raise HTTPException(404, "onboarding not found")
    return row["status"]


def _timeline(oid: str, after: int = 0) -> list[dict]:
    rows = db.fetch(
        "SELECT id, type, actor, payload, ts FROM app.onboarding_events WHERE onboarding_id = %s AND id > %s ORDER BY id", oid, after
    )
    return [{**r, "ts": r["ts"].isoformat()} for r in rows]


def _public_view(oid: str) -> dict:
    """What the applicant may see: no risk details, no internal reasoning."""
    o = db.fetch_one(
        "SELECT o.status, o.verdict, o.response, o.product_code, o.created_at, p.name_enc, p.document_id_enc"
        " FROM app.onboardings o JOIN app.prospects p ON p.id = o.prospect_id WHERE o.id = %s",
        oid,
    )
    if not o:
        raise HTTPException(404, "onboarding not found")
    state = service.graph_state(oid)
    docs = state.get("documents") or {}
    pending = db.fetch_one(
        "SELECT id, proposal FROM app.reviews WHERE onboarding_id = %s AND kind = 'client' AND status = 'pending'", oid
    )
    steps = [
        {"agent": e["payload"].get("agent") or e["actor"], "type": e["type"], "ok": e["payload"].get("ok"), "ts": e["ts"]}
        for e in _timeline(oid)
        if e["type"] in ("agent_started", "agent_finished", "escalated", "human_input", "finalized", "received")
        and e["actor"] not in ("guard",)
    ]
    return {
        "status": o["status"],
        "verdict": o["verdict"],
        "response": o["response"],
        "product": o["product_code"],
        "applicant": pii.mask_name(pii.decrypt(o["name_enc"])),
        "document": pii.mask_document(pii.decrypt(o["document_id_enc"])),
        "documents": docs.get("documents") if o["status"] in ("approved", "approved_edd") else None,
        "information_request": pending["proposal"].get("applicant_request") if pending else None,
        "steps": steps,
    }


@app.get("/api/v1/onboarding/{oid}")
def get_onboarding(oid: uuid.UUID):
    return _public_view(str(oid))


@app.get("/api/v1/onboarding/{oid}/events")
async def stream(oid: uuid.UUID, request: Request):
    """SSE. Postgres LISTEN/NOTIFY wakes us; we also poll every 10s so a missed notify is harmless."""
    oid_s = str(oid)
    _status(oid_s)

    async def gen():
        last = 0
        async with await psycopg.AsyncConnection.connect(settings().database_url, autocommit=True) as conn:
            await conn.execute("LISTEN onboarding_events")
            while not await request.is_disconnected():
                for e in _timeline(oid_s, last):
                    last = e["id"]
                    yield {"event": "step", "id": str(e["id"]), "data": json.dumps(_public_event(e))}
                status = _status(oid_s)
                yield {"event": "status", "data": json.dumps(_public_view(oid_s))}
                if status in FINAL:
                    return
                try:
                    async for _ in conn.notifies(timeout=10, stop_after=1):
                        pass
                except TimeoutError:
                    pass

    return EventSourceResponse(gen(), ping=15)


def _public_event(e: dict) -> dict:
    return {"type": e["type"], "agent": e["payload"].get("agent") or e["actor"], "ok": e["payload"].get("ok"), "ts": e["ts"]}


class ApplicantReply(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    evidence: dict = Field(default_factory=dict, description='e.g. {"selfie": true}')


@app.post("/api/v1/onboarding/{oid}/messages", status_code=202)
def reply(oid: uuid.UUID, body: ApplicantReply):
    pending = db.fetch_one(
        "SELECT id FROM app.reviews WHERE onboarding_id = %s AND kind = 'client' AND status = 'pending'", str(oid)
    )
    if not pending:
        raise HTTPException(409, "no pending information request for this onboarding")
    enqueue(str(oid), {"message": body.message, "evidence": body.evidence, "by": "applicant"})
    return {"accepted": True}


# ---------------------------------------------------------------- analyst
analyst = auth.require("analyst", "admin")
admin = auth.require("admin")


@app.get("/api/v1/reviews")
def list_reviews(status: Literal["pending", "resolved"] = "pending", kind: str = "analyst", _=Depends(analyst)):
    rows = db.fetch(
        "SELECT r.id, r.onboarding_id, r.kind, r.reasons, r.status, r.action, r.created_at, r.resolved_at,"
        " o.product_code, o.status AS onboarding_status, p.name_enc"
        " FROM app.reviews r JOIN app.onboardings o ON o.id = r.onboarding_id JOIN app.prospects p ON p.id = o.prospect_id"
        " WHERE r.status = %s AND r.kind = %s ORDER BY r.created_at",
        status, kind,
    )
    return [{**{k: v for k, v in r.items() if k != "name_enc"}, "applicant": pii.mask_name(pii.decrypt(r["name_enc"]))} for r in rows]


@app.get("/api/v1/reviews/{rid}")
def review_detail(rid: uuid.UUID, _=Depends(analyst)):
    r = db.fetch_one("SELECT * FROM app.reviews WHERE id = %s", str(rid))
    if not r:
        raise HTTPException(404, "review not found")
    oid = str(r["onboarding_id"])
    s = service.graph_state(oid)
    return {
        "review": r,
        "onboarding": _public_view(oid),
        "identity": s.get("identity"),
        "risk": s.get("risk"),
        "documents": s.get("documents"),
        "supervisor_trail": s.get("supervisor_trail", []),
        "agent_notes": s.get("agent_notes", []),
        "errors": s.get("errors", []),
        "human_inputs": s.get("human_inputs", []),
        "timeline": _timeline(oid),
    }


class Decision(BaseModel):
    action: Literal["approve", "reject", "request_info"]
    notes: str = Field(min_length=5, max_length=2000)


@app.post("/api/v1/reviews/{rid}/decision", status_code=202)
def decide(rid: uuid.UUID, body: Decision, user=Depends(analyst)):
    r = db.fetch_one("SELECT onboarding_id, status, kind, reasons FROM app.reviews WHERE id = %s", str(rid))
    if not r or r["kind"] != "analyst":
        raise HTTPException(404, "review not found")
    if r["status"] != "pending":
        raise HTTPException(409, "review already resolved")
    oid = str(r["onboarding_id"])
    if "system_failure" in r["reasons"]:  # dead-lettered: graph can't resume, analyst decides directly
        db.execute("UPDATE app.reviews SET status='resolved', action=%s, notes=%s, resolved_by=%s, resolved_at=now() WHERE id=%s",
                   body.action, body.notes, user["sub"], str(rid))
        if body.action == "request_info":
            events.set_status(oid, "processing")
            enqueue(oid)
        else:
            verdict = "approved" if body.action == "approve" else "rejected"
            events.set_status(oid, verdict, verdict=verdict, response=_manual_response(verdict))
        return {"accepted": True}
    enqueue(oid, {"action": body.action, "notes": body.notes, "by": user["sub"]})
    return {"accepted": True}


def _manual_response(verdict: str) -> str:
    return ("Your application has been approved after manual review. We will contact you with the next steps."
            if verdict == "approved" else
            "We are unable to open the product through the digital channel at this time. Please visit a branch or call 1-800-555-0100.")


class Reveal(BaseModel):
    reason: str = Field(min_length=10, max_length=500)


@app.post("/api/v1/reviews/{rid}/reveal")
def reveal(rid: uuid.UUID, body: Reveal, user=Depends(analyst)):
    r = db.fetch_one(
        "SELECT o.id, p.name_enc, p.document_id_enc FROM app.reviews r JOIN app.onboardings o ON o.id = r.onboarding_id"
        " JOIN app.prospects p ON p.id = o.prospect_id WHERE r.id = %s", str(rid))
    if not r:
        raise HTTPException(404, "review not found")
    db.execute("INSERT INTO app.pii_reveals (onboarding_id, actor, reason) VALUES (%s,%s,%s)", r["id"], user["sub"], body.reason)
    events.emit(str(r["id"]), "pii_revealed", "analyst", by=user["sub"])  # reason stays in the audited table only
    return {"prospect_name": pii.decrypt(r["name_enc"]), "document_id": pii.decrypt(r["document_id_enc"])}


# ---------------------------------------------------------------- analytics + admin
K_ANON = 5


@app.get("/api/v1/analytics/summary")
def analytics(_=Depends(analyst)):
    t = db.fetch_one("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE status IN ('approved','approved_edd')) AS approved,
               count(*) FILTER (WHERE status = 'rejected') AS rejected,
               count(*) FILTER (WHERE status IN ('awaiting_analyst','awaiting_client','processing','received')) AS in_progress,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) FILTER (WHERE escalated) AS escalated,
               count(*) FILTER (WHERE guard_override) AS guard_overrides,
               count(*) FILTER (WHERE fallback_used) AS fallbacks,
               count(*) FILTER (WHERE status IN ('approved','approved_edd') AND NOT escalated) AS straight_through,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) FILTER (WHERE status IN ('approved','approved_edd','rejected')) AS p50_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) FILTER (WHERE status IN ('approved','approved_edd','rejected')) AS p95_ms,
               coalesce(sum(llm_cost_usd), 0) AS llm_cost_usd
        FROM analytics.fact_onboarding""")
    by_product = db.fetch("SELECT product_code, verdict, count(*) AS n FROM analytics.fact_onboarding GROUP BY 1,2 ORDER BY 1,2")
    reasons = db.fetch("SELECT r AS reason, count(*) AS n FROM analytics.fact_onboarding, unnest(escalation_reasons) r GROUP BY 1 ORDER BY 2 DESC")
    agents_ = db.fetch("SELECT agent, count(*) AS runs, round(avg(duration_ms)) AS avg_ms, count(*) FILTER (WHERE NOT ok) AS failures FROM analytics.fact_agent_step GROUP BY 1 ORDER BY 1")
    overrides = db.fetch_one("""SELECT count(*) FILTER (WHERE action IS NOT NULL) AS decided,
        count(*) FILTER (WHERE action = 'approve') AS approved, count(*) FILTER (WHERE action = 'reject') AS rejected
        FROM app.reviews WHERE kind='analyst' AND status='resolved'""")
    # k-anonymity: small groups suppressed in breakdowns
    suppress = lambda rows: [r if r["n"] >= K_ANON else {**r, "n": f"<{K_ANON}"} for r in rows]
    return {"totals": t, "by_product": suppress(by_product), "escalation_reasons": suppress(reasons), "agents": agents_,
            "analyst_decisions": overrides, "k_anonymity": K_ANON}


@app.get("/api/v1/admin/hard-rules")
def get_rules(_=Depends(analyst)):
    return db.fetch("SELECT * FROM policy.hard_rules ORDER BY code")


class RuleUpdate(BaseModel):
    value: float | str | bool | None = None
    active: bool | None = None


@app.put("/api/v1/admin/hard-rules/{code}")
def update_rule(code: str, body: RuleUpdate, user=Depends(admin)):
    r = db.fetch_one("SELECT kind FROM policy.hard_rules WHERE code = %s", code)
    if not r:
        raise HTTPException(404, "rule not found")
    if body.value is not None:
        if r["kind"] == "threshold" and not isinstance(body.value, (int, float)):
            raise HTTPException(422, "threshold rules need a number")
        db.execute("UPDATE policy.hard_rules SET value = %s::jsonb, version = version + 1, updated_at = now() WHERE code = %s",
                   json.dumps(body.value), code)
    if body.active is not None:
        db.execute("UPDATE policy.hard_rules SET active = %s, version = version + 1, updated_at = now() WHERE code = %s", body.active, code)
    log.info("hard_rule.updated", code=code, by=user["sub"])
    return db.fetch_one("SELECT * FROM policy.hard_rules WHERE code = %s", code)


@app.get("/api/v1/admin/knowledge")
def knowledge(_=Depends(analyst)):
    return db.fetch("SELECT d.source_path, d.title, d.doc_type, d.product_code, d.effective_date, d.ingested_at,"
                    " count(c.id) AS chunks FROM rag.documents d LEFT JOIN rag.chunks c ON c.document_id = d.id"
                    " GROUP BY d.id ORDER BY d.source_path")


@app.post("/api/v1/admin/knowledge/reingest")
def reingest(_=Depends(admin)):
    return rag.ingest()


@app.get("/api/v1/admin/search")
def search(q: str = Query(min_length=3), product: str | None = None, _=Depends(analyst)):
    return rag.search(q, product)


@app.get("/api/v1/admin/guard-check")
def guard_preview(_=Depends(analyst)):
    return [r.__dict__ for r in guard.load_rules()]
