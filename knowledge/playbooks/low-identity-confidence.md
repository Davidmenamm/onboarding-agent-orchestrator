---
title: Playbook - Low Identity Verification Confidence
doc_type: playbook
effective_date: 2026-01-01
---
# Playbook: Low Identity Verification Confidence

## Trigger
Identity verification returns verified = true but confidence below 0.8, or the provider times out.

## Recommended remediation, in order of preference
1. Ask the applicant for a biometric selfie with liveness check. This resolves most cases caused by a poor quality ID photo. Re-run identity verification with the extra evidence.
2. If the confidence is still below 0.8 after the selfie, escalate to an onboarding analyst for manual document review.
3. If the provider timed out repeatedly, retry later; if still unavailable, escalate to an analyst with the note "identity provider unavailable".

## Analyst checklist
Compare the ID photo with the selfie, check document security features, and confirm the civil registry record manually. Record the outcome and reasoning.

## Do not
Do not reject automatically because of low confidence alone. Low confidence is uncertainty, not evidence of fraud.
