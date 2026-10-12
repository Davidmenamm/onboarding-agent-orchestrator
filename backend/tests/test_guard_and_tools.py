import pytest

from app import pii, tools
from app.graph import guard

RULES = guard.load_rules()  # from the real seeded DB: tests the stored rules, not a copy


def st(identity=None, risk=None, step_count=1, human=()):
    return {"identity": identity, "risk": risk, "step_count": step_count, "human_inputs": list(human)}


OK_ID = {"verified": True, "confidence": 0.95}
LOW = {"risk_level": "low", "matches": []}


def codes(v):
    return {x.rule for x in v}


def test_happy_path_passes():
    assert guard.check(RULES, st(OK_ID, LOW), "approved") == []


@pytest.mark.parametrize("conf,blocked", [(0.79, True), (0.8, False), (0.95, False)])
def test_confidence_threshold_boundary(conf, blocked):
    v = guard.check(RULES, st({"verified": True, "confidence": conf}, LOW), "approved")
    assert ("HR1_min_identity_confidence" in codes(v)) is blocked


def test_low_confidence_even_rejection_needs_analyst():
    v = guard.check(RULES, st({"verified": True, "confidence": 0.65}, LOW), "rejected")
    assert codes(v) == {"HR1_min_identity_confidence"}


def test_high_risk_needs_analyst_then_analyst_can_approve():
    s = st(OK_ID, {"risk_level": "high", "matches": ["x"]})
    assert "HR2_high_risk" in codes(guard.check(RULES, s, "approved"))
    s["human_inputs"] = [{"actor": "analyst", "action": "approve"}]
    assert guard.check(RULES, s, "approved") == []


def test_medium_risk_is_not_a_hard_rule():
    assert guard.check(RULES, st(OK_ID, {"risk_level": "medium", "matches": []}), "approved_edd") == []


def test_unverified_blocks_approval_but_allows_rejection():
    s = st({"verified": False, "confidence": 0.92}, LOW)
    assert "HR3_identity_not_verified" in codes(guard.check(RULES, s, "approved"))
    assert guard.check(RULES, s, "rejected") == []


def test_missing_results_fail_closed():
    assert {"HR4a_identity_missing", "HR4b_risk_missing"} <= codes(guard.check(RULES, st(), "approved"))


def test_step_budget_stops_loop_and_routing_is_never_checked():
    assert guard.check(RULES, st(step_count=3)) == []  # no verdict -> routing is the LLM's business
    assert codes(guard.check(RULES, st(step_count=16))) == {"HR5_step_budget"}


def test_analyst_decision_is_binding():
    s = st(OK_ID, LOW, human=[{"actor": "analyst", "action": "reject"}])
    assert "ANALYST_BINDING" in codes(guard.check(RULES, s, "approved"))


def test_mock_scenarios():
    assert tools.verify_identity("1712345678") == {"verified": True, "confidence": 0.95}
    assert tools.verify_identity("1712345671")["confidence"] == 0.65
    assert tools.verify_identity("1712345671", extra_evidence=True)["confidence"] == 0.91
    assert tools.verify_identity("1712345672")["verified"] is False
    assert tools.check_risk_lists("Juan Perez", "1712345674")["risk_level"] == "high"
    with pytest.raises(tools.ToolTimeout):
        tools.verify_identity("1712345675")


def test_documentation_adds_edd_docs():
    from app import rag

    rag.ingest()
    base = tools.prepare_documentation("cuenta_ahorros", {"risk_level": "low"})
    edd = tools.prepare_documentation("cuenta_ahorros", {"risk_level": "medium"})
    assert len(edd["documents"]) == len(base["documents"]) + 1 and edd["enhanced_due_diligence"]


def test_pii_roundtrip_and_pseudonyms():
    blob = pii.encrypt("Juan Perez")
    assert b"Juan" not in blob and pii.decrypt(blob) == "Juan Perez"
    assert pii.lookup_hash("1712345678") == pii.lookup_hash("1712345678") != "1712345678"
    text = pii.pseudonymize("Client juan perez, id 1712345678, Perez family", "Juan Perez", "1712345678")
    assert "Perez" not in text and "1712345678" not in text and "PERSON_1" in text
    assert pii.mask_document("1712345678") == "******5678"


def test_presidio_redacts_free_text():
    out = pii.redact_free_text("Hi, I am Maria Gonzalez, email maria@mail.com, my id is 0912345678")
    assert "Maria Gonzalez" not in out and "maria@mail.com" not in out and "0912345678" not in out
