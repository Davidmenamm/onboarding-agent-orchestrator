"""Specialist sub-agents. The orchestrator owns the tool allowlist (AGENT_TOOLS): each agent is built with
ONLY its tools bound, and tools receive PII from the vault internally, so the LLM never sees or passes PII."""
import json
from dataclasses import dataclass, field
from typing import Literal

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field, model_validator
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from app import llm, pii, rag, tools

AGENT_TOOLS: dict[str, list[str]] = {
    "identity": ["verify_identity"],
    "risk": ["check_risk_lists"],
    "documentation": ["prepare_documentation", "search_policies"],
    "remediation": ["search_policies"],
    "response": ["search_policies"],
}

AgentName = Literal["identity", "risk", "documentation", "remediation", "respond"]


# ---------------------------------------------------------------- structured outputs
class RoutingDecision(BaseModel):
    """The supervisor's decision for the next step."""

    next: list[AgentName] = Field(min_length=1, description="Agent(s) to run next. Independent agents may run together.")
    verdict: Literal["approved", "approved_edd", "rejected"] | None = Field(
        None, description="Required when next == ['respond']: the final verdict."
    )
    rationale: str = Field(description="Short reasoning for this decision.")
    uncertainty: float = Field(ge=0, le=1, description="Your own uncertainty about this decision, 0 = certain.")

    @model_validator(mode="after")
    def _shape(self):  # schema validity only, not policy
        if "respond" in self.next and len(self.next) > 1:
            raise ValueError("respond must be the only next step")
        if "remediation" in self.next:  # remediation pauses the case; other work waits until it resumes
            self.next = ["remediation"]
        if self.next == ["respond"] and self.verdict is None:
            raise ValueError("respond requires a verdict")
        self.next = list(dict.fromkeys(self.next))
        return self


class Proposal(BaseModel):
    """Remediation proposal for an ambiguous or failed verification."""

    target: Literal["applicant", "analyst"] = Field(description="Who must act: the applicant or an onboarding analyst.")
    reasons: list[str] = Field(description="Machine-friendly reasons, e.g. low_identity_confidence, high_risk.")
    summary: str = Field(description="Explanation for the analyst of what happened and why.")
    recommended_actions: list[str] = Field(description="Concrete actions recommended, in order.")
    applicant_request: str | None = Field(None, description="If target=applicant: what to ask them, neutral wording.")
    citations: list[str] = Field(default_factory=list, description="Policy sources used (file paths).")


# ---------------------------------------------------------------- run context + tools
@dataclass
class RunContext:
    prospect_id: str
    product: str
    risk_level: str | None = None
    extra_evidence: bool = False
    raw: dict = field(default_factory=dict)  # raw tool outputs -> state (D4)
    errors: list = field(default_factory=list)
    citations: list = field(default_factory=list)


_retry = retry(
    retry=retry_if_exception_type(tools.ToolTimeout),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.2, max=2),
    reraise=True,
)


def build_tools(agent: str, ctx: RunContext) -> list[BaseTool]:
    def guarded(name: str, fn):
        try:
            out = _retry(fn)()
            ctx.raw[name] = out
            return out
        except Exception as e:  # exhausted retries -> visible error, the supervisor decides what to do
            ctx.errors.append({"tool": name, "error": f"{type(e).__name__}: {e}", "attempts": 3})
            return {"error": f"{type(e).__name__}: {e}", "attempts": 3}

    @tool
    def verify_identity() -> dict:
        """Verify the applicant's national ID against the civil registry. Returns verified and confidence (0-1)."""
        _, doc = pii.load_prospect(ctx.prospect_id)
        return guarded("verify_identity", lambda: tools.verify_identity(doc, ctx.extra_evidence))

    @tool
    def check_risk_lists() -> dict:
        """Screen the applicant against sanctions, PEP and watch lists. Returns risk_level and matches."""
        name, doc = pii.load_prospect(ctx.prospect_id)
        out = guarded("check_risk_lists", lambda: tools.check_risk_lists(name, doc))
        if "matches" in out:  # matches may echo the name -> pseudonymize before the LLM sees it
            out = {**out, "matches": [pii.pseudonymize(m, name, doc) for m in out["matches"]]}
            ctx.raw["check_risk_lists"] = out
        return out

    @tool
    def prepare_documentation() -> dict:
        """Prepare the list of documents required for this product according to bank policy and the risk level."""
        return guarded(
            "prepare_documentation",
            lambda: tools.prepare_documentation(ctx.product, {"risk_level": ctx.risk_level}),
        )

    @tool
    def search_policies(query: str) -> str:
        """Search the bank's policy corpus (products, KYC/AML procedures, playbooks, communication guidelines)."""
        res = rag.search(query, ctx.product, k=4)
        ctx.citations += [h["source"] for h in res["hits"]]
        return json.dumps(
            {"low_relevance": res["low_relevance"], "hits": [{k: h[k] for k in ("source", "section", "content", "relevance")} for h in res["hits"]]}
        )

    available = {t.name: t for t in (verify_identity, check_risk_lists, prepare_documentation, search_policies)}
    return [available[n] for n in AGENT_TOOLS[agent]]


