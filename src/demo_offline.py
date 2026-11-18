"""Offline demo world: runs the WHOLE pipeline with zero API calls and zero credits.

Fictional companies served from tests/fixtures/html, recorded model responses (below), Hunter mock fixtures,
fake grounded search with reserved example.com sources, and a dry-run send. Used by `cli demo` and by tests.
Nothing here is presented as real-world data.
"""
from __future__ import annotations

import re
from pathlib import Path

from src.llm.gemini_client import Citation

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "html"

DOMAINS = ["acme-thin.io", "ledgerly.io", "brightpath.io", "schedulr.io", "blocked-demo.io", "www.acme-thin.io"]


def _h(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def pages() -> dict:
    return {
        "https://acme-thin.io/": _h("thin_homepage.html"),
        "https://acme-thin.io/careers": _h("careers_compliance.html"),
        "https://ledgerly.io/": _h("nav_heavy.html"),
        "https://brightpath.io/": _h("consultancy_home.html"),
        "https://schedulr.io/": _h("certified_saas_home.html"),
        "https://blocked-demo.io/": _h("cloudflare_challenge.html"),
    }


# ------------------------------------------------------------------ recorded extraction responses
def _fact(v, url, snip):
    return {"value": v, "source_url": url, "snippet": snip}


EXTRACT = {
    "https://acme-thin.io/careers": lambda: {
        "funding_stage": _fact("series_a", "https://acme-thin.io/careers",
                               "We are a Series A company backed by Example Capital"),
        "signals": [{"kind": "compliance",
                     "signal": "Hiring a Head of Compliance to own SOC 2 / ISO 27001 and lead the first SOC 2 Type II audit",
                     "snippet": "lead our first SOC 2 Type II audit, working with engineering to answer enterprise "
                                "security questionnaires",
                     "source_url": "https://acme-thin.io/careers", "event_date": "2026-10-20",
                     "evidence_quality": "MEDIUM", "trigger_type": "grc_leader_hire"}],
        "hiring": [{"title": t, "source_url": "https://acme-thin.io/careers", "event_date": d} for t, d in [
            ("Head of Compliance", "2026-10-20"), ("Senior Backend Engineer", "2026-10-02"),
            ("Product Designer", "2026-09-28"), ("Account Executive, Enterprise", "2026-10-11"),
            ("Customer Success Manager", "2026-09-15"), ("Data Engineer", "2026-10-05"),
            ("Frontend Engineer", "2026-10-09")]],
    },
    "https://acme-thin.io/": lambda: {
        "company_name": _fact("Acme Thin", "https://acme-thin.io/", "Acme Thin is a cloud subscription product"),
        "product_description": _fact("Invoice automation for B2B finance teams", "https://acme-thin.io/",
                                     "Invoice automation for B2B finance teams"),
        "business_model": _fact("cloud_subscription", "https://acme-thin.io/",
                                "Acme Thin is a cloud subscription product that reads supplier invoices"),
        "buyer_type": _fact("business", "https://acme-thin.io/",
                            "Finance teams at growing B2B software companies use Acme Thin"),
    },
    "https://ledgerly.io/": lambda: {
        "company_name": _fact("Ledgerly", "https://ledgerly.io/", "Ledgerly is a cloud subscription platform"),
        "product_description": _fact("Spend management for finance teams", "https://ledgerly.io/",
                                     "Spend management for modern finance teams"),
        "business_model": _fact("cloud_subscription", "https://ledgerly.io/",
                                "Ledgerly is a cloud subscription platform that helps finance teams"),
        "buyer_type": _fact("business", "https://ledgerly.io/",
                            "finance teams at mid-market and enterprise B2B companies"),
        "funding_stage": _fact("series_b", "https://ledgerly.io/", "Ledgerly raised a $24M Series B led by Example Ventures"),
        "has_enterprise_pricing_tier": _fact("true", "https://ledgerly.io/",
                                             "an Enterprise plan that adds SSO, audit logs and a dedicated success manager"),
        "signals": [
            {"kind": "compliance", "signal": "Preparing for a SOC 2 Type II audit scheduled for Q1 2027 after enterprise "
                                             "prospects asked for the report",
             "snippet": "We are preparing for our SOC 2 Type II audit, which is scheduled for the first quarter of 2027",
             "source_url": "https://ledgerly.io/", "event_date": None, "evidence_quality": "STRONG",
             "trigger_type": "explicit_audit_target"},
            {"kind": "enterprise", "signal": "Enterprise customer Northwind Logistics routes purchases through security reviews",
             "snippet": "Enterprise customers like Northwind Logistics use Ledgerly",
             "source_url": "https://ledgerly.io/", "event_date": None, "evidence_quality": "STRONG",
             "trigger_type": None}],
    },
    "https://brightpath.io/": lambda: {
        "company_name": _fact("Brightpath", "https://brightpath.io/", "Brightpath is an IT consultancy and outsourcing firm"),
        "business_model": _fact("it_services", "https://brightpath.io/",
                                "Brightpath is an IT consultancy and outsourcing firm"),
        "buyer_type": _fact("business", "https://brightpath.io/", "managed services for banks and retailers"),
        "funding_stage": _fact("series_b", "https://brightpath.io/", "Brightpath recently closed a Series B round"),
    },
    "https://schedulr.io/": lambda: {
        "company_name": _fact("Schedulr", "https://schedulr.io/", "Schedulr is an open-source scheduling platform"),
        "business_model": _fact("cloud_subscription", "https://schedulr.io/",
                                "sold as a cloud subscription to businesses"),
        "buyer_type": _fact("business", "https://schedulr.io/", "sold as a cloud subscription to businesses"),
        "has_enterprise_pricing_tier": _fact("true", "https://schedulr.io/",
                                             "with an Enterprise plan for large organisations"),
        "certifications_claimed": [_fact("SOC 2 Type II", "https://schedulr.io/",
                                         "Schedulr is SOC 2 Type II certified and HIPAA compliant")],
        "signals": [{"kind": "enterprise", "signal": "Enterprise customers in healthcare and finance",
                     "snippet": "including enterprise customers in healthcare and finance",
                     "source_url": "https://schedulr.io/", "event_date": None, "evidence_quality": "MEDIUM",
                     "trigger_type": None}],
    },
}


def fake_extract(system: str, user: str) -> dict:
    # careers first: its SOURCE header is the more specific match
    for url in sorted(EXTRACT, key=len, reverse=True):
        if f"=== SOURCE {url} " in user:
            return EXTRACT[url]()
    return {}


def fake_qualify(system: str, user: str) -> dict:
    proposals = {"ledgerly.io": (25, 40, 15, 10, 2), "acme-thin.io": (25, 25, 15, 4, 10),
                 "schedulr.io": (25, 10, 5, 10, 0)}
    for d, (a, b, c, e, r) in proposals.items():
        if f'"domain": "{d}"' in user:
            return {"saas_fit": a, "compliance_trigger": b, "stage": c, "enterprise": e, "recency": r,
                    "rationale": "Model view based on the listed evidence."}
    return {"saas_fit": 0, "compliance_trigger": 0, "stage": 5, "enterprise": 0, "recency": 0,
            "rationale": "insufficient evidence"}


# ------------------------------------------------------------------ search (grounded) + structuring
SEARCH = {
    ("Acme Thin", "recent news"): (
        "Acme Thin announced a Series A round in September 2026 to expand its invoice automation product into "
        "enterprise finance teams.",
        [Citation(0, "https://news.example.com/acme-thin-series-a", "news.example.com",
                  "https://news.example.com/acme-thin-series-a")]),
}


def fake_grounded(prompt: str):
    for (company, focus), (text, cites) in SEARCH.items():
        if company in prompt and focus in prompt.lower():
            return text, cites, [{"text": text, "citations": [0]}]
    return "No relevant, citable results were found.", [], []


def _ids(user: str, *keywords: str) -> list[str]:
    out = []
    for line in user.splitlines():
        m = re.match(r"- (E\d+) ", line.strip())
        if m and all(k.lower() in line.lower() for k in keywords):
            out.append(m.group(1))
    return out


def fake_pain(system: str, user: str) -> dict:
    if "Convert search findings" in system:
        if "Acme Thin" in user and "Series A" in user:
            return {"items": [{"kind": "funding", "statement": "Acme Thin announced a Series A round to expand into "
                                                               "enterprise finance teams",
                               "source_index": 0, "event_date": "2026-09", "evidence_quality": "MEDIUM",
                               "trigger_type": None, "funding_stage": "series_a"}]}
        return {"items": []}
    if "Acme Thin" in user:
        hire = _ids(user, "Head of Compliance")[:1]
        news = _ids(user, "Series A round")[:1]
        pps = []
        if hire:
            pps.append({"evidence_id": hire[0],
                        "hypothesis": "Until the new Head of Compliance is in place, engineering is probably carrying "
                                      "SOC 2 evidence collection and security questionnaires by hand.",
                        "why_it_matters_to_this_role": "A Head of Engineering loses roadmap time to audit prep.",
                        "confidence": "MEDIUM"})
        if news:
            pps.append({"evidence_id": news[0],
                        "hypothesis": "Moving upmarket after the Series A usually brings longer security reviews from "
                                      "enterprise buyers.",
                        "why_it_matters_to_this_role": "Security reviews pull senior engineers into sales cycles.",
                        "confidence": "HIGH"})     # capped to MEDIUM by code (evidence is MEDIUM)
        return {"pain_points": pps}
    if "Ledgerly" in user:
        audit = _ids(user, "SOC 2 Type II audit")[:1]
        ent = _ids(user, "Northwind")[:1]
        return {"pain_points": [
            {"evidence_id": audit[0], "hypothesis": "With a first SOC 2 Type II audit planned for Q1 2027, the team "
                                                    "may be collecting evidence and screenshots manually.",
             "why_it_matters_to_this_role": "A CTO owns the controls and the engineering time audits consume.",
             "confidence": "HIGH"},
            {"evidence_id": ent[0], "hypothesis": "Enterprise buyers like Northwind often send security "
                                                  "questionnaires that land on engineering.",
             "why_it_matters_to_this_role": "Questionnaires slow deals and interrupt engineering work.",
             "confidence": "MEDIUM"}]}
    return {"pain_points": []}


def fake_draft(system: str, user: str) -> dict:
    if "Acme Thin" in user:
        hire = _ids(user, "Head of Compliance")[0]
        news = (_ids(user, "Series A round") or [hire])[0]
        return {
            "subject": "Acme Thin's first SOC 2 audit",
            "recipient_name": "Daniel",
            "opening_hook": "Acme Thin posted a Head of Compliance role on 20 October to lead its first SOC 2 Type II audit.",
            "hook_evidence_id": hire,
            "pain_point_1": "Engineering is probably carrying evidence collection until that hire lands.",
            "pain_point_2": "Enterprise buyers after a Series A often bring heavier security reviews.",
            "email_body": (
                "Hi Daniel,\n\nI noticed Acme Thin posted a Head of Compliance role on 20 October, with a remit that "
                "includes leading your first SOC 2 Type II audit.\n\nWhen a team hires for that while moving into "
                "enterprise accounts, engineering often ends up pulling evidence and answering security "
                "questionnaires by hand. That might already be landing on your desk.\n\nSprinto automates the SOC 2 "
                "and ISO 27001 groundwork: control monitoring, evidence collection and auditor-ready reports, so a "
                "new compliance lead starts with a system rather than a spreadsheet.\n\nWould a 15-minute chat next "
                "week be useful to compare notes on how you are planning the audit?\n\nBest,\nVikram"),
            "personalisation_evidence": [
                {"claim": "Head of Compliance role posted 20 Oct 2026 to lead the first SOC 2 Type II audit",
                 "evidence_id": hire},
                {"claim": "Moving into enterprise accounts after the Series A", "evidence_id": news}],
            "cta": "Would a 15-minute chat next week be useful?"}
    if "Ledgerly" in user:
        audit = _ids(user, "SOC 2 Type II audit")[0]
        ent = _ids(user, "Northwind")[0]
        return {
            "subject": "Your Q1 SOC 2 audit at Ledgerly",
            "recipient_name": "Arjun",
            "opening_hook": "Ledgerly's site says the SOC 2 Type II audit is scheduled for the first quarter of 2027.",
            "hook_evidence_id": audit,
            "pain_point_1": "Evidence collection before a first audit often eats engineering time.",
            "pain_point_2": "Enterprise buyers like Northwind may add security questionnaires on top.",
            "email_body": (
                "Hi Arjun,\n\nLedgerly's site says your SOC 2 Type II audit is scheduled for the first quarter of "
                "2027, after enterprise prospects asked for the report.\n\nIn the months before a first audit, "
                "engineering teams often lose real time to evidence screenshots, access reviews and questionnaires "
                "from buyers like Northwind Logistics. That could be a lot to carry alongside the enterprise "
                "push.\n\nSprinto automates SOC 2 and ISO 27001 readiness: it monitors controls continuously and "
                "collects evidence for the auditor, so your engineers can stay on product work.\n\nWould a "
                "15-minute chat before the audit window help?\n\nBest,\nVikram"),
            "personalisation_evidence": [
                {"claim": "SOC 2 Type II audit scheduled for Q1 2027", "evidence_id": audit},
                {"claim": "Enterprise customer Northwind Logistics", "evidence_id": ent}],
            "cta": "Would a 15-minute chat before the audit window help?"}
    return {"subject": "Hello", "recipient_name": "there", "opening_hook": "Hi", "hook_evidence_id": "E0",
            "pain_point_1": "", "email_body": "Hi there", "personalisation_evidence": [], "cta": ""}


HANDLERS = {"extract": fake_extract, "qualify": fake_qualify, "pain": fake_pain, "draft": fake_draft}
