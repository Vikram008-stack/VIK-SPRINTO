"""Step 3 agent loop: an explicit state machine (not free-form agent chatter).

  EXTRACTED -> check sufficiency (code) -> [stop?] -> plan targeted fetches (Evidence Map) -> fetch ONLY those
            -> re-extract affected fields -> merge with provenance -> re-score -> ... (max 2 extra rounds)
  At the cap: unresolved -> designed handoff (NEEDS_RESEARCH / INSUFFICIENT_DATA with unknowns + note).

The model judges facts; the code decides whether there is enough to judge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from string import Template
from typing import Optional

from pydantic import BaseModel

from src.common.budget import BudgetExceeded, CompanyBudget
from src.common.config import Settings
from src.common.logging import get_logger
from src.extract.extractor import extract, merge_facts
from src.llm.gemini_client import BaseLlm, CallCtx, LlmError
from src.llm.prompts import QUALIFY_SYSTEM_V1, QUALIFY_USER_V1
from src.pain.research import EvidenceItem, structure_search
from src.pain.search_provider import CachedSearch, SearchUnavailable
from src.qualify.scorer import Decision, QualContext, decide
from src.qualify.sufficiency import ROW_FIELDS, unmet
from src.scrape.fetcher import FetchSession, PageFetcher
from src.scrape.links import candidates
from src.store.db import Database
from src.store.models import (
    CompanyFacts, Failure, QualificationResult, QualificationRound, QualStatus, ReasonCode, Step,
)

log = get_logger(__name__)

SOURCE_TO_PAGE = {"/careers": "careers", "/jobs": "careers", "/security": "security", "/trust": "security",
                  "/product": "product", "/solutions": "product", "/about": "about", "/pricing": "pricing",
                  "/customers": "customers", "/blog": "news", "/news": "news"}
ROW_PRIORITY = ["trigger", "stage", "already_certified", "b2b_saas"]


class ProposedScores(BaseModel):
    saas_fit: int
    compliance_trigger: int
    stage: int
    enterprise: int
    recency: int
    rationale: str


@dataclass
class LoopOutcome:
    decision: Decision
    facts: CompanyFacts
    rounds: list[QualificationRound] = field(default_factory=list)
    search_evidence: list[EvidenceItem] = field(default_factory=list)
    model_proposed: Optional[ProposedScores] = None
    notes: list[str] = field(default_factory=list)


@dataclass
class LoopDeps:
    settings: Settings
    db: Database
    fetcher: PageFetcher
    llm: BaseLlm
    search: Optional[CachedSearch]
    today: date
    run_id: Optional[str] = None


def _ctx(deps: LoopDeps, domain: str, budget: Optional[CompanyBudget]) -> CallCtx:
    return CallCtx(domain=domain, step=Step.QUALIFY, budget=budget, run_id=deps.run_id)


def _qctx(deps: LoopDeps, session: FetchSession, complete: bool, budget_exceeded: bool = False) -> QualContext:
    return QualContext(today=deps.today, flags=list(session.flags), research_complete=complete,
                       already_certified_policy=deps.settings.pipeline.qualification.already_certified_policy,
                       budget_exceeded=budget_exceeded)


def plan_fetches(deps: LoopDeps, session: FetchSession, unmet_rows: list[str], row_fetches: dict[str, int],
                 attempted_types: set[str]) -> tuple[list[tuple[str, str, list[str]]], bool]:
    """Returns ([(url, page_type, rows_served)], wants_search). One new page per unmet row per round."""
    rows = {r.id: r for r in deps.settings.rubric.evidence_map}
    plan: dict[str, tuple[str, str, list[str]]] = {}
    wants_search = False
    budget_left = session.remaining
    for rid in [r for r in ROW_PRIORITY if r in unmet_rows]:
        row = rows[rid]
        if row_fetches.get(rid, 0) >= row.fetch_limit:
            if "search" in row.sources:
                wants_search = True
            continue
        chosen = None
        for src in row.sources:
            if src == "search":
                continue
            ptype = SOURCE_TO_PAGE.get(src)
            if ptype is None:
                continue
            if ptype in plan:            # already planned this round (e.g. careers serves stage + trigger)
                plan[ptype][2].append(rid)
                chosen = ptype
                break
            if ptype in attempted_types:
                continue
            urls = [u for u in candidates(session.domain, ptype, session.discovered, session.scheme,
                                          session.base_host) if u.rstrip("/") not in session.fetched_urls()]
            if urls and len(plan) < budget_left:
                plan[ptype] = (urls[0], ptype, [rid])
                chosen = ptype
                break
        if chosen is None and "search" in row.sources:
            wants_search = True
    return list(plan.values()), wants_search


def run_loop(deps: LoopDeps, session: FetchSession, facts: CompanyFacts,
             budget: Optional[CompanyBudget] = None) -> LoopOutcome:
    s = deps.settings
    rubric = s.rubric
    max_rounds = s.pipeline.qualification.max_extra_rounds
    row_fetches: dict[str, int] = {}
    attempted: set[str] = {p.meta.page_type for p in session.pages if p.meta.page_type}
    rounds: list[QualificationRound] = []
    search_items: list[EvidenceItem] = []
    notes: list[str] = []
    searched = False

    missing = unmet(rubric, facts, session.fetched_types(), session.discovered)
    d = decide(rubric, facts, _qctx(deps, session, False), missing)
    rounds.append(QualificationRound(round=0, missing_before=[], fetched_urls=[p.meta.url for p in session.pages],
                                     missing_after=missing, score=d.score, note="after homepage extraction"))

    for rnd in range(1, max_rounds + 1):
        # ---- stop conditions: decision cannot change
        if d.hard_disqualifier or d.status == QualStatus.QUALIFIED:
            notes.append(f"stopped before round {rnd}: decision final ({d.status.value})")
            break
        if not missing:
            notes.append(f"stopped before round {rnd}: all Evidence-Map rows satisfied")
            break
        saas = d.breakdown.get("saas_fit").points
        if facts.business_model is not None and saas < rubric.decision["qualified"]["min_saas_fit"]:
            notes.append(f"stopped before round {rnd}: SaaS fit {saas} can never qualify")
            break
        if "homepage_unusable" in session.flags and not session.pages[-1].ok and rnd == 1 \
                and not session.discovered:
            notes.append("homepage unusable: no links to follow; handing off")
            break

        plan, wants_search = plan_fetches(deps, session, missing, row_fetches, attempted)
        can_search = (wants_search or rnd == max_rounds) and "trigger" in missing and not searched and \
            deps.search is not None and s.pipeline.search.loop_may_search
        if not plan and not can_search:
            notes.append(f"stopped before round {rnd}: nothing left to fetch for {missing}")
            break

        tokens_before = budget.tokens if budget else 0
        cost_before = budget.cost_usd if budget else 0.0
        fetched_urls: list[str] = []
        new_pages = []
        try:
            for url, ptype, served in plan:
                attempted.add(ptype)
                for rid in served:
                    row_fetches[rid] = row_fetches.get(rid, 0) + 1
                page = deps.fetcher.fetch(session, url, ptype, round_no=rnd, budget=budget)
                fetched_urls.append(url)
                if page.ok and page.clean:
                    new_pages.append(page)
            focus = sorted({f for rid in missing for f in ROW_FIELDS.get(rid, [])})
            if new_pages:
                chunks = [c for p in new_pages for c in p.clean.chunks]
                footer = {p.meta.url: p.clean.footer_compliance for p in new_pages if p.clean.footer_compliance}
                ex = extract(deps.llm, _ctx(deps, session.domain, budget), session.domain, chunks, deps.today,
                             focus_fields=focus, footer_compliance=footer)
                facts = merge_facts(facts, ex.facts, focus)
            if can_search:
                searched = True
                try:
                    company = facts.company_name or session.domain
                    res = deps.search.search(_ctx(deps, session.domain, budget), session.domain, company,
                                             "funding rounds, SOC 2 / ISO 27001 / compliance or audit plans, "
                                             "enterprise customers or security requirements (last 12 months)")
                    fetched_urls.append(f"search:{res.provider}")
                    items, rej = structure_search(deps.llm, _ctx(deps, session.domain, budget), res,
                                                  session.domain, company, id_prefix="Q")
                    notes += rej
                    search_items += items
                    now = datetime.now(timezone.utc)
                    for it in items:
                        if it.trigger_type and it.kind in ("compliance", "enterprise"):
                            sig = it.to_signal(now)
                            (facts.compliance_signals if it.kind == "compliance"
                             else facts.enterprise_signals).append(sig)
                        if it.funding_stage and facts.funding_stage is None:
                            facts.funding_stage = it.funding_stage
                            facts.field_sources["funding_stage"] = it.source_url
                except SearchUnavailable as e:
                    notes.append(f"search_unavailable: {e}")
                    deps.db.log_failure(Failure(domain=session.domain, step=Step.QUALIFY,
                                                reason=ReasonCode.SEARCH_UNAVAILABLE, detail=str(e)[:300]),
                                        run_id=deps.run_id)
        except BudgetExceeded as e:
            notes.append(str(e))
            rounds.append(QualificationRound(round=rnd, missing_before=missing, fetched_urls=fetched_urls,
                                             missing_after=missing, note=f"budget_exceeded: {e}"))
            final = decide(rubric, facts, _qctx(deps, session, True, budget_exceeded=True), missing)
            return LoopOutcome(final, facts, rounds, search_items, None, notes)

        missing_before = missing
        missing = unmet(rubric, facts, session.fetched_types() | attempted, session.discovered)
        d = decide(rubric, facts, _qctx(deps, session, False), missing)
        r = QualificationRound(round=rnd, missing_before=missing_before, fetched_urls=fetched_urls,
                               missing_after=missing, score=d.score,
                               tokens=(budget.tokens - tokens_before) if budget else 0,
                               cost_usd=round((budget.cost_usd - cost_before), 6) if budget else None)
        rounds.append(r)
        log.info("loop round", extra={"domain": session.domain, "round": rnd, "missing_before": missing_before,
                                      "fetched": fetched_urls, "missing_after": missing, "score": d.score})

    final = decide(rubric, facts, _qctx(deps, session, True), missing)
    return LoopOutcome(final, facts, rounds, search_items, None, notes)


def propose_scores(deps: LoopDeps, facts: CompanyFacts, budget: Optional[CompanyBudget]) -> Optional[ProposedScores]:
    """Stronger model proposes component scores; advisory only. Python's score always wins."""
    user = Template(QUALIFY_USER_V1).substitute(today=deps.today.isoformat(),
                                                facts=facts.model_dump_json(indent=1)[:12000])
    try:
        return deps.llm.generate_json(CallCtx(facts.domain, Step.QUALIFY, budget, deps.run_id), "qualify",
                                      QUALIFY_SYSTEM_V1, user, ProposedScores).parsed
    except (LlmError, BudgetExceeded) as e:
        log.warning("model score proposal failed", extra={"domain": facts.domain, "err": str(e)[:200]})
        return None


def to_result(domain: str, out: LoopOutcome, prompt_version: str, rubric_version: str) -> QualificationResult:
    d = out.decision
    proposed = None
    diff = None
    if out.model_proposed:
        p = out.model_proposed
        proposed = p.saas_fit + p.compliance_trigger + p.stage + p.enterprise + p.recency
        diff = proposed - d.score
    return QualificationResult(
        domain=domain, status=d.status, score=d.score, breakdown=d.breakdown, model_proposed_score=proposed,
        score_diff=diff, hard_disqualifier=d.hard_disqualifier, reasons=d.reasons, unknowns=d.unknowns,
        confidence=d.confidence, handoff_note=d.handoff_note, rounds=out.rounds, gates=d.gates,
        rubric_version=rubric_version,
        prompt_version=prompt_version)
