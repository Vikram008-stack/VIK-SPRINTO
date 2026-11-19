"""Orchestrator: domains -> per-company outcome (+ audit rows + report). Every failure gets a reason; the run
continues. Hunter and Gmail calls are serialised; companies run in a small thread pool.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.common.budget import BudgetExceeded, CompanyBudget
from src.common.config import Settings, config_hash
from src.common.domains import parse_domains
from src.common.logging import get_logger
from src.contacts.hunter import HunterClient, HunterError, manual_contact, parse_contacts
from src.contacts.ranker import rank_contacts
from src.extract.extractor import extract
from src.llm.gemini_client import BaseLlm, CallCtx, LlmError
from src.mail.drafter import draft_email
from src.mail.gmail_sender import GmailSender
from src.pain.painpoints import gather_evidence, generate_pain_points
from src.pain.search_provider import CachedSearch
from src.qualify.loop import LoopDeps, propose_scores, run_loop, to_result
from src.qualify.scorer import QualContext, decide
from src.qualify.sufficiency import unmet
from src.scrape.fetcher import PageFetcher
from src.store.db import Database
from src.store.models import (
    CompanyStatus, ContactChoice, Failure, QualStatus, ReasonCode, Step,
)

log = get_logger(__name__)
DONE_STATUSES = {"QUALIFIED", "NEEDS_RESEARCH", "REJECTED", "INSUFFICIENT_DATA", "DUPLICATE", "NO_CONTACT_FOUND",
                 "DRAFT_NEEDS_HUMAN", "DRAFTED", "SENT"}
HUMAN_REVIEW = {"NEEDS_RESEARCH", "INSUFFICIENT_DATA", "NO_CONTACT_FOUND", "DRAFT_NEEDS_HUMAN", "SEND_FAILED",
                "FAILED"}


def _j(o: Any) -> Any:
    """JSON-safe dump of pydantic models / dataclasses / enums."""
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if hasattr(o, "__dataclass_fields__"):
        return {k: _j(getattr(o, k)) for k in o.__dataclass_fields__}
    if isinstance(o, (list, tuple)):
        return [_j(x) for x in o]
    if isinstance(o, dict):
        return {k: _j(v) for k, v in o.items()}
    if hasattr(o, "value") and not isinstance(o, (str, int, float, bool)):
        return o.value
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return o


@dataclass
class CompanyOutcome:
    domain: str
    input_raw: str
    run_id: str
    status: str = "PENDING"
    status_reason: Optional[str] = None
    next_action: str = ""
    company_name: Optional[str] = None
    confidence: Optional[str] = None
    qualification: Optional[dict] = None
    facts: Optional[dict] = None
    pages: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    loop_notes: list[str] = field(default_factory=list)
    extraction_dropped: list[str] = field(default_factory=list)
    model_proposed: Optional[dict] = None
    contact: Optional[dict] = None
    contact_candidates: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    pain_points: list[dict] = field(default_factory=list)
    pain_flags: list[str] = field(default_factory=list)
    draft: Optional[dict] = None
    draft_attempts: list[dict] = field(default_factory=list)
    send: Optional[dict] = None
    failures: list[dict] = field(default_factory=list)
    cost: dict = field(default_factory=dict)
    seconds: float = 0.0
    finished_at: Optional[str] = None
    versions: dict = field(default_factory=dict)
    human_override: Optional[dict] = None    # {decision, reason, by, at, original_status, original_score}

    def to_json(self) -> str:
        return json.dumps(_j(self), indent=2, default=str)


@dataclass
class RunOptions:
    dry_run: bool = True
    refresh: bool = False
    limit: Optional[int] = None
    command: str = "run"


NEXT_ACTION = {
    "QUALIFIED": "Proceed to contact selection",
    "SENT": "Delivered to the demo inbox. Review it there; nothing goes to a real prospect without approval",
    "DRAFTED": "Review the draft below, then send it to your demo inbox or run the follow-up agent",
    "DRAFT_NEEDS_HUMAN": "Rewrite the draft by hand: it failed the quality checks twice, so it was not sent",
    "NO_CONTACT_FOUND": "No suitable decision-maker was found; add one manually (config/manual_contacts.yaml)",
    "NEEDS_RESEARCH": "Check the open questions below. If you're satisfied, approve it manually to pick the "
                      "contact and draft the email, or reject it",
    "INSUFFICIENT_DATA": "Not enough public evidence. Approve manually if you know more, or reject it",
    "REJECTED": "No action: it does not fit the ICP (see the evidence)",
    "DUPLICATE": "No action: duplicate of another input",
    "SEND_FAILED": "Connect Gmail, then send again",
    "FAILED": "Run this company again; if it repeats, check the reason code",
}


def friendly_llm_error(e) -> str:
    """Plain-English status for an AI failure; the raw detail stays in the failures list (reason codes)."""
    d = (getattr(e, "detail", "") or "").lower()
    if any(k in d for k in ("503", "unavailable", "high demand", "overloaded")):
        return ("The AI model is overloaded right now (Google returned 'high demand' on every model we tried). "
                "The research is saved; try again in a minute.")
    if any(k in d for k in ("429", "quota", "resource_exhausted", "rate limit")):
        return ("The AI model's usage limit was reached (free-tier daily quota). Try again later, or enable "
                "billing on the Gemini key.")
    if "timeout" in d or "timed out" in d or "deadline" in d:
        return "The AI model took too long to answer. Try again in a minute."
    if getattr(e, "reason", None) is not None and e.reason.value == "llm_schema_error":
        return "The AI model's answer failed validation twice, so nothing was guessed. Try again."
    return "The AI step failed. The research is saved; try again in a minute."


class Pipeline:
    def __init__(self, settings: Settings, db: Database, llm: BaseLlm, fetcher: PageFetcher,
                 search: Optional[CachedSearch], hunter: HunterClient, sender: GmailSender, run_id: str,
                 today: Optional[date] = None, reports_dir: Optional[Path] = None,
                 outcomes_dir: Optional[Path] = None):
        self.s = settings
        self.db = db
        self.llm = llm
        self.fetcher = fetcher
        self.search = search
        self.hunter = hunter
        self.sender = sender
        self.run_id = run_id
        self.today = today or date.today()
        self.reports_dir = reports_dir or settings.pipeline.paths.abs("reports") / run_id
        self.outcomes_dir = outcomes_dir or settings.pipeline.paths.abs("db").parent / "outcomes"
        self._hunter_lock = threading.Lock()
        self._gmail_lock = threading.Lock()

    # ------------------------------------------------------------------ run
    def run(self, lines: list[str], opts: RunOptions) -> list[CompanyOutcome]:
        parsed = parse_domains(lines)
        self.db.start_run(self.run_id, opts.command, {"dry_run": opts.dry_run, "refresh": opts.refresh,
                                                      "limit": opts.limit}, opts.dry_run,
                          self.s.rubric.rubric_version, config_hash())
        outcomes: list[CompanyOutcome] = []
        for raw, err in parsed.invalid:
            self.db.log_failure(Failure(domain=None, step=Step.INPUT, reason=ReasonCode.INVALID_DOMAIN,
                                        detail=f"{raw}: {err}"), run_id=self.run_id)
        for d, raw in parsed.duplicates:
            o = CompanyOutcome(domain=d, input_raw=raw, run_id=self.run_id, status="DUPLICATE",
                               status_reason="duplicate_input", next_action=NEXT_ACTION["DUPLICATE"])
            o.failures.append({"step": "input", "reason": "duplicate_input", "detail": f"{raw} normalises to {d}"})
            self.db.log_failure(Failure(domain=d, step=Step.INPUT, reason=ReasonCode.DUPLICATE_INPUT,
                                        detail=f"{raw} normalises to {d}"), run_id=self.run_id)
            outcomes.append(o)
        todo = parsed.unique[: opts.limit] if opts.limit else parsed.unique
        with ThreadPoolExecutor(max_workers=self.s.pipeline.run.concurrency) as pool:
            results = list(pool.map(lambda dr: self._safe_process(dr[0], dr[1], opts), todo))
        outcomes = results + outcomes   # input order, duplicates last
        for o in outcomes:
            self._write_report(o)
        return outcomes

    def _safe_process(self, domain: str, raw: str, opts: RunOptions) -> CompanyOutcome:
        try:
            return self.process(domain, raw, opts)
        except Exception as e:  # last-resort: never crash the run
            o = CompanyOutcome(domain=domain, input_raw=raw, run_id=self.run_id, status="FAILED",
                               status_reason=ReasonCode.UNEXPECTED_ERROR.value, next_action=NEXT_ACTION["FAILED"])
            o.failures.append({"step": "pipeline", "reason": "unexpected_error",
                               "detail": f"{type(e).__name__}: {e}"[:500]})
            self.db.log_failure(Failure(domain=domain, step=Step.REPORT, reason=ReasonCode.UNEXPECTED_ERROR,
                                        detail=traceback.format_exc()[-1500:]), run_id=self.run_id)
            self._finish(o, None)
            return o

    # ------------------------------------------------------------------ per company
    def _fail(self, o: CompanyOutcome, step: Step, reason: ReasonCode, detail: str = "") -> None:
        o.failures.append({"step": step.value, "reason": reason.value, "detail": detail[:500]})
        self.db.log_failure(Failure(domain=o.domain, step=step, reason=reason, detail=detail[:1000]),
                            run_id=self.run_id)

    def _load_previous(self, domain: str) -> Optional[dict]:
        p = self.outcomes_dir / f"{domain}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def process(self, domain: str, raw: str, opts: RunOptions) -> CompanyOutcome:
        self.db.upsert_company(domain, raw, self.run_id)
        prev_row = self.db.company(domain)
        prev = self._load_previous(domain)
        if not opts.refresh and prev_row and prev_row["status"] in DONE_STATUSES and prev:
            if prev_row["status"] == "DRAFTED" and not opts.dry_run:
                return self._send_previous(prev, raw)
            o = CompanyOutcome(**{k: v for k, v in prev.items() if k in CompanyOutcome.__dataclass_fields__})
            o.loop_notes = list(o.loop_notes) + [f"skipped in run {self.run_id}: already {o.status} "
                                                 f"(use --refresh to redo)"]
            return o

        p = self.s.pipeline
        budget = CompanyBudget(max_cost_usd=p.run.max_cost_usd_per_company, max_seconds=p.run.max_seconds_per_company)
        o = CompanyOutcome(domain=domain, input_raw=raw, run_id=self.run_id,
                           versions={"rubric": self.s.rubric.rubric_version, **p.llm.prompt_versions,
                                     "models": dict(p.llm.models)})
        session = None
        try:
            # ---------------- Step 1: homepage (single fetch)
            session = self.fetcher.start(domain)
            home = self.fetcher.fetch_homepage(session, budget)
            if not home.ok:
                reason = home.meta.failure_reason or ReasonCode.HOMEPAGE_UNUSABLE
                self._fail(o, Step.SCRAPE, reason, home.meta.failure_detail or "homepage not usable")
                if "parked_domain" in session.flags:
                    d = decide(self.s.rubric, _empty(domain), QualContext(today=self.today, flags=session.flags,
                                                                           research_complete=True))
                    o.qualification = _j(to_result(domain, _wrap(d), "-", self.s.rubric.rubric_version))
                    return self._finish(o, session, "REJECTED", "parked_domain: not an operating business")
                o.qualification = _j(to_result(domain, _wrap(decide(
                    self.s.rubric, _empty(domain), QualContext(today=self.today, flags=session.flags,
                                                               research_complete=True))),
                    "-", self.s.rubric.rubric_version))
                return self._finish(o, session, "NEEDS_RESEARCH", f"site unreadable: {reason.value}")

            # ---------------- Step 2: extract
            ctx = CallCtx(domain, Step.EXTRACT, budget, self.run_id)
            footer = {home.meta.url: home.clean.footer_compliance} if home.clean.footer_compliance else {}
            ex = extract(self.llm, ctx, domain, home.clean.chunks, self.today, footer_compliance=footer)
            facts = ex.facts
            o.extraction_dropped += ex.dropped
            o.company_name = facts.company_name
            self.db.insert("extractions", {"run_id": self.run_id, "domain": domain, "round": 0, "model": ex.model,
                                           "prompt_version": ex.prompt_version, "facts_json": facts,
                                           "created_at": datetime.now(timezone.utc)})

            # ---------------- Step 3: qualify (stage gate + agent loop)
            deps = LoopDeps(self.s, self.db, self.fetcher, self.llm, self.search, self.today, self.run_id)
            gate = decide(self.s.rubric, facts, QualContext(today=self.today, flags=session.flags),
                          unmet(self.s.rubric, facts, session.fetched_types(), session.discovered))
            if gate.hard_disqualifier:
                out = _wrap(gate, facts, ["stage gate: hard disqualifier after homepage; no further fetches"])
            else:
                out = run_loop(deps, session, facts, budget)
                if out.decision.status != QualStatus.DUPLICATE:
                    out.model_proposed = propose_scores(deps, out.facts, budget)
            facts = out.facts
            o.company_name = facts.company_name or o.company_name
            result = to_result(domain, out, p.llm.prompt_versions.get("qualify", "v1"), self.s.rubric.rubric_version)
            o.qualification = _j(result)
            o.facts = _j(facts)
            o.loop_notes += out.notes
            o.model_proposed = _j(out.model_proposed) if out.model_proposed else None
            o.confidence = result.confidence.value
            self._persist_qualification(domain, result, facts)
            if result.score_diff:
                self._fail(o, Step.QUALIFY, ReasonCode.SCORE_DIFF,
                           f"model proposed {result.model_proposed_score}, Python computed {result.score}; "
                           f"Python wins")
            if result.status != QualStatus.QUALIFIED:
                why = "; ".join(result.reasons)[:300]
                return self._finish(o, session, result.status.value, why)

            return self._outreach(o, session, facts, "; ".join(result.reasons), out.search_evidence, budget, opts)

        except BudgetExceeded as e:
            self._fail(o, Step.QUALIFY, ReasonCode.BUDGET_EXCEEDED, str(e))
            return self._finish(o, session, "INSUFFICIENT_DATA", f"budget_exceeded: {e}")
        except LlmError as e:
            self._fail(o, Step.EXTRACT if o.qualification is None else Step.DRAFT, e.reason, e.detail)
            status = "INSUFFICIENT_DATA" if o.qualification is None else "DRAFT_NEEDS_HUMAN"
            return self._finish(o, session, status, friendly_llm_error(e))
        finally:
            o.cost = budget.snapshot()


    # ------------------------------------------------------------------ steps 4-7 (shared by run + human approval)
    def _outreach(self, o: CompanyOutcome, session, facts, why: str, prior_evidence, budget: CompanyBudget,
                  opts: RunOptions) -> CompanyOutcome:
        p = self.s.pipeline
        domain = o.domain
        # ---------------- Step 4: decision-maker
        choice = self._choose_contact(o, domain, facts)
        if choice.status != "CHOSEN" or not choice.chosen:
            self._fail(o, Step.CONTACTS, ReasonCode.NO_CONTACT_FOUND, choice.why_this_person)
            return self._finish(o, session, "NO_CONTACT_FOUND", choice.why_this_person[:300])
        contact = choice.chosen

        # ---------------- Step 5: pain points
        evidence, pflags, _n = gather_evidence(self.llm, self.search, facts, contact, budget, self.run_id,
                                               prior=prior_evidence)
        o.evidence = _j(evidence)
        pains, flags2 = generate_pain_points(self.llm, facts, contact, evidence, budget, self.run_id)
        o.pain_points = _j(pains)
        o.pain_flags = pflags + flags2
        for f in o.pain_flags:
            if f.startswith("search_unavailable"):
                self._fail(o, Step.PAIN, ReasonCode.SEARCH_UNAVAILABLE, f)
            elif f == "fewer_than_two_pain_points":
                self._fail(o, Step.PAIN, ReasonCode.FEWER_THAN_TWO_PAIN_POINTS, f)
        for i, pp in enumerate(pains, 1):
            self.db.insert("pain_points", {"run_id": self.run_id, "domain": domain, "position": i,
                                           "signal": pp.signal, "source_url": pp.source_url,
                                           "event_date": pp.event_date, "hypothesis": pp.hypothesis,
                                           "why_it_matters_to_this_role": pp.why_it_matters_to_this_role,
                                           "confidence": pp.confidence, "prompt_version":
                                               p.llm.prompt_versions.get("pain"),
                                           "created_at": datetime.now(timezone.utc)})
        if not pains:
            self._fail(o, Step.PAIN, ReasonCode.NO_PAIN_EVIDENCE, "no evidence-backed pain point")
            return self._finish(o, session, "DRAFT_NEEDS_HUMAN", "no evidence-backed pain point to write from")

        # ---------------- Step 6: draft
        dr = draft_email(self.llm, p.email, facts, contact, why, evidence, pains, budget, self.run_id)
        o.draft = _j(dr.draft) if dr.draft else None
        o.draft_attempts = [{"attempt": a["attempt"], "errors": a["errors"], "words": a["words"]}
                            for a in dr.attempts]
        for a in dr.attempts:
            self.db.insert("emails", {"run_id": self.run_id, "domain": domain, "attempt": a["attempt"],
                                      "draft_json": a["draft"], "guardrail_ok": not a["errors"],
                                      "guardrail_errors": a["errors"], "word_count": a["words"],
                                      "prompt_version": p.llm.prompt_versions.get("draft"),
                                      "created_at": datetime.now(timezone.utc)})
        if not dr.ok:
            self._fail(o, Step.DRAFT, ReasonCode.GUARDRAIL_FAILED, "; ".join(dr.attempts[-1]["errors"]))
            return self._finish(o, session, "DRAFT_NEEDS_HUMAN", "guardrails failed twice; not sent")

        # ---------------- Step 7: send (demo inbox only)
        return self._send(o, session, dr.draft, contact, opts)


    def approve(self, outcome: dict, decision: str, reason: str, reviewer: str = "reviewer",
                opts: Optional[RunOptions] = None) -> CompanyOutcome:
        """Human-in-the-loop override for a company the rules did not qualify. The original verdict and score are
        kept; the human decision and reason are logged. Approve -> steps 4-7 run on the facts already gathered."""
        from src.store.models import CompanyFacts
        opts = opts or RunOptions(dry_run=True, command="approve")
        o = CompanyOutcome(**{k: v for k, v in outcome.items() if k in CompanyOutcome.__dataclass_fields__})
        o.run_id = self.run_id
        q = o.qualification or {}
        o.human_override = {"decision": decision, "reason": reason, "by": reviewer,
                            "at": datetime.now(timezone.utc).isoformat(), "original_status": o.status,
                            "original_score": q.get("score")}
        self.db.start_run(self.run_id, "approve", {"domain": o.domain, "decision": decision}, opts.dry_run,
                          self.s.rubric.rubric_version, config_hash())
        self.db.upsert_company(o.domain, o.input_raw, self.run_id)
        o.loop_notes = list(o.loop_notes) + [f"human {decision}: {reason}"]
        if decision == "reject":
            out = self._finish(o, None, "REJECTED", f"Rejected by a human reviewer: {reason}")
            self._write_report(out)
            return out
        budget = CompanyBudget(max_cost_usd=self.s.pipeline.run.max_cost_usd_per_company,
                               max_seconds=self.s.pipeline.run.max_seconds_per_company)
        try:
            facts = CompanyFacts(**(o.facts or {"domain": o.domain}))
            why = f"Approved by a human reviewer ({reason}); rule-based score was {q.get('score')}"
            out = self._outreach(o, None, facts, why, None, budget, opts)
        except BudgetExceeded as e:
            self._fail(o, Step.DRAFT, ReasonCode.BUDGET_EXCEEDED, str(e))
            out = self._finish(o, None, "DRAFT_NEEDS_HUMAN", f"budget_exceeded: {e}")
        except LlmError as e:
            self._fail(o, Step.DRAFT, e.reason, e.detail)
            out = self._finish(o, None, "DRAFT_NEEDS_HUMAN", friendly_llm_error(e))
        finally:
            o.cost = budget.snapshot()
        if out.status_reason and not out.status_reason.startswith("Approved"):
            out.status_reason = f"Approved by a human ({reason}). " + out.status_reason
        self._write_report(out)
        return out

    # ------------------------------------------------------------------ helpers
    def _choose_contact(self, o: CompanyOutcome, domain: str, facts) -> ContactChoice:
        contacts = []
        with self._hunter_lock:
            try:
                data = self.hunter.domain_search(domain)
                contacts = parse_contacts(domain, data)
                if not contacts:
                    self._fail(o, Step.CONTACTS, ReasonCode.HUNTER_NO_RESULTS, "Hunter returned no people")
            except HunterError as e:
                self._fail(o, Step.CONTACTS, e.reason, e.detail)
        choice, ranked = rank_contacts(domain, contacts, self.s.roles, facts.employee_count_estimate)
        o.contact_candidates = [{"name": r.contact.name, "position": r.contact.position, "tier": r.tier,
                                 "excluded": r.excluded, "confidence": r.contact.confidence,
                                 "source": r.contact.source} for r in ranked]
        if choice.status != "CHOSEN":
            m = manual_contact(domain, self.s.manual_contacts)
            if m:
                choice = ContactChoice(domain=domain, chosen=m, why_this_person=(
                    f"{m.name}, {m.position}: from config/manual_contacts.yaml (source: apollo_manual) because "
                    f"Hunter gave no suitable person. " + choice.why_this_person)[:800],
                    ranking_factors=["manual fallback (Option B)"])
        if choice.status == "CHOSEN" and choice.chosen and choice.chosen.email and choice.chosen.source == "hunter":
            with self._hunter_lock:
                status = self.hunter.verify(domain, choice.chosen.email)
            if status:
                choice.chosen.verification_status = status
                if status in ("invalid", "disposable"):
                    self._fail(o, Step.CONTACTS, ReasonCode.EMAIL_UNVERIFIED, f"{choice.chosen.email}: {status}")
        o.contact = _j(choice)
        for role, c in (("chosen", choice.chosen), ("runner_up", choice.runner_up)):
            if c:
                self.db.insert("contacts", {"run_id": self.run_id, "domain": domain, "role": role, "name": c.name,
                                            "position": c.position, "seniority": c.seniority,
                                            "department": c.department, "email": c.email,
                                            "confidence": c.confidence, "verification_status": c.verification_status,
                                            "source": c.source, "why_this_person": choice.why_this_person
                                            if role == "chosen" else None,
                                            "created_at": datetime.now(timezone.utc)})
        return choice

    def _send(self, o: CompanyOutcome, session, draft, contact, opts: RunOptions) -> CompanyOutcome:
        self.sender.dry_run = opts.dry_run
        with self._gmail_lock:
            res = self.sender.send(draft, contact, o.domain)
        o.send = _j(res)
        self.db.insert("sends", {"run_id": self.run_id, "domain": o.domain, "to_address": res.to,
                                 "intended_recipient": res.intended_recipient, "status": res.status,
                                 "message_id": res.message_id, "reason": res.reason, "detail": res.detail,
                                 "sent_at": res.timestamp})
        if res.status == "FAILED":
            self._fail(o, Step.SEND, res.reason or ReasonCode.GMAIL_SEND_ERROR, res.detail or "")
            return self._finish(o, session, "SEND_FAILED", f"{res.reason.value if res.reason else 'send failed'}")
        status = "SENT" if res.status == "SENT" else "DRAFTED"
        return self._finish(o, session, status,
                            f"sent to demo inbox, message_id {res.message_id}" if status == "SENT"
                            else "dry run: draft stored, nothing sent")

    def _send_previous(self, prev: dict, raw: str) -> CompanyOutcome:
        from src.store.models import Contact, EmailDraft
        o = CompanyOutcome(**{k: v for k, v in prev.items() if k in CompanyOutcome.__dataclass_fields__})
        o.run_id = self.run_id
        draft = EmailDraft(**prev["draft"])
        contact = Contact(**prev["contact"]["chosen"])
        o.loop_notes = list(o.loop_notes) + [f"run {self.run_id}: sending previously approved dry-run draft"]
        return self._send(o, None, draft, contact, RunOptions(dry_run=False))

    def _persist_qualification(self, domain: str, r, facts) -> None:
        now = datetime.now(timezone.utc)
        for rd in r.rounds:
            self.db.insert("qualification_rounds", {"run_id": self.run_id, "domain": domain, "round": rd.round,
                                                    "missing_before": rd.missing_before,
                                                    "fetched_urls": rd.fetched_urls,
                                                    "missing_after": rd.missing_after, "score": rd.score,
                                                    "tokens": rd.tokens, "cost_usd": rd.cost_usd, "note": rd.note,
                                                    "created_at": now})
        self.db.insert("qualifications", {"run_id": self.run_id, "domain": domain, "status": r.status,
                                          "score": r.score, "breakdown_json": r.breakdown,
                                          "model_proposed_score": r.model_proposed_score,
                                          "score_diff": r.score_diff, "hard_disqualifier": r.hard_disqualifier,
                                          "reasons_json": r.reasons, "unknowns_json": r.unknowns,
                                          "confidence": r.confidence, "handoff_note": r.handoff_note,
                                          "rubric_version": r.rubric_version, "prompt_version": r.prompt_version,
                                          "created_at": now})
        scored = {u for c in r.breakdown.components for u in c.source_urls}
        trig = r.breakdown.get("compliance_trigger")
        for kind, sigs in (("compliance", facts.compliance_signals), ("enterprise", facts.enterprise_signals)):
            for sg in sigs:
                pts = trig.points if (trig and sg.source_url in trig.source_urls and sg.trigger_type == trig.bucket) \
                    else 0
                self.db.insert("signals", {"run_id": self.run_id, "domain": domain, "kind": kind,
                                           "signal": sg.signal, "snippet": sg.snippet, "source_url": sg.source_url,
                                           "source_origin": sg.source_origin, "event_date": sg.event_date,
                                           "observed_at": sg.observed_at, "quality": sg.evidence_quality,
                                           "trigger_type": sg.trigger_type, "points": pts})
        for h in facts.hiring_signals:
            self.db.insert("signals", {"run_id": self.run_id, "domain": domain, "kind": "hiring", "signal": h.title,
                                       "source_url": h.source_url, "source_origin": "fetched_page",
                                       "event_date": h.event_date, "observed_at": now, "quality": "MEDIUM",
                                       "points": 0})
        _ = scored

    def _finish(self, o: CompanyOutcome, session, status: Optional[str] = None,
                reason: Optional[str] = None) -> CompanyOutcome:
        if status:
            o.status = status
        if reason:
            o.status_reason = reason
        o.next_action = NEXT_ACTION.get(o.status, "")
        if session is not None:
            o.pages = [{"url": pg.meta.url, "final_url": pg.meta.final_url, "page_type": pg.meta.page_type,
                        "mode": pg.meta.mode.value, "ok": pg.meta.ok,
                        "reason": pg.meta.failure_reason.value if pg.meta.failure_reason else None,
                        "raw_tokens": pg.meta.raw_tokens, "clean_tokens": pg.meta.clean_tokens,
                        "credits": pg.meta.credits, "from_cache": pg.meta.from_cache} for pg in session.pages]
            o.flags = list(dict.fromkeys(session.flags))
        o.finished_at = datetime.now(timezone.utc).isoformat()
        o.seconds = round(o.cost.get("seconds", 0.0), 2) if o.cost else o.seconds
        self.db.set_company_status(o.domain, o.status, (o.status_reason or "")[:300],
                                   company_name=o.company_name, completed_at=o.finished_at)
        return o

    def _write_report(self, o: CompanyOutcome) -> None:
        from src.report.defensibility import render_company
        if o.cost:
            o.seconds = round(o.cost.get("seconds", 0.0), 2)
            self.db.set_company_status(o.domain, o.status, (o.status_reason or "")[:300],
                                       seconds_spent=o.seconds, cost_usd=o.cost.get("cost_usd"))
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.outcomes_dir.mkdir(parents=True, exist_ok=True)
        (self.reports_dir / f"{o.domain}.json").write_text(o.to_json(), encoding="utf-8")
        (self.reports_dir / f"{o.domain}.md").write_text(render_company(json.loads(o.to_json())), encoding="utf-8")
        if o.status != "DUPLICATE":
            (self.outcomes_dir / f"{o.domain}.json").write_text(o.to_json(), encoding="utf-8")


# ------------------------------------------------------------------ small adapters
def _empty(domain: str):
    from src.store.models import CompanyFacts
    return CompanyFacts(domain=domain)


def _wrap(d, facts=None, notes=None):
    from src.qualify.loop import LoopOutcome
    from src.store.models import QualificationRound
    return LoopOutcome(decision=d, facts=facts or _empty("x"), rounds=[QualificationRound(
        round=0, missing_before=[], fetched_urls=[], missing_after=d.unknowns, score=d.score,
        note="homepage only")], notes=notes or [])
