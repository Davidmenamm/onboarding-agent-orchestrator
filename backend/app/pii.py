"""PII handling: encryption at rest, lookup hash, pseudonyms for the LLM, Presidio for free text, masking."""
import base64
import hashlib
import hmac
import os
import re
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.settings import settings

PERSON, DOC = "PERSON_1", "DOC_1"  # one prospect per onboarding -> fixed pseudonyms


def _key() -> bytes:
    return base64.b64decode(settings().pii_encryption_key)


def encrypt(plain: str) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(_key()).encrypt(nonce, plain.encode(), None)


def decrypt(blob: bytes) -> str:
    return AESGCM(_key()).decrypt(blob[:12], blob[12:], None).decode()


def lookup_hash(document_id: str) -> str:
    return hmac.new(settings().pii_hash_pepper.encode(), document_id.encode(), hashlib.sha256).hexdigest()


def mask_name(name: str) -> str:
    return " ".join(p[0] + "*" * (len(p) - 1) if i else p[:1] + "*" * (len(p) - 1) for i, p in enumerate(name.split()))


def mask_document(document_id: str) -> str:
    return "*" * (len(document_id) - 4) + document_id[-4:]


def pseudonymize(text: str, name: str, document_id: str) -> str:
    """Replace the known identifiers (we have them structured, no NER needed)."""
    text = text.replace(document_id, DOC)
    for part in sorted([name, *name.split()], key=len, reverse=True):
        text = re.sub(re.escape(part), PERSON, text, flags=re.IGNORECASE)
    return text


def reidentify(text: str, name: str, document_id: str) -> str:
    return text.replace(PERSON, name).replace(DOC, mask_document(document_id))


@lru_cache
def _presidio():
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine

    nlp = NlpEngineProvider(
        nlp_configuration={"nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}]}
    ).create_engine()
    return AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"]), AnonymizerEngine()


def redact_free_text(text: str) -> str:
    """Free text from applicants/analysts may contain arbitrary PII -> Presidio before any LLM call."""
    analyzer, anonymizer = _presidio()
    results = analyzer.analyze(text=text, language="en")
    redacted = anonymizer.anonymize(text=text, analyzer_results=results).text  # type: ignore[arg-type]
    return re.sub(r"\b\d{10}\b", "<ID_NUMBER>", redacted)  # national IDs Presidio doesn't know


_DIGITS = re.compile(r"\b\d{6,}\b")


def mask_log_value(value: str) -> str:
    """structlog processor helper: mask long digit runs (IDs, accounts)."""
    return _DIGITS.sub(lambda m: mask_document(m.group()), value)
