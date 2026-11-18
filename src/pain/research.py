"""Turn search findings into evidence items with verified provenance (call 2 of the two-call pattern).

The structuring model may only point at a numbered source (source_index). The URL is looked up by code from the
grounding metadata, so a URL invented in prose can never become evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from string import Template
from typing import Literal, Optional

from pydantic import BaseModel

from src.extract.extractor import parse_event_date
from src.llm.gemini_client import BaseLlm, CallCtx
from src.llm.prompts import RESEARCH_STRUCT_SYSTEM_V1, RESEARCH_STRUCT_USER_V1
from src.pain.search_provider import SearchResult
from src.store.models import EvidenceQuality, Signal


class LlmEvidenceItem(BaseModel):
    kind: Literal["compliance", "enterprise", "funding", "hiring", "product", "leadership", "other"]
    statement: str
    source_index: int
    event_date: Optional[str] = None
    evidence_quality: Literal["STRONG", "MEDIUM", "WEAK"]
    trigger_type: Optional[Literal[
        "explicit_audit_target", "customer_requires_cert", "stated_deadline", "grc_leader_hire",
        "procurement_questionnaire", "enterprise_expansion", "trust_page_no_need", "generic_mention"]] = None
    funding_stage: Optional[Literal["pre_seed", "seed", "series_a", "series_b", "series_c", "series_d_plus",
                                    "bootstrapped", "public", "acquired"]] = None


class LlmEvidenceOut(BaseModel):
    items: list[LlmEvidenceItem] = []


@dataclass
class EvidenceItem:
    id: str
    kind: str
    statement: str
    source_url: str
    original_url: Optional[str]
    title: Optional[str]
    event_date: Optional[date]
    quality: EvidenceQuality
    trigger_type: Optional[str] = None
    funding_stage: Optional[str] = None
    origin: str = "grounding"

    def to_signal(self, observed_at) -> Signal:
        return Signal(signal=self.statement[:300], source_url=self.source_url, snippet=None,
                      event_date=self.event_date, observed_at=observed_at, evidence_quality=self.quality,
                      trigger_type=self.trigger_type, source_origin="grounding")


def structure_search(llm: BaseLlm, ctx: CallCtx, result: SearchResult, domain: str, company: str,
                     id_prefix: str = "S") -> tuple[list[EvidenceItem], list[str]]:
    """Returns (evidence items, rejection notes)."""
    if not result.citations:
        return [], ["search returned no citations: nothing usable"]
    srcs = "\n".join(f"[{c.index}] {c.title or ''} :: {c.resolved_uri or c.uri}" for c in result.citations)
    user = Template(RESEARCH_STRUCT_USER_V1).substitute(company=company, domain=domain, text=result.text[:6000],
                                                        sources=srcs)
    out: LlmEvidenceOut = llm.generate_json(ctx, "pain", RESEARCH_STRUCT_SYSTEM_V1, user, LlmEvidenceOut).parsed
    by_index = {c.index: c for c in result.citations}
    items, notes = [], []
    for n, it in enumerate(out.items):
        c = by_index.get(it.source_index)
        if c is None:
            notes.append(f"dropped (no such source index {it.source_index}): {it.statement[:60]}")
            continue
        url = c.resolved_uri or c.uri
        if not url.startswith("http"):
            notes.append(f"dropped (bad url): {it.statement[:60]}")
            continue
        q = EvidenceQuality(it.evidence_quality)
        ev = parse_event_date(it.event_date)
        if ev is None and q == EvidenceQuality.STRONG:
            q = EvidenceQuality.MEDIUM          # undated third-party claims are never STRONG
        items.append(EvidenceItem(id=f"{id_prefix}{n + 1}", kind=it.kind, statement=it.statement, source_url=url,
                                  original_url=c.uri if c.resolved_uri else None, title=c.title, event_date=ev,
                                  quality=q, trigger_type=it.trigger_type, funding_stage=it.funding_stage))
    return items, notes
