"""Step 6: email draft (structured), checked by guardrails, regenerated once with the violations.

Evidence is referenced by evidence_id; code maps ids to URLs, so a draft cannot cite a source we did not collect.
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field
from string import Template
from typing import Optional

from pydantic import BaseModel

from src.common.budget import CompanyBudget
from src.common.config import EmailCfg
from src.llm.gemini_client import BaseLlm, CallCtx
from src.llm.prompts import DRAFT_SYSTEM_V1, DRAFT_USER_V1
from src.mail.guardrails import check_draft, word_count
from src.pain.research import EvidenceItem
from src.store.models import CompanyFacts, Contact, EmailDraft, EvidenceClaim, PainPoint, Step


SIGNOFF_RE = re.compile(r"\s*\b(Best regards|Kind regards|Best|Thanks|Thank you|Regards|Cheers),\s*", re.I)
NL = "\n"


def format_body(body: str) -> str:
    """Models sometimes return one long line. Put greeting, paragraphs and sign-off on their own lines."""
    b = (body or "").strip()
    if NL in b:
        return b
    m = re.match(r"^((Hi|Hello|Hey|Dear)\s+[^,\n]{1,40},)\s*", b, re.I)
    greet, rest = (m.group(1), b[m.end():]) if m else ("", b)
    sign = ""
    sm = SIGNOFF_RE.search(rest)
    if sm:
        sign = rest[sm.start():].strip()
        rest = rest[:sm.start()].strip()
        sign = re.sub(r",\s*", "," + NL, sign, count=1)
    sentences = re.split(r"(?<=[.?!])\s+", rest)
    paras, cur = [], []
    for sent in sentences:
        cur.append(sent)
        if len(cur) == 2 or sent.endswith("?"):
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    parts = ([greet] if greet else []) + paras + ([sign] if sign else [])
    return (NL + NL).join(p for p in parts if p)


class LlmClaim(BaseModel):
    claim: str
    evidence_id: str


class LlmDraft(BaseModel):
    subject: str
    recipient_name: str
    opening_hook: str
    hook_evidence_id: str
    pain_point_1: str
    pain_point_2: Optional[str] = None
    email_body: str
    personalisation_evidence: list[LlmClaim] = []
    cta: str


@dataclass
class DraftOutcome:
    draft: Optional[EmailDraft]
    ok: bool
    attempts: list[dict] = field(default_factory=list)    # [{"draft": EmailDraft, "errors": [...]}]
    hook_url: Optional[str] = None


def _to_draft(raw: LlmDraft, by_id: dict[str, EvidenceItem]) -> tuple[EmailDraft, Optional[str], list[str]]:
    errs: list[str] = []
    claims: list[EvidenceClaim] = []
    for c in raw.personalisation_evidence:
        ev = by_id.get(c.evidence_id)
        if ev is None:
            errs.append(f"claim references unknown evidence_id {c.evidence_id}")
            continue
        claims.append(EvidenceClaim(claim=c.claim, source_url=ev.source_url))
    hook = by_id.get(raw.hook_evidence_id)
    if hook is None:
        errs.append(f"hook_evidence_id {raw.hook_evidence_id} is not a collected evidence item")
    elif all(c.source_url != hook.source_url for c in claims):
        claims.insert(0, EvidenceClaim(claim=raw.opening_hook[:200], source_url=hook.source_url))
    d = EmailDraft(subject=raw.subject.strip(), recipient_name=raw.recipient_name.strip(),
                   opening_hook=raw.opening_hook.strip(), pain_point_1=raw.pain_point_1.strip(),
                   pain_point_2=(raw.pain_point_2 or None), email_body=format_body(raw.email_body),
                   personalisation_evidence=claims, cta=raw.cta.strip())
    return d, (hook.source_url if hook else None), errs


def draft_email(llm: BaseLlm, cfg: EmailCfg, facts: CompanyFacts, contact: Contact, why_qualified: str,
                evidence: list[EvidenceItem], pains: list[PainPoint], budget: Optional[CompanyBudget],
                run_id: Optional[str]) -> DraftOutcome:
    by_id = {e.id: e for e in evidence}
    allowed = {e.source_url for e in evidence}
    first = contact.first_name or contact.name.split()[0]
    pains_txt = "\n".join(f"- ({p.confidence.value}) {p.hypothesis} | why it matters: {p.why_it_matters_to_this_role}"
                          f" | signal: {p.signal[:160]} ({p.event_date or 'undated'})" for p in pains) or "- none"
    ev_txt = "\n".join(f"- {e.id} [{e.quality.value}] ({e.event_date or 'undated'}) {e.statement[:300]}"
                       for e in evidence)
    system = Template(DRAFT_SYSTEM_V1).substitute(max_words=cfg.max_words, sender=cfg.sender_name or "the Sprinto team")
    outcome = DraftOutcome(draft=None, ok=False)
    feedback = ""
    for attempt in range(cfg.guardrail_max_attempts):
        user = Template(DRAFT_USER_V1).substitute(
            first_name=first, full_name=contact.name, title=contact.position or "", company=facts.company_name
            or facts.domain, domain=facts.domain, why=why_qualified[:400], evidence=ev_txt, pains=pains_txt,
            feedback=feedback)
        raw: LlmDraft = llm.generate_json(CallCtx(facts.domain, Step.DRAFT, budget, run_id), "draft", system, user,
                                          LlmDraft).parsed
        d, hook_url, map_errs = _to_draft(raw, by_id)
        errs = map_errs + check_draft(d, contact, allowed, hook_url, cfg.max_words, cfg.word_tolerance,
                                      cfg.max_em_dashes)
        outcome.attempts.append({"attempt": attempt + 1, "draft": d, "errors": errs, "words": word_count(d.email_body)})
        outcome.draft, outcome.hook_url = d, hook_url
        if not errs:
            outcome.ok = True
            return outcome
        feedback = ("\nYOUR PREVIOUS DRAFT VIOLATED THESE RULES. Fix every one:\n- " + "\n- ".join(errs))
    return outcome
