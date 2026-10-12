r"""The orchestration graph.

supervisor (LLM) --decides--> [identity | risk | documentation]* (parallel fan-out) --> supervisor ...
                           \-> remediation (LLM) -> human_gate (interrupt: applicant or analyst) -> supervisor
                           \-> respond (LLM, template fallback) -> finalize -> END
The hard-rule guard runs inside the supervisor node on the LLM's output: it can only veto a verdict or stop the loop.
"""
import operator
import time
import uuid
from functools import lru_cache
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, interrupt
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import db, events, llm, pii, rag
from app.graph import agents, guard
from app.settings import settings


class OnboardingState(TypedDict, total=False):
    onboarding_id: str
    prospect_ref: str  # -> PII vault; no raw PII in state/checkpoints
    product: str
    identity: dict | None
    risk: dict | None
    documents: dict | None
    extra_evidence: bool
    policy_context: list[dict]
    supervisor_trail: Annotated[list[dict], operator.add]
    agent_notes: Annotated[list[dict], operator.add]
    errors: Annotated[list[dict], operator.add]
    human_inputs: Annotated[list[dict], operator.add]
    supervisor_uncertainty: float
    route: list[str]
    proposal: dict | None
    forced_reasons: list[str]
    step_count: int
    verdict: str | None
    response: str | None


def _cfg(usage: llm.Usage) -> dict:
    return {"callbacks": [usage, *_tracing()]}


def _tracing() -> list:
    if not settings().langfuse_public_key:
        return []
    from langfuse.langchain import CallbackHandler  # optional dependency, profile "observability"

    return [CallbackHandler()]


def _fallback_emitter(oid: str, agent: str):
    return lambda frm, to: events.emit(oid, "fallback_used", agent, kind="llm_model", from_model=frm, to_model=to)


def _usage_event(oid: str, agent: str, usage: llm.Usage, started: float, ok: bool, **extra) -> None:
    events.emit(oid, "agent_finished", agent, agent=agent, ok=ok, duration_ms=int((time.monotonic() - started) * 1000), **extra)
    if usage.input_tokens:
        events.emit(oid, "llm_usage", agent, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, cost_usd=usage.cost_usd)


# ---------------------------------------------------------------- nodes
def intake(state: OnboardingState) -> dict:
    oid = state["onboarding_id"]
    events.set_status(oid, "processing")
    ctx = rag.search("onboarding procedure identity verification risk screening order of checks escalation", state["product"], k=3)
    return {
        "policy_context": [{"source": h["source"], "content": h["content"]} for h in ctx["hits"]],
        "step_count": 0,
        "extra_evidence": False,
    }


def supervisor(state: OnboardingState) -> dict:
    oid, started, usage = state["onboarding_id"], time.monotonic(), llm.Usage()
    step = state.get("step_count", 0) + 1
    rules = guard.load_rules()
    events.emit(oid, "agent_started", "supervisor", agent="supervisor", step=step)

    # Step budget etc. (stop_loop) is checked before spending another LLM call.
    stop = guard.check(rules, {**state, "step_count": step})
    if stop:
        return _veto(oid, step, stop, None, started, usage)

    try:
        d = agents.decide(state, [r.description for r in rules], _cfg(usage), _fallback_emitter(oid, "supervisor"))
    except llm.LLMUnavailable as e:
        # Code does NOT take over routing: a human does.
        events.emit(oid, "fallback_used", "supervisor", kind="llm_unavailable", error=str(e)[:300])
        _usage_event(oid, "supervisor", usage, started, ok=False)
        return {
            "step_count": step,
            "route": ["human_gate"],
            "proposal": _system_proposal(["llm_unavailable"], "All LLM models are unavailable; the orchestrator cannot reason about this case."),
            "supervisor_trail": [{"step": step, "next": ["human_gate"], "rationale": "LLM unavailable", "vetoed": False}],
        }

    decision = d.model_dump()
    events.emit(oid, "supervisor_decision", "supervisor", step=step, **decision)
    probe = {**state, "step_count": step, "supervisor_uncertainty": d.uncertainty}
    if d.next == ["respond"]:
        violations = guard.check(rules, probe, d.verdict)
        if violations:
            return _veto(oid, step, violations, decision, started, usage, d.uncertainty)
    _usage_event(oid, "supervisor", usage, started, ok=True)
    return {
        "step_count": step,
        "supervisor_uncertainty": d.uncertainty,
        "route": d.next,
        "verdict": d.verdict,
        "forced_reasons": [],
        "supervisor_trail": [{"step": step, **decision, "vetoed": False}],
    }


def _veto(oid, step, violations, decision, started, usage, uncertainty=None) -> dict:
    reasons = [v.rule for v in violations]
    events.emit(oid, "guard_override", "guard", step=step, proposed=decision, violations=[v.__dict__ for v in violations])
    _usage_event(oid, "supervisor", usage, started, ok=True, vetoed=True)
    out = {
        "step_count": step,
        "route": ["remediation"],
        "verdict": None,
        "forced_reasons": reasons,
        "supervisor_trail": [{"step": step, **(decision or {}), "vetoed": True, "violations": reasons}],
    }
    if uncertainty is not None:
        out["supervisor_uncertainty"] = uncertainty
    return out


