---
title: KYC and AML Onboarding Procedure
doc_type: procedure
effective_date: 2026-01-01
---
# KYC and AML Onboarding Procedure

## Purpose
Defines how the bank knows its customer (KYC) and prevents money laundering and terrorist financing (AML) during digital onboarding.

## Identity verification
Every applicant's national ID is verified against the civil registry service. The service returns whether the identity is verified and a confidence score between 0 and 1. A confidence of 0.8 or higher is sufficient for straight-through processing. Below 0.8 the case requires escalation: either additional evidence from the applicant (for example a biometric selfie liveness check) or review by an onboarding analyst.

## Risk screening
Every applicant is screened against sanctions lists (OFAC, UN, EU), politically exposed persons (PEP) lists and internal watchlists. Results are classified as low, medium or high risk. High risk requires escalation to Compliance; no automated approval is permitted. Medium risk requires enhanced due diligence.

## Order of checks
Identity verification and risk screening are independent and may run in parallel. Documentation is prepared only after both results are known, because the risk level determines whether EDD documents are required.

## Record keeping
All verification results, decisions and analyst actions are retained for 10 years in pseudonymous form. Personal data of rejected or abandoned applications is purged after 90 days.
