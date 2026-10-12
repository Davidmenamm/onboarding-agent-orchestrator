"""Hard-rule guard. Evaluates ONLY thresholds / tags / presence from policy.hard_rules.

It never chooses an agent or an order (that is the LLM supervisor's job). It can only:
  - veto a final verdict the supervisor proposed, or
  - stop the loop (step budget) -> human review.
"""
import operator
from dataclasses import dataclass

from app import db

_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "==": operator.eq, "!=": operator.ne}
APPROVALS = {"approved", "approved_edd"}


@dataclass(frozen=True)
class Rule:
    code: str
    kind: str
    field: str
    operator: str
    value: object
    effect: str
    description: str


@dataclass(frozen=True)
class Violation:
    rule: str
    effect: str
    message: str


def load_rules() -> list[Rule]:
    rows = db.fetch(
        "SELECT code, kind, field, operator, value, effect, description FROM policy.hard_rules WHERE active ORDER BY code"
    )
    return [Rule(**r) for r in rows]  # psycopg already decodes jsonb


def _get(state: dict, path: str):
    cur: object = state
    for part in path.split("."):
        if not isinstance(cur, dict) or cur.get(part) is None:
            return None
        cur = cur[part]
    return cur


def triggered(rule: Rule, state: dict) -> bool:
    val = _get(state, rule.field)
    if rule.operator == "missing":
        return val is None
    if val is None:  # absent data is the presence rules' job
        return False
    return bool(_OPS[rule.operator](val, rule.value))


def analyst_decision(state: dict) -> str | None:
    """Latest analyst action, if any (approve/reject)."""
    for h in reversed(state.get("human_inputs") or []):
        if h.get("actor") == "analyst" and h.get("action") in ("approve", "reject"):
            return h["action"]
    return None


def check(rules: list[Rule], state: dict, verdict: str | None = None) -> list[Violation]:
    """verdict=None -> per-step check (only stop_loop). verdict=X -> also validate the proposed final verdict."""
    out: list[Violation] = []
    human = analyst_decision(state)
    for r in rules:
        if not triggered(r, state):
            continue
        if r.effect == "stop_loop":
            out.append(Violation(r.code, r.effect, r.description))
        elif verdict is None:
            continue
        elif r.effect == "require_human_review" and human is None:
            out.append(Violation(r.code, r.effect, r.description))
        elif r.effect == "block_approval" and verdict in APPROVALS and human != "approve":
            out.append(Violation(r.code, r.effect, r.description))
    # Built-in invariant (a tag check, not routing): an analyst decision is binding.
    if verdict is not None and human == "reject" and verdict != "rejected":
        out.append(Violation("ANALYST_BINDING", "block_approval", "Analyst rejected; verdict must be rejected."))
    if verdict is not None and human == "approve" and verdict == "rejected":
        out.append(Violation("ANALYST_BINDING", "block_approval", "Analyst approved; verdict cannot be rejected."))
    return out
