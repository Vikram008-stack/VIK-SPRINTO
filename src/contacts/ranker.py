"""Deterministic decision-maker ranking (roles.yaml). Hunter finds people; this code chooses the person.

A title containing "manager" or "head" is NOT enough: the title must match a tier, and irrelevant functions
(sales, marketing, HR, ...) are penalised out.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.common.config import RolesCfg
from src.contacts.hunter import PERSONAL_DOMAINS
from src.store.models import Contact, ContactChoice

VERIFY_RANK = {"valid": 3, "accept_all": 2, "unknown": 1, None: 1, "pending": 1, "invalid": 0, "disposable": 0,
               "webmail": 0}
SENIORITY_RANK = {"executive": 3, "senior": 2, "junior": 1, None: 0}
TIER_LABEL = {1: "security leadership", 2: "compliance / GRC", 3: "engineering / IT leadership",
              4: "founder / CEO"}


@dataclass
class Ranked:
    contact: Contact
    tier: Optional[int]
    excluded: Optional[str]
    factors: list[str]

    def key(self):
        c = self.contact
        return (-(5 - self.tier) if self.tier else 0, -(c.confidence or 0),
                -VERIFY_RANK.get(c.verification_status, 1), -SENIORITY_RANK.get(c.seniority, 0))


def _tier(title: str, roles: RolesCfg, small: bool) -> Optional[int]:
    t = f" {title.lower()} "
    if small and any(p in t for p in roles.small_company["promote"]):
        return int(roles.small_company["promote_to_tier"])
    for tier in sorted(roles.tiers):
        for pat in roles.tiers[tier]:
            p = pat.lower()
            if (f" {p} " in t) or (f" {p}," in t) or (len(p) > 4 and p in t):
                return tier
    return None


def rank_contacts(domain: str, contacts: list[Contact], roles: RolesCfg,
                  employee_count: Optional[int]) -> tuple[ContactChoice, list[Ranked]]:
    small = employee_count is not None and employee_count <= int(roles.small_company["max_employees"])
    ranked: list[Ranked] = []
    for c in contacts:
        title = c.position or ""
        factors: list[str] = []
        excluded = None
        email_domain = (c.email or "").split("@")[-1].lower()
        if not c.email:
            excluded = "no email"
        elif email_domain in PERSONAL_DOMAINS:
            excluded = "personal-looking email"
        elif c.source == "hunter" and (c.confidence or 0) < roles.min_confidence:
            excluded = f"Hunter confidence {c.confidence} < {roles.min_confidence}"
        elif c.verification_status in ("invalid", "disposable"):
            excluded = f"email {c.verification_status}"
        tier = _tier(title, roles, small) if title else None
        penalised = [p for p in roles.penalise if p in title.lower()]
        if penalised and tier not in (1, 2):
            factors.append(f"penalised title ({', '.join(penalised)})")
            tier = None
        if tier is None and not excluded:
            excluded = "title not a compliance decision-maker" if title else "no title"
        if tier:
            factors.append(f"tier {tier}: {TIER_LABEL[tier]}" + (" (promoted: small company)" if small and tier == 1
                                                                 and not any(k in title.lower() for k in
                                                                             ("security", "ciso")) else ""))
        if c.confidence is not None:
            factors.append(f"Hunter confidence {c.confidence}")
        if c.verification_status:
            factors.append(f"verification {c.verification_status}")
        if c.seniority:
            factors.append(f"seniority {c.seniority}")
        ranked.append(Ranked(c, tier, excluded, factors))

    eligible = sorted([r for r in ranked if not r.excluded], key=lambda r: r.key())
    if not eligible:
        reasons = "; ".join(f"{r.contact.name} ({r.contact.position or 'no title'}): {r.excluded}" for r in ranked)
        return ContactChoice(domain=domain, status="NO_CONTACT_FOUND",
                             why_this_person=f"no suitable decision-maker among {len(ranked)} contacts. {reasons}"
                             .strip()), ranked
    best = eligible[0]
    runner = eligible[1] if len(eligible) > 1 else None
    why = (f"{best.contact.name}, {best.contact.position}: " + ", ".join(best.factors)
           + (f". Company size {employee_count} (<= {roles.small_company['max_employees']}: founders/CTO own "
              f"compliance)." if small else "."))
    if runner:
        why += f" Runner-up: {runner.contact.name}, {runner.contact.position} ({', '.join(runner.factors)})."
    return ContactChoice(domain=domain, chosen=best.contact, runner_up=runner.contact if runner else None,
                         why_this_person=why, ranking_factors=best.factors, status="CHOSEN"), ranked