# ---------------------------------------------------------------- prompts
PROMPTS = {
    "identity": "ROLE: identity. You are the identity verification specialist of a bank onboarding team. "
    "Call verify_identity exactly once, then report the result in one or two sentences: verified, confidence, "
    "and whether confidence meets the 0.8 policy threshold. If the tool returned an error, report it plainly.",
    "risk": "ROLE: risk. You are the AML risk screening specialist. Call check_risk_lists exactly once, then "
    "summarize risk level and matches in one or two sentences. If the tool returned an error, report it plainly.",
    "documentation": "ROLE: documentation. You prepare onboarding documentation according to bank policy. "
    "Call prepare_documentation, optionally search_policies for context, then summarize the required documents "
    "and whether enhanced due diligence applies, in at most three sentences.",
    "remediation": "ROLE: remediation. A verification failed or is ambiguous. Use search_policies to find the "
    "applicable playbook, then propose a concrete solution following it. Choose target=applicant when the "
    "applicant can resolve it (e.g. a biometric selfie), target=analyst when a human decision is needed "
    "(high risk, repeated low confidence, provider unavailable, or FORCED_TARGET says so). Never reveal "
    "watchlist details in applicant_request. Cite the policy sources.",
    "response": "ROLE: response. Write the final message to the applicant, addressed to PERSON_1 by that "
    "placeholder. Use search_policies for the customer communication guidelines and follow them. Plain text, "
    "under 160 words, no internal jargon (KYC, AML, EDD, PEP), never mention watchlists or fraud.",
}

SUPERVISOR_PROMPT = """You are the orchestrator of a bank's digital onboarding team. You decide, step by step,
which specialist agent runs next, and finally the verdict. You manage uncertainty using the confidences,
risk levels and policy context you are given.

Agents you can dispatch:
- identity: verifies the national ID -> {verified, confidence 0-1}
- risk: screens sanctions/PEP lists -> {risk_level low|medium|high, matches}
- documentation: required documents from policy (needs the risk result to decide enhanced due diligence)
- remediation: proposes a solution when something failed or is ambiguous, and hands it to the applicant
  or to a human analyst (the case pauses until they answer)
- respond: final step; writes the applicant message. Requires a verdict:
  approved | approved_edd (eligible with enhanced due diligence) | rejected

You may dispatch independent agents together (e.g. ["identity","risk"]). remediation and respond always run alone. You may re-run an agent (e.g. after
a tool error, or after the applicant provided extra evidence). Avoid repeating steps without new information.

Hard rules enforced by code (if your verdict violates one it is vetoed and the case goes to an analyst):
{hard_rules}

An analyst decision in human_inputs is binding. Be honest about your uncertainty (0-1).
Return a RoutingDecision."""


def supervisor_messages(state: dict, hard_rules: list[str]) -> list:
    view = {
        "product": state["product"],
        "applicant": "PERSON_1 (document DOC_1)",
        "identity": state.get("identity"),
        "risk": state.get("risk"),
        "documents_prepared": bool(state.get("documents")),
        "enhanced_due_diligence": (state.get("documents") or {}).get("enhanced_due_diligence"),
        "extra_evidence_provided": state.get("extra_evidence", False),
        "errors": state.get("errors", [])[-5:],
        "human_inputs": state.get("human_inputs", []),
        "previous_decisions": [
            {k: d.get(k) for k in ("next", "verdict", "rationale", "vetoed")} for d in state.get("supervisor_trail", [])[-6:]
        ],
        "agent_notes": state.get("agent_notes", [])[-6:],
        "policy_context": [{"source": c["source"], "content": c["content"][:600]} for c in state.get("policy_context", [])],
        "step": state.get("step_count", 0),
    }
    return [
        ("system", SUPERVISOR_PROMPT.replace("{hard_rules}", "\n".join(f"- {r}" for r in hard_rules))),
        HumanMessage(json.dumps(view, default=str)),
    ]


# ---------------------------------------------------------------- runners
def structured(model, schema: type[BaseModel], messages: list, config: dict) -> BaseModel:
    """Structured output via a required tool call. Z.AI rejects tool_choice=<name>, so we use "required"."""
    msg = model.bind_tools([schema], tool_choice="required").invoke(messages, config=config)
    calls = [c for c in msg.tool_calls if c["name"] == schema.__name__] or msg.tool_calls
    if not calls:
        raise ValueError(f"model returned no {schema.__name__}")
    return schema.model_validate(calls[0]["args"])


def run_agent(agent: str, task: str, ctx: RunContext, config: dict, on_fallback, response_format=None) -> dict:
    def go(model):
        a = create_agent(model, tools=build_tools(agent, ctx), system_prompt=PROMPTS[agent])
        out = a.invoke({"messages": [HumanMessage(task)]}, config={**config, "recursion_limit": 12})
        if response_format:
            transcript = "\n".join(f"{m.type}: {m.content}" for m in out["messages"])[-6000:]
            out["structured_response"] = structured(
                model, response_format,
                [("system", PROMPTS[agent]), HumanMessage(f"{transcript}\n\nNow return your final {response_format.__name__}.")],
                config,
            )
        return out

    return llm.call(go, on_fallback)


def decide(state: dict, hard_rules: list[str], config: dict, on_fallback) -> RoutingDecision:
    msgs = supervisor_messages(state, hard_rules)
    return llm.call(
        lambda m: structured(m, RoutingDecision, msgs, config),
        on_fallback,
    )
