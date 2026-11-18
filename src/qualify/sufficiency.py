"""Evidence-Map sufficiency checks. PURE PYTHON. The model is never asked whether it has enough.

Each check maps to a `sufficient_when` key in icp_rubric.yaml. Returns which rows are unmet.
"""
from __future__ import annotations

from typing import Callable

from src.common.config import RubricCfg
from src.qualify.scorer import already_certified, score_trigger
from src.store.models import CompanyFacts


def cert_named_or_security_page_seen(f: CompanyFacts, rubric: RubricCfg, fetched: set[str],
                                     discovered: dict[str, list[str]]) -> bool:
    # Settled if a certification is named, a security/trust page was read, or the homepage does not link one
    # (Evidence Map: "homepage footer, /security, /trust"; unanswered -> treat as not certified, flag).
    return already_certified(f) or bool(f.certifications_claimed) or "security" in fetched \
        or not discovered.get("security")


def buyer_and_model_known(f: CompanyFacts, *_a) -> bool:
    return f.business_model is not None and f.buyer_type is not None


def funding_or_roles_or_enterprise_tier(f: CompanyFacts, *_a) -> bool:
    return f.funding_stage is not None or (f.open_roles_count or 0) >= 5 or bool(f.has_enterprise_pricing_tier)


def dated_sourced_trigger_ge_20(f: CompanyFacts, rubric: RubricCfg, *_a) -> bool:
    t = score_trigger(rubric, f)
    if t.signal is None:
        return False
    if t.points >= 40:            # short-circuit: a strong 40-pt signal ends research early
        return True
    return t.points >= 20 and t.signal.event_date is not None


CHECKS: dict[str, Callable[..., bool]] = {
    "cert_named_or_security_page_seen": cert_named_or_security_page_seen,
    "buyer_and_model_known": buyer_and_model_known,
    "funding_or_roles_or_enterprise_tier": funding_or_roles_or_enterprise_tier,
    "dated_sourced_trigger_ge_20": dated_sourced_trigger_ge_20,
}

# Which extraction fields each row is about (for targeted re-extraction)
ROW_FIELDS: dict[str, list[str]] = {
    "already_certified": ["certifications_claimed", "compliance signals (trust/security page)"],
    "b2b_saas": ["business_model", "buyer_type", "product_description"],
    "stage": ["funding_stage", "open_roles_count", "has_enterprise_pricing_tier", "hiring"],
    "trigger": ["compliance signals with trigger_type and event_date", "hiring", "enterprise signals"],
}


def unmet(rubric: RubricCfg, facts: CompanyFacts, fetched_types: set[str],
          discovered: dict[str, list[str]]) -> list[str]:
    out = []
    for row in rubric.evidence_map:
        check = CHECKS[row.sufficient_when]
        if not check(facts, rubric, fetched_types, discovered):
            out.append(row.id)
    return out
