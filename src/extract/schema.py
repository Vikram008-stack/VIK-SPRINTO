"""LLM response schemas for extraction (what Gemini must return).

Kept deliberately simple (strings, optional nested objects, lists) so Gemini's response_schema accepts them.
Enum values and URLs are validated in Python afterwards (src/extract/extractor.py), never trusted.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class LlmFact(BaseModel):
    value: str
    source_url: str
    snippet: str


class LlmSignal(BaseModel):
    kind: Literal["compliance", "enterprise"]
    signal: str
    snippet: str
    source_url: str
    event_date: Optional[str] = None
    evidence_quality: Literal["STRONG", "MEDIUM", "WEAK"]
    trigger_type: Optional[Literal[
        "explicit_audit_target", "customer_requires_cert", "stated_deadline", "grc_leader_hire",
        "procurement_questionnaire", "enterprise_expansion", "trust_page_no_need", "generic_mention"]] = None


class LlmHiring(BaseModel):
    title: str
    source_url: str
    event_date: Optional[str] = None


class ExtractionOut(BaseModel):
    company_name: Optional[LlmFact] = None
    product_description: Optional[LlmFact] = None
    business_model: Optional[LlmFact] = None
    buyer_type: Optional[LlmFact] = None
    funding_stage: Optional[LlmFact] = None
    employee_count_estimate: Optional[LlmFact] = None
    open_roles_count: Optional[LlmFact] = None
    geography: Optional[LlmFact] = None
    has_enterprise_pricing_tier: Optional[LlmFact] = None
    operating_status: Optional[LlmFact] = None
    acquired_by: Optional[LlmFact] = None
    parent_company: Optional[LlmFact] = None
    certifications_claimed: list[LlmFact] = []
    signals: list[LlmSignal] = []
    hiring: list[LlmHiring] = []
    missing_fields: list[str] = []
    extraction_notes: str = ""


SCALAR_FIELDS = ["company_name", "product_description", "business_model", "buyer_type", "funding_stage",
                 "employee_count_estimate", "open_roles_count", "geography", "has_enterprise_pricing_tier",
                 "operating_status", "acquired_by", "parent_company"]
