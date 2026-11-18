"""Step 5: exactly-two (or fewer, flagged) evidence-linked pain-point hypotheses for ONE person.

Evidence pool = sourced signals from qualification + search evidence items. The model may only reference an
evidence_id; the URL/date come from the pool by code. Confidence is capped by evidence quality in code.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from string import Template
from typing import Literal, Optional

from pydantic import BaseModel

from src.common.budget import CompanyBudget
from src.llm.gemini_client import BaseLlm, CallCtx
from src.llm.prompts import PAIN_SYSTEM_V1, PAIN_USER_V1
from src.pain.research import EvidenceItem, structure_search
from src.pain.search_provider import CachedSearch, SearchUnavailable
from src.qualify.scorer import all_trigger_signals
from src.store.models import CompanyFacts, Confidence, Contact, EvidenceQuality, PainPoint, Step


class LlmPain(BaseModel):
    evidence_id: str
    hypothesis: str
    why_it_matters_to_this_role: str
    confidence: Literal["HIGH", "MEDIUM", "LOW"]


class LlmPainOut(BaseModel):
    pain_points: list[LlmPain] = []


@dataclass
class PainOutcome:
    pain_points: list[PainPoint]
    evidence: list[EvidenceItem]
    flags: list[str]
    searches: int = 0


QUALITY_CAP = {EvidenceQuality.STRONG: Confidence.HIGH, EvidenceQuality.MEDIUM: Confidence.MEDIUM,
               EvidenceQuality.WEAK: Confidence.LOW, EvidenceQuality.UNACCEPTABLE: Confidence.LOW}
CONF_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


def evidence_from_facts(facts: CompanyFacts) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    seen = set()
    sigs = all_trigger_signals(facts) + [s for s in facts.enterprise_signals if not s.trigger_type]
    for s in sigs:
        k = (s.source_url, s.signal[:60])
        if k in seen:
            continue
        seen.add(k)
        items.append(EvidenceItem(id=f"E{len(items) + 1}", kind="compliance" if s in facts.compliance_signals
                                  else "signal", statement=s.signal + (f' ("{s.snippet}")' if s.snippet else ""),
                                  source_url=s.source_url, original_url=None, title=None, event_date=s.event_date,
                                  quality=s.evidence_quality, trigger_type=s.trigger_type,
                                  origin=s.source_origin))
    return items


def research_queries(facts: CompanyFacts, contact: Optional[Contact]) -> list[str]:
    q = ["recent news: funding, enterprise customer wins, partnerships, expansion (last 12 months)",
         "security, compliance, SOC 2, ISO 27001, GDPR, trust center or audit announcements"]
    if contact and contact.name:
        q.append(f"public posts or interviews by {contact.name} ({contact.position or ''}) about security, "
                 f"compliance, enterprise sales or hiring")
    return q


def gather_evidence(llm: BaseLlm, search: Optional[CachedSearch], facts: CompanyFacts, contact: Optional[Contact],
                    budget: Optional[CompanyBudget], run_id: Optional[str],
                    prior: Optional[list[EvidenceItem]] = None) -> tuple[list[EvidenceItem], list[str], int]:
    items = evidence_from_facts(facts)
    flags: list[str] = []
    used = 0
    for it in prior or []:
        if all(it.source_url != x.source_url or it.statement[:60] != x.statement[:60] for x in items):
            it.id = f"E{len(items) + 1}"
            items.append(it)
    if search is None:
        flags.append("search_unavailable")
        return items, flags, used
    company = facts.company_name or facts.domain
    ctx = CallCtx(facts.domain, Step.PAIN, budget, run_id)
    for focus in research_queries(facts, contact):
        if search.remaining(facts.domain) <= 0:
            break
        try:
            res = search.search(ctx, facts.domain, company, focus)
            used += 0 if res.cached else 1
        except SearchUnavailable as e:
            flags.append(f"search_unavailable: {str(e)[:120]}")
            break
        new, _rej = structure_search(llm, ctx, res, facts.domain, company, id_prefix="S")
        for it in new:
            if all(it.source_url != x.source_url or it.statement[:60] != x.statement[:60] for x in items):
                it.id = f"E{len(items) + 1}"
                items.append(it)
    return items, flags, used


def _fmt_evidence(items: list[EvidenceItem]) -> str:
    return "\n".join(f"- {i.id} [{i.quality.value}] ({i.event_date or 'undated'}) {i.statement} :: {i.source_url}"
                     for i in items)


def generate_pain_points(llm: BaseLlm, facts: CompanyFacts, contact: Contact, evidence: list[EvidenceItem],
                         budget: Optional[CompanyBudget], run_id: Optional[str]) -> tuple[list[PainPoint], list[str]]:
    flags: list[str] = []
    if not evidence:
        return [], ["no_pain_evidence"]
    user = Template(PAIN_USER_V1).substitute(person_name=contact.name, person_title=contact.position or "unknown title",
                                             company=facts.company_name or facts.domain, domain=facts.domain,
                                             summary=(facts.product_description or "")[:300],
                                             evidence=_fmt_evidence(evidence))
    out: LlmPainOut = llm.generate_json(CallCtx(facts.domain, Step.PAIN, budget, run_id), "pain",
                                        PAIN_SYSTEM_V1, user, LlmPainOut).parsed
    by_id = {e.id: e for e in evidence}
    pains: list[PainPoint] = []
    used_ids: set[str] = set()
    for p in out.pain_points:
        ev = by_id.get(p.evidence_id)
        if ev is None:
            flags.append(f"pain point dropped: unknown evidence_id {p.evidence_id}")
            continue
        if p.evidence_id in used_ids:
            flags.append(f"pain point dropped: evidence {p.evidence_id} reused")
            continue
        conf = Confidence(p.confidence)
        cap = QUALITY_CAP[ev.quality]
        if CONF_RANK[conf] > CONF_RANK[cap]:
            conf = cap
        used_ids.add(p.evidence_id)
        pains.append(PainPoint(signal=ev.statement[:300], source_url=ev.source_url, event_date=ev.event_date,
                               hypothesis=p.hypothesis.strip(), why_it_matters_to_this_role=
                               p.why_it_matters_to_this_role.strip(), confidence=conf))
        if len(pains) == 2:
            break
    if len(pains) < 2:
        flags.append("fewer_than_two_pain_points")
    return pains, flags
