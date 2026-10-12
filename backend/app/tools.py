"""Mocked bank tools. Deterministic by the last digit of document_id (see PLAN.md §3)."""
from app import rag


class ToolTimeout(Exception):
    pass


def verify_identity(document_id: str, extra_evidence: bool = False) -> dict:
    last = document_id[-1]
    if last == "5":
        raise ToolTimeout("identity provider did not answer within 10s")
    if last == "1":
        return {"verified": True, "confidence": 0.91 if extra_evidence else 0.65}
    if last == "2":
        return {"verified": False, "confidence": 0.92}
    return {"verified": True, "confidence": 0.95}


def check_risk_lists(name: str, document_id: str) -> dict:
    last = document_id[-1]
    if last == "3":
        return {"risk_level": "medium", "matches": ["PEP relative: partial match 61%"]}
    if last == "4":
        return {"risk_level": "high", "matches": [f"OFAC SDN: similar name to '{name}' 87%"]}
    return {"risk_level": "low", "matches": []}


def prepare_documentation(product: str, client_data: dict) -> dict:
    """Required documents, read from the policy corpus (RAG store) - the single source of truth.
    EDD extras when risk is medium/high. Policy passages are attached as citations."""
    policy = rag.product_policy(product)
    if not policy:
        raise ValueError(f"no policy document for product {product!r}")
    meta = policy["metadata"]
    edd = client_data.get("risk_level") in ("medium", "high")
    docs = [{**d, "edd": False} for d in meta["required_documents"]]
    if edd:
        docs += [{**d, "edd": True} for d in meta.get("edd_documents", [])]
    cites = rag.search(f"{meta['label']} required documentation", product, k=2)["hits"]
    return {
        "product": product,
        "product_label": meta["label"],
        "enhanced_due_diligence": edd,
        "documents": docs,
        "policy_source": policy["source_path"],
        "citations": [{"source": h["source"], "section": h["section"]} for h in cites],
    }
