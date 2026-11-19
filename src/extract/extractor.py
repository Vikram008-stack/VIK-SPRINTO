"""Step 2: structured extraction with provenance enforcement.

The model proposes; Python verifies:
- source_url must be one of the URLs we actually sent (otherwise dropped: url_provenance_rejected)
- snippet must actually appear in that source's text (otherwise dropped: snippet_not_found)
- enum values validated; numbers parsed; dates parsed (YYYY-MM-DD / YYYY-MM), else null
- code-level evidence-quality caps (undated news/blog mention -> WEAK; generic/trust-page types -> WEAK)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from string import Template
from typing import Iterable, Optional, get_args

from src.common.logging import get_logger
from src.extract.schema import SCALAR_FIELDS, ExtractionOut, LlmFact
from src.llm.gemini_client import BaseLlm, CallCtx
from src.llm.prompts import EXTRACT_SYSTEM_V1, EXTRACT_USER_V1
from src.store.models import (
    BusinessModel, BuyerType, Chunk, CompanyFacts, EvidenceQuality, FundingStage, HiringSignal, Signal,
)

log = get_logger(__name__)

ENUMS = {
    "business_model": set(get_args(BusinessModel)),
    "buyer_type": set(get_args(BuyerType)),
    "funding_stage": set(get_args(FundingStage)),
    "operating_status": {"operating", "closed", "discontinued", "pre_product", "stealth"},
}
POLICY_CERT_RE = re.compile(r"SOC ?2|ISO[ /-]?(IEC )?27001", re.I)

# ---- trigger validation: the quoted evidence must contain what the trigger type claims (code, not the model)
_FRAMEWORK = r"(SOC ?[12]|ISO[ /-]?(IEC )?27\d{3}|GDPR|HIPAA|PCI|FedRAMP|certif\w*|complian\w*|attestation)"
_SECURITY = r"(secur\w*|complian\w*|SOC ?2|ISO ?27001|GDPR|HIPAA|audit\w*|SSO|SAML|data residency|" \
            r"penetration test|pen ?test|vendor (risk|review|assessment)|questionnaire|due diligence|trust center)"
TRIGGER_RULES: dict[str, tuple[re.Pattern, str]] = {
    "explicit_audit_target": (re.compile(rf"(audit\w*|certif\w*|attestation|{_FRAMEWORK})", re.I),
                              "must name an audit / certification"),
    "stated_deadline": (re.compile(rf"(audit\w*|deadline|by (Q[1-4]|20\d\d|end of)|{_FRAMEWORK})", re.I),
                        "must name a compliance deadline or audit prep"),
    "customer_requires_cert": (re.compile(rf"(requir\w*|mandat\w*|ask\w* for|need\w*|demand\w*).{{0,80}}{_FRAMEWORK}|"
                                          rf"{_FRAMEWORK}.{{0,80}}(requir\w*|mandat\w*)", re.I),
                               "must say a customer requires a certification"),
    "grc_leader_hire": (re.compile(r"(complian\w*|GRC|governance|risk|secur\w*|CISO|trust|privacy)", re.I),
                        "must be a compliance / GRC / security role"),
    "procurement_questionnaire": (re.compile(r"(questionnaire|procurement|vendor (risk|review|assessment)|"
                                             r"due diligence|security review)", re.I),
                                  "must mention procurement or security questionnaires"),
    "enterprise_expansion": (re.compile(_SECURITY, re.I),
                             "enterprise expansion counts only with an identifiable security requirement"),
    "trust_page_no_need": (re.compile(_SECURITY, re.I), "must mention security / trust"),
    "generic_mention": (re.compile(_SECURITY, re.I), "must mention security / compliance"),
}
STRONG_TYPES = {"explicit_audit_target", "customer_requires_cert", "stated_deadline"}
NAMED_CUSTOMER_RE = re.compile(r"\b(customers? (like|such as|including)|trusted by|used by|[A-Z][a-z]+ (Inc|Ltd|Corp)\b)")


def trigger_supported(trigger_type: Optional[str], signal: str, snippet: str) -> tuple[bool, str]:
    """Does the QUOTED evidence (not the model's paraphrase) support the proposed trigger type?"""
    if not trigger_type:
        return True, ""
    rule = TRIGGER_RULES.get(trigger_type)
    if rule is None:
        return False, f"unknown trigger type {trigger_type}"
    pattern, need = rule
    if pattern.search(snippet or ""):
        return True, ""
    return False, f"{need}; the quoted text does not ({(snippet or '')[:80]!r})"


@dataclass
class ExtractionResult:
    facts: CompanyFacts
    dropped: list[str] = field(default_factory=list)     # human-readable reasons for rejected model output
    model: str = ""
    prompt_version: str = ""
    tokens: int = 0
    cost_usd: Optional[float] = None
    cached: bool = False


# ------------------------------------------------------------------ verification helpers
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def snippet_supported(snippet: str, source_text: str) -> bool:
    """Verbatim (normalised) containment, or >= 80% of the snippet's words present in order-insensitive form."""
    sn, tx = _norm(snippet), _norm(source_text)
    if not sn:
        return False
    if sn in tx:
        return True
    words = [w for w in sn.split() if len(w) > 2]
    if not words:
        return False
    tx_words = set(tx.split())
    return sum(w in tx_words for w in words) / len(words) >= 0.8


def parse_event_date(v: Optional[str]) -> Optional[date]:
    if not v:
        return None
    v = v.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v)
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"^(\d{4})-(\d{2})$", v)
        if m:   # month precision -> first of month (conservative: never makes a signal look newer)
            return date(int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        return None
    return None   # year-only or free text -> undated


def _parse_int(v: str) -> Optional[int]:
    m = re.search(r"\d[\d,\.]*\s*[kK]?", v or "")
    if not m:
        return None
    s = m.group(0).replace(",", "").strip()
    try:
        return int(float(s[:-1]) * 1000) if s.lower().endswith("k") else int(float(s))
    except ValueError:
        return None


def _norm_url(u: str) -> str:
    return (u or "").strip().rstrip("/")


# ------------------------------------------------------------------ prompt
def build_sources(chunks: Iterable[Chunk]) -> tuple[str, dict[str, str], dict[str, Chunk]]:
    by_url: dict[str, list[str]] = {}
    first: dict[str, Chunk] = {}
    for c in chunks:
        by_url.setdefault(c.source_url, []).append(c.text)
        first.setdefault(c.source_url, c)
    blocks, texts = [], {}
    for url, parts in by_url.items():
        c = first[url]
        txt = "\n".join(parts)
        texts[_norm_url(url)] = txt
        blocks.append(f"=== SOURCE {url} (page: {c.page_type or 'page'}, fetched: {c.fetched_at.date()}) ===\n{txt}")
    return "\n\n".join(blocks), texts, {(_norm_url(u)): c for u, c in first.items()}


# ------------------------------------------------------------------ main
def extract(llm: BaseLlm, ctx: CallCtx, domain: str, chunks: list[Chunk], today: date,
            focus_fields: Optional[list[str]] = None,
            footer_compliance: Optional[dict[str, list[str]]] = None) -> ExtractionResult:
    sources_text, texts, first_chunk = build_sources(chunks)
    focus = ""
    if focus_fields:
        focus = ("FOCUS: this is a targeted follow-up. Concentrate on these fields/questions: "
                 + ", ".join(focus_fields) + ". Still follow every rule.")
    user = Template(EXTRACT_USER_V1).substitute(domain=domain, today=today.isoformat(), focus=focus,
                                                sources=sources_text)
    res = llm.generate_json(ctx, "extract", EXTRACT_SYSTEM_V1, user, ExtractionOut)
    out: ExtractionOut = res.parsed
    facts, dropped = verify(out, domain, texts, first_chunk)

    # deterministic, code-derived certification evidence (footer/trust badges seen by the cleaner)
    for url, badges in (footer_compliance or {}).items():
        for b in badges:
            if POLICY_CERT_RE.search(b) or re.search(r"HIPAA|PCI|27701|42001", b, re.I):
                if b.lower() not in [c.lower() for c in facts.certifications_claimed]:
                    facts.certifications_claimed.append(b)
                    facts.field_sources.setdefault(f"cert:{b}", url)

    return ExtractionResult(facts=facts, dropped=dropped, model=res.model, prompt_version=res.prompt_version,
                            tokens=res.input_tokens + res.output_tokens, cost_usd=res.cost_usd, cached=res.cached)


def verify(out: ExtractionOut, domain: str, texts: dict[str, str],
           first_chunk: dict[str, Chunk]) -> tuple[CompanyFacts, list[str]]:
    dropped: list[str] = []
    facts = CompanyFacts(domain=domain)

    def check(fact: Optional[LlmFact], name: str) -> Optional[LlmFact]:
        if fact is None or not (fact.value or "").strip():
            return None
        url = _norm_url(fact.source_url)
        if url not in texts:
            dropped.append(f"{name}: url_provenance_rejected ({fact.source_url})")
            return None
        if not snippet_supported(fact.snippet, texts[url]):
            dropped.append(f"{name}: snippet_not_found ({fact.snippet[:60]!r})")
            return None
        return fact

    for name in SCALAR_FIELDS:
        f = check(getattr(out, name), name)
        if f is None:
            continue
        val: object = f.value.strip()
        if name in ENUMS:
            v = str(val).lower().replace(" ", "_").replace("-", "_")
            if v not in ENUMS[name]:
                dropped.append(f"{name}: invalid enum {val!r}")
                continue
            val = v
        elif name in ("employee_count_estimate", "open_roles_count"):
            val = _parse_int(str(val))
            if val is None:
                dropped.append(f"{name}: not a number")
                continue
        elif name == "has_enterprise_pricing_tier":
            val = str(val).lower() in ("true", "yes", "1")
        setattr(facts, name, val)
        facts.field_sources[name] = first_chunk[_norm_url(f.source_url)].source_url
        facts.field_snippets[name] = " ".join((f.snippet or "").split()[:25])

    for cf in out.certifications_claimed:
        f = check(cf, "certification")
        if f and f.value.strip() not in facts.certifications_claimed:
            facts.certifications_claimed.append(f.value.strip())
            facts.field_sources[f"cert:{f.value.strip()}"] = first_chunk[_norm_url(f.source_url)].source_url

    seen_events: set[tuple] = set()
    for s in out.signals:
        url = _norm_url(s.source_url)
        if url not in texts:
            dropped.append(f"signal: url_provenance_rejected ({s.source_url})")
            continue
        if not snippet_supported(s.snippet, texts[url]):
            dropped.append(f"signal: snippet_not_found ({s.snippet[:60]!r})")
            continue
        chunk = first_chunk[url]
        ev = parse_event_date(s.event_date)
        q = EvidenceQuality(s.evidence_quality)
        trigger_type = s.trigger_type
        # code checks that the QUOTED text supports the trigger type the model proposed
        ok, why = trigger_supported(trigger_type, s.signal, s.snippet)
        if not ok:
            facts.rejected_triggers.append({"signal": s.signal[:200], "quote": " ".join(s.snippet.split()[:25]),
                                            "proposed_type": trigger_type, "source_url": chunk.source_url,
                                            "why": why})
            dropped.append(f"trigger demoted ({trigger_type}): {why}")
            trigger_type = None
            q = EvidenceQuality.WEAK
        # code-level caps on model-proposed quality (rubric: STRONG only for explicit audit / requirement evidence)
        if trigger_type in ("generic_mention", "trust_page_no_need"):
            q = EvidenceQuality.WEAK
        if q == EvidenceQuality.STRONG and trigger_type not in STRONG_TYPES and s.kind == "compliance":
            q = EvidenceQuality.MEDIUM
        if q == EvidenceQuality.STRONG and s.kind == "enterprise" and trigger_type not in STRONG_TYPES \
                and not NAMED_CUSTOMER_RE.search(s.snippet or ""):
            q = EvidenceQuality.MEDIUM
        if ev is None and chunk.page_type in ("news", "blog") and q == EvidenceQuality.STRONG:
            q = EvidenceQuality.WEAK
        event_key = (trigger_type, ev, _norm(s.signal)[:60])
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        snippet = " ".join(s.snippet.split()[:25])
        sig = Signal(signal=s.signal, source_url=chunk.source_url, snippet=snippet, event_date=ev,
                     observed_at=chunk.fetched_at, evidence_quality=q, trigger_type=trigger_type,
                     source_origin="fetched_page")
        (facts.compliance_signals if s.kind == "compliance" else facts.enterprise_signals).append(sig)

    for h in out.hiring:
        url = _norm_url(h.source_url)
        if url not in texts or not snippet_supported(h.title, texts[url]):
            dropped.append(f"hiring: unsupported title {h.title!r}")
            continue
        facts.hiring_signals.append(HiringSignal(title=h.title.strip(), source_url=first_chunk[url].source_url,
                                                 event_date=parse_event_date(h.event_date)))

    if facts.open_roles_count is None and facts.hiring_signals:
        facts.open_roles_count = len(facts.hiring_signals)       # counted by code from verified titles
        facts.field_sources["open_roles_count"] = facts.hiring_signals[0].source_url

    facts.missing_fields = [n for n in SCALAR_FIELDS if getattr(facts, n) is None]
    facts.extraction_notes = (out.extraction_notes or "")[:500]
    if dropped:
        log.info("extraction items dropped", extra={"domain": domain, "count": len(dropped)})
    return facts, dropped


# ------------------------------------------------------------------ merge (agent loop)
def merge_facts(base: CompanyFacts, new: CompanyFacts, focus_fields: Optional[list[str]] = None) -> CompanyFacts:
    """Merge a follow-up extraction into the running facts, keeping provenance and recording conflicts."""
    merged = base.model_copy(deep=True)
    for name in SCALAR_FIELDS:
        nv = getattr(new, name)
        if nv is None:
            continue
        ov = getattr(merged, name)
        if ov is None:
            setattr(merged, name, nv)
            if name in new.field_sources:
                merged.field_sources[name] = new.field_sources[name]
        elif ov != nv and name in ("funding_stage", "business_model", "buyer_type", "operating_status",
                                   "acquired_by"):
            note = (f"{name}: {ov!r} ({merged.field_sources.get(name, '?')}) vs {nv!r} "
                    f"({new.field_sources.get(name, '?')})")
            if note not in merged.conflicts:
                merged.conflicts.append(note)
        elif ov != nv and name in ("open_roles_count", "employee_count_estimate") and isinstance(nv, int):
            if nv > (ov or 0):   # later, more specific page (e.g. careers) wins for counts
                setattr(merged, name, nv)
                if name in new.field_sources:
                    merged.field_sources[name] = new.field_sources[name]
    for c in new.certifications_claimed:
        if c.lower() not in [x.lower() for x in merged.certifications_claimed]:
            merged.certifications_claimed.append(c)
    for k, v in new.field_sources.items():
        if k.startswith("cert:"):
            merged.field_sources.setdefault(k, v)

    def _merge_sigs(old: list[Signal], add: list[Signal]) -> list[Signal]:
        keys = {(s.source_url, _norm(s.signal)[:60]) for s in old}
        out = list(old)
        for s in add:
            k = (s.source_url, _norm(s.signal)[:60])
            if k not in keys:
                out.append(s)
                keys.add(k)
        return out

    merged.compliance_signals = _merge_sigs(merged.compliance_signals, new.compliance_signals)
    merged.enterprise_signals = _merge_sigs(merged.enterprise_signals, new.enterprise_signals)
    hk = {(h.source_url, h.title.lower()) for h in merged.hiring_signals}
    for h in new.hiring_signals:
        if (h.source_url, h.title.lower()) not in hk:
            merged.hiring_signals.append(h)
    merged.conflicts = list(dict.fromkeys(merged.conflicts + new.conflicts))
    for k, v in new.field_snippets.items():
        merged.field_snippets.setdefault(k, v)
    seen_rej = {(r["source_url"], r["signal"]) for r in merged.rejected_triggers}
    merged.rejected_triggers += [r for r in new.rejected_triggers if (r["source_url"], r["signal"]) not in seen_rej]
    merged.missing_fields = [n for n in SCALAR_FIELDS if getattr(merged, n) is None]
    if new.extraction_notes:
        merged.extraction_notes = (merged.extraction_notes + " | " + new.extraction_notes).strip(" |")[:1000]
    return merged
