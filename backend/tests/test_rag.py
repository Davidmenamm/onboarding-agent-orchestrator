"""RAG eval on a small golden set + single-source-of-truth checks."""
import pytest

from app import rag, tools

GOLDEN = [  # (question, product, expected source)
    ("What should we do when identity confidence is below 0.8?", None, "playbooks/low-identity-confidence.md"),
    ("Sanctions list match found for applicant", None, "playbooks/risk-list-match.md"),
    ("What documents are required under enhanced due diligence?", None, "procedures/enhanced-due-diligence.md"),
    ("credit limit for medium risk credit card applicants", "tarjeta_credito", "products/credit-card.md"),
    ("How should we word a rejection message?", None, "guidelines/customer-communication.md"),
    ("Can we send personal data to an external AI service?", None, "guidelines/data-protection.md"),
    ("when are checkbooks issued", "cuenta_corriente", "products/checking-account.md"),
    ("identity provider says the identity does not match", None, "playbooks/identity-not-verified.md"),
]


@pytest.fixture(scope="module", autouse=True)
def corpus():
    rag.ingest()


def test_ingest_is_idempotent():
    assert rag.ingest()["added"] == 0 and rag.ingest()["updated"] == 0


def test_retrieval_hit_at_3_and_mrr():
    rr = []
    for q, product, expected in GOLDEN:
        sources = [h["source"] for h in rag.search(q, product, k=5)["hits"]]
        rr.append(1 / (sources.index(expected) + 1) if expected in sources else 0)
    hit3 = sum(r >= 1 / 3 for r in rr) / len(rr)
    mrr = sum(rr) / len(rr)
    print(f"hit@3={hit3:.2f} MRR={mrr:.2f}")
    assert hit3 >= 0.85 and mrr >= 0.7


def test_off_topic_query_flags_low_relevance():
    assert rag.search("best pizza recipe with pineapple")["low_relevance"]


def test_documentation_tool_reads_the_rag_store():
    out = tools.prepare_documentation("cuenta_ahorros", {"risk_level": "medium"})
    codes = [d["code"] for d in out["documents"]]
    assert codes[-1] == "source_of_funds" and out["policy_source"] == "products/savings-account.md"
    assert out["citations"]
    assert {p["code"] for p in rag.products()} == {"cuenta_ahorros", "cuenta_corriente", "tarjeta_credito"}