def _ctx(state: OnboardingState) -> agents.RunContext:
    return agents.RunContext(
        prospect_id=state["prospect_ref"],
        product=state["product"],
        risk_level=(state.get("risk") or {}).get("risk_level"),
        extra_evidence=state.get("extra_evidence", False),
    )


def _specialist(name: str, tool: str, state_key: str, task: str):
    def node(state: OnboardingState) -> dict:
        oid, started, usage, ctx = state["onboarding_id"], time.monotonic(), llm.Usage(), _ctx(state)
        events.emit(oid, "agent_started", name, agent=name)
        try:
            res = agents.run_agent(name, task, ctx, _cfg(usage), _fallback_emitter(oid, name))
            note = res["messages"][-1].content
        except llm.LLMUnavailable as e:
            # The specialist's LLM is down: report it; the supervisor (or a human) decides what next.
            _usage_event(oid, name, usage, started, ok=False)
            return {"errors": [{"agent": name, "error": f"LLM unavailable: {str(e)[:200]}"}]}
        raw = ctx.raw.get(tool)  # D4: the raw tool output, not the LLM's paraphrase
        if raw is not None:
            events.emit(oid, "tool_result", name, tool=tool, result=raw)
        for err in ctx.errors:
            events.emit(oid, "tool_error", name, **err)
        _usage_event(oid, name, usage, started, ok=raw is not None)
        out: dict[str, Any] = {"agent_notes": [{"agent": name, "note": str(note)[:800]}], "errors": ctx.errors}
        if raw is not None:
            out[state_key] = raw
        return out

    node.__name__ = name
    return node


identity = _specialist("identity", "verify_identity", "identity", "Verify the identity of applicant PERSON_1 (document DOC_1).")
risk = _specialist("risk", "check_risk_lists", "risk", "Screen applicant PERSON_1 (document DOC_1) against risk lists.")
documentation = _specialist(
    "documentation", "prepare_documentation", "documents", "Prepare the required onboarding documentation for this applicant."
)


def remediation(state: OnboardingState) -> dict:
    oid, started, usage, ctx = state["onboarding_id"], time.monotonic(), llm.Usage(), _ctx(state)
    forced = state.get("forced_reasons") or []
    events.emit(oid, "agent_started", "remediation", agent="remediation", forced_reasons=forced)
    situation = {
        "product": state["product"],
        "identity": state.get("identity"),
        "risk": state.get("risk"),
        "errors": state.get("errors", [])[-5:],
        "human_inputs": state.get("human_inputs", []),
        "supervisor_rationale": (state.get("supervisor_trail") or [{}])[-1].get("rationale"),
        "hard_rule_violations": forced,
    }
    task = f"Situation: {situation}"
    if forced:
        task += "\nFORCED_TARGET: analyst (a hard rule requires human review)."
    try:
        res = agents.run_agent("remediation", task, ctx, _cfg(usage), _fallback_emitter(oid, "remediation"), agents.Proposal)
        proposal = res["structured_response"].model_dump()
    except llm.LLMUnavailable:
        events.emit(oid, "fallback_used", "remediation", kind="template_proposal")
        proposal = _system_proposal(forced or ["needs_review"], "Automatic remediation unavailable; manual review required.")
    if forced:
        proposal["target"] = "analyst"
        proposal["reasons"] = list(dict.fromkeys([*forced, *proposal["reasons"]]))
    proposal["citations"] = list(dict.fromkeys(proposal.get("citations", []) + ctx.citations))
    events.emit(oid, "remediation_proposed", "remediation", proposal=proposal)
    _usage_event(oid, "remediation", usage, started, ok=True)
    return {"proposal": proposal, "route": ["human_gate"]}


def _system_proposal(reasons: list[str], summary: str) -> dict:
    return {
        "target": "analyst",
        "reasons": reasons,
        "summary": summary,
        "recommended_actions": ["Review the case manually", "Approve, reject or request information"],
        "applicant_request": None,
        "citations": [],
    }


def human_gate(state: OnboardingState) -> dict:
    """Pause for a human. NOTE: LangGraph re-runs this node from the top on resume, so side effects are idempotent."""
    oid, p = state["onboarding_id"], state["proposal"] or _system_proposal(["needs_review"], "Review required.")
    kind = p["target"]
    review_id = str(uuid.uuid5(uuid.UUID(oid), f"review-{state.get('step_count', 0)}"))
    inserted = db.fetch(
        "INSERT INTO app.reviews (id, onboarding_id, kind, reasons, proposal) VALUES (%s,%s,%s,%s,%s::jsonb)"
        " ON CONFLICT (id) DO NOTHING RETURNING id",
        review_id, oid, "analyst" if kind == "analyst" else "client", p["reasons"], events.json.dumps(p),
    )
    if inserted:
        events.set_status(oid, "awaiting_analyst" if kind == "analyst" else "awaiting_client")
        events.emit(oid, "escalated", "orchestrator", review_id=review_id, target=kind, reasons=p["reasons"])

    answer = interrupt({"review_id": review_id, "target": kind, "proposal": p})

    if kind == "analyst":
        entry = {"actor": "analyst", "action": answer["action"], "notes": pii.redact_free_text(answer.get("notes") or "")}
    else:
        entry = {"actor": "applicant", "message": pii.redact_free_text(answer.get("message") or ""), "evidence": answer.get("evidence") or {}}
    db.execute(
        "UPDATE app.reviews SET status='resolved', action=%s, notes=%s, resolved_by=%s, resolved_at=now()"
        " WHERE id=%s AND status='pending'",
        entry.get("action", "replied"), entry.get("notes") or entry.get("message"), answer.get("by", kind), review_id,
    )
    events.set_status(oid, "processing")
    events.emit(oid, "human_input", kind, **entry)
    out: dict[str, Any] = {"human_inputs": [entry], "proposal": None, "forced_reasons": []}
    if entry.get("evidence"):
        out["extra_evidence"] = True
    return out


