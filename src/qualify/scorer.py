"""Step 3 scoring + decision. Pure Python, fixed rules from icp_rubric.yaml. The model never decides.

score_components(): facts -> five ComponentScores, each linked to source URLs.
decide():           score + gates + hard disqualifiers + context flags -> QualStatus with reasons.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from src.common.config import RubricCfg
from src.store.models import (
    CompanyFacts, ComponentScore, Confidence, EvidenceQuality, QualStatus, ScoreBreakdown, Signal,
)

LEADER_HIRE_RE = re.compile(
    r"\b(head of (security|compliance|grc|information security|trust|risk)|ciso|chief (information )?security"
    r" officer|(vp|vice president|director)[ ,of]*(security|compliance|grc|information security)|"
    r"compliance (manager|lead|officer)|grc (manager|lead)|security (lead|manager)|"
    r"(governance|risk).{0,20}compliance (manager|lead))\b", re.I)
SECURITY_IC_RE = re.compile(r"\b(security|appsec|devsecops|soc analyst|security engineer)\b", re.I)
POLICY_CERT_RE = re.compile(r"SOC ?2|ISO[ /-]?(IEC )?27001", re.I)
FRAMEWORK_RE = re.compile(r"SOC ?2|ISO[ /-]?(IEC )?27001|ISO[ /-]?27701|ISO[ /-]?42001|GDPR|HIPAA|PCI|FedRAMP", re.I)


@dataclass
class QualContext:
    """Non-fact context that affects the decision (from scraping and the loop)."""
    today: date
    flags: list[str] = field(default_factory=list)       # homepage_unusable, domain_mismatch, parked_domain, ...
    research_complete: bool = False                      # loop finished or nothing left to fetch
    already_certified_policy: str = "research"
    budget_exceeded: bool = False
    duplicate: bool = False


@dataclass
class ScoredTrigger:
    points: int
    bucket: str
    signal: Optional[Signal]
    reason: str


@dataclass
class Decision:
    status: QualStatus
    score: int
    breakdown: ScoreBreakdown
    trigger: ScoredTrigger
    hard_disqualifier: Optional[str]
    reasons: list[str]
    unknowns: list[str]
    confidence: Confidence
    handoff_note: Optional[str] = None
    max_possible: int = 100


# ------------------------------------------------------------------ helpers
def _fw(text: str) -> set[str]:
    return {re.sub(r"[^a-z0-9]", "", m.group(0).lower()) for m in FRAMEWORK_RE.finditer(text or "")}


def already_certified(facts: CompanyFacts) -> bool:
    return any(POLICY_CERT_RE.search(c) for c in facts.certifications_claimed)


def code_trigger_signals(facts: CompanyFacts) -> list[Signal]:
    """Derive trigger signals from verified job titles (code, not the model)."""
    out: list[Signal] = []
    for h in facts.hiring_signals:
        if LEADER_HIRE_RE.search(h.title):
            out.append(Signal(signal=f"Hiring: {h.title}", source_url=h.source_url, snippet=h.title[:200],
                              event_date=h.event_date, evidence_quality=EvidenceQuality.MEDIUM,
                              trigger_type="grc_leader_hire", source_origin="fetched_page"))
        elif SECURITY_IC_RE.search(h.title):
            out.append(Signal(signal=f"Hiring (individual contributor): {h.title}", source_url=h.source_url,
                              snippet=h.title[:200], event_date=h.event_date, evidence_quality=EvidenceQuality.WEAK,
                              trigger_type="trust_page_no_need", source_origin="fetched_page"))
    return out


def all_trigger_signals(facts: CompanyFacts) -> list[Signal]:
    sigs = [s for s in facts.compliance_signals + facts.enterprise_signals if s.trigger_type]
    sigs += code_trigger_signals(facts)
    return [s for s in sigs if s.source_url and s.evidence_quality != EvidenceQuality.UNACCEPTABLE]


def recency_points(rubric: RubricCfg, ev: Optional[date], today: date) -> tuple[int, str]:
    r = rubric.components["recency"]
    if ev is None:
        return int(r["undated"]), "undated"
    days = (today - ev).days
    if days < 0:
        return int(r["bands_days"][0][1]), f"upcoming ({ev.isoformat()})"
    for limit, pts in r["bands_days"]:
        if days <= limit:
            return int(pts), f"{days} days ago"
    return int(r["older"]), f"{days} days ago"


# ------------------------------------------------------------------ components
def score_trigger(rubric: RubricCfg, facts: CompanyFacts) -> ScoredTrigger:
    buckets = rubric.components["compliance_trigger"]["buckets"]
    best: Optional[ScoredTrigger] = None
    for s in all_trigger_signals(facts):
        pts = int(buckets.get(s.trigger_type, 0))
        cand = ScoredTrigger(pts, s.trigger_type, s, f"{s.signal} [{s.evidence_quality.value}]")
        if best is None or (pts, s.evidence_quality.rank, s.event_date or date.min) > \
                (best.points, best.signal.evidence_quality.rank, best.signal.event_date or date.min):
            best = cand
    if best is None and facts.certifications_claimed:
        url = next((v for k, v in facts.field_sources.items() if k.startswith("cert:")), None)
        if url:
            sig = Signal(signal="Existing certification / trust claims: " + ", ".join(facts.certifications_claimed),
                         source_url=url, evidence_quality=EvidenceQuality.WEAK, trigger_type="trust_page_no_need")
            best = ScoredTrigger(int(buckets["trust_page_no_need"]), "trust_page_no_need", sig, sig.signal)
    return best or ScoredTrigger(0, "none", None, "no sourced compliance trigger found")


def score_components(rubric: RubricCfg, facts: CompanyFacts, today: date) -> tuple[ScoreBreakdown, ScoredTrigger]:
    comps: list[ComponentScore] = []
    src = facts.field_sources

    # A. SaaS fit
    sb = rubric.components["saas_fit"]["buckets"]
    bm = facts.business_model
    if bm is None:
        a = ComponentScore(component="saas_fit", points=0, max_points=25, bucket="unknown",
                           reason="business model not established from sources")
    else:
        pts = int(sb.get(bm, 0))
        reason = f"business_model={bm}"
        if bm == "cloud_subscription" and facts.operating_status == "pre_product":
            pts = min(pts, 15)
            reason += " (no working product yet: capped)"
        a = ComponentScore(component="saas_fit", points=pts, max_points=25, bucket=bm, reason=reason,
                           source_urls=[src["business_model"]] if "business_model" in src else [])
    comps.append(a)

    # B. Compliance trigger (strongest verified only)
    trig = score_trigger(rubric, facts)
    comps.append(ComponentScore(component="compliance_trigger", points=trig.points, max_points=40,
                                bucket=trig.bucket, reason=trig.reason,
                                source_urls=[trig.signal.source_url] if trig.signal else []))

    # C. Stage
    stb = rubric.components["stage"]["buckets"]
    fs = facts.funding_stage
    traction = bool(facts.enterprise_signals or (facts.open_roles_count or 0) > 0
                    or facts.has_enterprise_pricing_tier or facts.buyer_type in ("business", "both"))
    if fs in ("series_a", "series_b", "series_c"):
        bucket = "series_a_c"
    elif fs == "seed":
        bucket = "seed_traction" if traction else "unknown"
    elif fs == "bootstrapped":
        bucket = "bootstrapped_traction" if traction else "unknown"
    elif fs in ("series_d_plus", "public"):
        bucket = "series_d_plus"
    elif fs == "pre_seed":
        bucket = "pre_seed_launched"
    elif facts.operating_status == "pre_product":
        bucket = "pre_product"
    else:
        bucket = "unknown"
    comps.append(ComponentScore(component="stage", points=int(stb[bucket]), max_points=15, bucket=bucket,
                                reason=f"funding_stage={fs or 'unknown'}",
                                source_urls=[src["funding_stage"]] if "funding_stage" in src else []))

    # D. Enterprise exposure
    eb = rubric.components["enterprise"]["buckets"]
    explicit = [s for s in facts.enterprise_signals
                if s.evidence_quality in (EvidenceQuality.STRONG, EvidenceQuality.MEDIUM)]
    if explicit:
        e_bucket, e_urls = "explicit_enterprise", [explicit[0].source_url]
    elif facts.buyer_type in ("business", "both") and (facts.has_enterprise_pricing_tier or facts.enterprise_signals):
        e_bucket, e_urls = "b2b_midmarket", [src.get("has_enterprise_pricing_tier") or src.get("buyer_type")]
    elif facts.buyer_type in ("business", "both"):
        e_bucket, e_urls = "b2b_unclear", [src.get("buyer_type")]
    elif facts.buyer_type == "consumer":
        e_bucket, e_urls = "b2c", [src.get("buyer_type")]
    else:
        e_bucket, e_urls = "none", []
    comps.append(ComponentScore(component="enterprise", points=int(eb[e_bucket]), max_points=10, bucket=e_bucket,
                                reason=f"buyer_type={facts.buyer_type or 'unknown'}",
                                source_urls=[u for u in e_urls if u]))

    # E. Recency of the scored trigger
    if trig.signal is not None and trig.points > 0:
        rp, rreason = recency_points(rubric, trig.signal.event_date, today)
        rurls = [trig.signal.source_url]
    else:
        rp, rreason, rurls = 0, "no trigger to date", []
    comps.append(ComponentScore(component="recency", points=rp, max_points=10,
                                bucket="dated" if rp else "undated", reason=rreason, source_urls=rurls))

    # Rule: no point without a source URL (except 'unknown' stage, which is a default not a claim)
    for c in comps:
        if c.points and not c.source_urls and not (c.component == "stage" and c.bucket == "unknown"):
            c.reason += " | points removed: no source_url"
            c.points = 0
    return ScoreBreakdown(components=comps), trig


# ------------------------------------------------------------------ decision
def hard_disqualifier(facts: CompanyFacts, ctx: QualContext) -> Optional[str]:
    if ctx.duplicate:
        return "duplicate"
    if "parked_domain" in ctx.flags:
        return "not_operating"
    if facts.operating_status in ("closed", "discontinued"):
        return "closed_or_discontinued"
    if facts.business_model in ("it_services", "non_software"):
        return "not_software"
    return None


def decide(rubric: RubricCfg, facts: CompanyFacts, ctx: QualContext, unmet_rows: Optional[list[str]] = None
           ) -> Decision:
    unmet_rows = unmet_rows or []
    breakdown, trig = score_components(rubric, facts, ctx.today)
    score = breakdown.total
    saas = breakdown.get("saas_fit").points
    q = rubric.decision["qualified"]
    reasons: list[str] = []
    unknowns = list(unmet_rows)

    # best case if every unknown component came back at max -> lets the loop stop when nothing can change
    max_possible = score
    if facts.business_model is None:
        max_possible += 25
    if trig.points < 40:
        max_possible += 40 - trig.points + (10 - breakdown.get("recency").points)
    if facts.funding_stage is None:
        max_possible += 10
    if facts.buyer_type is None:
        max_possible += 10

    hd = hard_disqualifier(facts, ctx)
    if hd:
        return Decision(QualStatus.DUPLICATE if hd == "duplicate" else QualStatus.REJECTED, score, breakdown, trig,
                        hd, [f"hard disqualifier: {hd}"], unknowns, Confidence.HIGH, max_possible=max_possible)

    certified = already_certified(facts)
    if certified and ctx.already_certified_policy == "disqualify":
        return Decision(QualStatus.REJECTED, score, breakdown, trig, "already_certified",
                        ["already SOC 2 / ISO 27001 certified (policy: disqualify)"], unknowns, Confidence.HIGH,
                        max_possible=max_possible)

    if ctx.budget_exceeded:
        return Decision(QualStatus.INSUFFICIENT_DATA, score, breakdown, trig, None, ["budget_exceeded"], unknowns,
                        Confidence.LOW, handoff_note="Per-company cost/time budget hit before research finished.",
                        max_possible=max_possible)

    # Context that forces human research regardless of score
    force: list[str] = []
    if "homepage_unusable" in ctx.flags:
        force.append("site could not be read (blocked or empty)")
    if "domain_mismatch" in ctx.flags or facts.parent_company:
        force.append("multiple domains / parent entity: confirm which entity owns compliance")
    if facts.acquired_by or facts.funding_stage == "acquired":
        force.append("recently acquired: confirm which entity controls compliance")
    if facts.operating_status == "stealth":
        force.append("stealth company")
    if facts.conflicts:
        force.append("conflicting facts: " + "; ".join(facts.conflicts))

    trigger_quality_ok = trig.signal is not None and trig.signal.evidence_quality.rank >= \
        EvidenceQuality[q["min_trigger_quality"]].rank
    new_framework = False
    if certified and trig.signal is not None and trig.bucket in (
            "explicit_audit_target", "customer_requires_cert", "stated_deadline"):
        held = set().union(*[_fw(c) for c in facts.certifications_claimed]) if facts.certifications_claimed else set()
        new_framework = bool(_fw(trig.signal.signal + " " + (trig.signal.snippet or "")) - held)

    gates = {
        f"score >= {q['min_score']}": score >= q["min_score"],
        f"SaaS fit >= {q['min_saas_fit']}": saas >= q["min_saas_fit"],
        f"trigger >= {q['min_trigger']}": trig.points >= q["min_trigger"],
        f"trigger evidence >= {q['min_trigger_quality']}": trigger_quality_ok,
        "not already certified (or new framework targeted)": (not certified) or new_framework,
        "no context needing human research": not force,
    }
    failed = [k for k, ok in gates.items() if not ok]

    plausible_b2b_saas = saas >= q["min_saas_fit"] and facts.buyer_type in ("business", "both", None)
    lo, _hi = rubric.decision["needs_research_band"]

    if not failed:
        status = QualStatus.QUALIFIED
        reasons.append(f"all qualification gates passed (score {score})")
    elif force:
        status = QualStatus.NEEDS_RESEARCH
        reasons += force
    elif certified and not new_framework:
        status = QualStatus.NEEDS_RESEARCH
        reasons.append("already SOC 2 / ISO 27001 certified: check renewal and additional frameworks "
                       "(policy: research)")
    elif ctx.research_complete and "b2b_saas" in unmet_rows:
        # research was not adequate: we never established what they sell -> designed handoff, not a rejection
        status = QualStatus.INSUFFICIENT_DATA
        reasons.append("could not establish what the company sells / who buys it within the loop cap")
    elif score < lo and ctx.research_complete and not plausible_b2b_saas:
        status = QualStatus.REJECTED
        reasons.append(f"score {score} < {lo} after research and not a plausible B2B SaaS fit")
    else:
        status = QualStatus.NEEDS_RESEARCH
        reasons.append("gates not met: " + "; ".join(failed))
        if trig.points < q["min_trigger"]:
            reasons.append("compliance trigger plausible but unverified")

    # Confidence: how sure we are of the call (separate from the score)
    if status == QualStatus.QUALIFIED:
        conf = Confidence.HIGH if (trig.signal and trig.signal.evidence_quality == EvidenceQuality.STRONG
                                   and not unknowns) else Confidence.MEDIUM
    elif status == QualStatus.REJECTED:
        conf = Confidence.HIGH if not unknowns else Confidence.MEDIUM
    else:
        conf = Confidence.LOW if len(unknowns) >= 2 else Confidence.MEDIUM

    handoff = None
    if status in (QualStatus.NEEDS_RESEARCH, QualStatus.INSUFFICIENT_DATA):
        handoff = ("Human review: " + "; ".join(reasons)
                   + (f". Unknowns: {', '.join(unknowns)}" if unknowns else ""))
    return Decision(status, score, breakdown, trig, None, reasons, unknowns, conf, handoff, max_possible)