TEMPLATES = {
    "approved": "Hello PERSON_1, good news: your {product} application is approved. To complete the opening, please "
    "provide: {docs}. We validate documents within 1 business day.",
    "approved_edd": "Hello PERSON_1, your {product} application is approved. Our onboarding policy requires some "
    "additional documentation: {docs}. We validate documents within 1 business day.",
    "rejected": "Hello PERSON_1, thank you for your interest. We are unable to open the {product} through the digital "
    "channel at this time. You are welcome to visit a branch or call 1-800-555-0100 so we can help you further.",
}


def respond(state: OnboardingState) -> dict:
    oid, started, usage, ctx = state["onboarding_id"], time.monotonic(), llm.Usage(), _ctx(state)
    verdict = state["verdict"]
    events.emit(oid, "agent_started", "response", agent="response")
    docs = state.get("documents") or {}
    label = docs.get("product_label") or state["product"]
    doc_list = ", ".join(d["label"] for d in docs.get("documents", [])) or "your national ID"
    task = (
        f"Verdict: {verdict}. Product: {label}. Required documents: {doc_list}. "
        f"Analyst notes, if any: {[h.get('notes') for h in state.get('human_inputs', []) if h.get('actor') == 'analyst']}"
    )
    try:
        res = agents.run_agent("response", task, ctx, _cfg(usage), _fallback_emitter(oid, "response"))
        text = str(res["messages"][-1].content).strip()
        if verdict != "rejected" and "PERSON_1" not in text:
            text = "Hello PERSON_1,\n" + text
    except llm.LLMUnavailable:
        events.emit(oid, "fallback_used", "response", kind="template_response")
        text = TEMPLATES[verdict].format(product=label, docs=doc_list)
    _usage_event(oid, "response", usage, started, ok=True)
    return {"response": text}


def finalize(state: OnboardingState) -> dict:
    oid, verdict = state["onboarding_id"], state["verdict"]
    name, doc = pii.load_prospect(state["prospect_ref"])
    text = pii.reidentify(state["response"] or "", name.split()[0], doc)  # address by first name only
    events.set_status(oid, verdict, verdict=verdict, response=text)
    if verdict == "rejected":
        db.execute(
            "UPDATE app.prospects SET purge_after = now() + make_interval(days => %s)"
            " WHERE id = %s",
            settings().pii_retention_days, state["prospect_ref"],
        )
    events.emit(oid, "finalized", "orchestrator", verdict=verdict)
    return {}


# ---------------------------------------------------------------- wiring
def _dispatch(state: OnboardingState) -> list[str]:
    return state["route"]


def build(checkpointer=None):
    retry = RetryPolicy(max_attempts=2, initial_interval=0.5, jitter=True)
    g = StateGraph(OnboardingState)
    g.add_node("intake", intake, retry_policy=retry)
    g.add_node("supervisor", supervisor, retry_policy=retry)
    for name, fn in [("identity", identity), ("risk", risk), ("documentation", documentation)]:
        g.add_node(name, fn, retry_policy=retry)
        g.add_edge(name, "supervisor")
    g.add_node("remediation", remediation, retry_policy=retry)
    g.add_node("human_gate", human_gate)
    g.add_node("respond", respond, retry_policy=retry)
    g.add_node("finalize", finalize, retry_policy=retry)
    g.add_edge(START, "intake")
    g.add_edge("intake", "supervisor")
    g.add_conditional_edges(
        "supervisor", _dispatch, ["identity", "risk", "documentation", "remediation", "respond", "human_gate"]
    )
    g.add_edge("remediation", "human_gate")
    g.add_edge("human_gate", "supervisor")
    g.add_edge("respond", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


@lru_cache
def checkpointer() -> PostgresSaver:
    pool = ConnectionPool(
        settings().database_url,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row, "options": "-c search_path=langgraph"},
        max_size=10,
        open=True,
    )
    serde = EncryptedSerializer.from_pycryptodome_aes(key=settings().langgraph_aes_key.encode())
    saver = PostgresSaver(pool, serde=serde)  # type: ignore[arg-type]
    saver.setup()
    return saver


@lru_cache
def graph():
    return build(checkpointer())
