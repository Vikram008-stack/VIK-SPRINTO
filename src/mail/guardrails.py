"""Email quality guardrails (enforced in code; regenerate once, then DRAFT_NEEDS_HUMAN)."""
from __future__ import annotations

import re
from typing import Optional

from src.store.models import Contact, EmailDraft

PLACEHOLDER_RE = re.compile(r"\[[^\]]{1,40}\]|\{[^}]{1,40}\}|<[A-Z_ ]{2,30}>|\bTODO\b|\bXX+\b|\bTBD\b|lorem ipsum",
                            re.I)
BUZZWORDS = ["synergy", "synergies", "leverage", "cutting-edge", "cutting edge", "game-changer", "game changer",
             "revolutionize", "revolutionise", "seamless", "seamlessly", "robust", "unlock", "empower",
             "best-in-class", "world-class", "supercharge", "next-generation", "holistic", "paradigm",
             "in today's fast-paced", "skyrocket", "turnkey", "disrupt"]
URGENCY_RE = re.compile(r"\b(act now|limited time|urgent|last chance|before it'?s too late|asap|right away|"
                        r"don'?t miss|only \d+ (spots|slots) left|expires? (today|soon))\b", re.I)
FAMILIARITY_RE = re.compile(r"\b(as we discussed|great (to|catching) (re)?connect|following up on our|as you know|"
                            r"i know you('re| are)|you('re| are) (clearly )?struggling|your team is struggling|"
                            r"remember me|long time no)\b", re.I)
STAT_RE = re.compile(r"\b\d+(\.\d+)?\s?%|\b\d+x\b|\b\d+ times (faster|cheaper)\b|\bsave \$?\d+", re.I)
SPRINTO_CLAIM_RE = re.compile(r"\b(trusted by|used by \d+|customers like|companies like [A-Z]|guarantee[sd]?|"
                              r"in (just )?\d+ (days|weeks|hours))\b", re.I)
HEDGE_RE = re.compile(r"\b(often|sounds like|might|may|usually|typically|guess|wonder|if|perhaps|could|likely|"
                      r"tends? to|in my experience|curious)\b", re.I)
GENERIC_HOOK_RE = re.compile(r"(saw|noticed) (that )?you('re| are) a saas|came across your (company|website)|"
                             r"i hope this (email|message) finds you", re.I)
CTA_RE = re.compile(r"\b(\d+[- ]minute|quick (chat|call)|call next week|worth a (chat|call)|open to a|"
                    r"grab (a|some) time|book a (call|demo)|hop on a call)\b", re.I)


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text or ""))


def check_draft(d: EmailDraft, contact: Contact, allowed_urls: set[str], hook_url: Optional[str],
                max_words: int = 120, tolerance: float = 0.10, max_em_dashes: int = 0,
                hedging_required: bool = True) -> list[str]:
    errs: list[str] = []
    body = d.email_body or ""
    first = (contact.first_name or contact.name.split()[0]).strip()
    head = body.strip().split("\n", 1)[0]
    if first.lower() not in head.lower():
        errs.append(f"greeting must address the recipient by first name ({first}) in the first line")
    if (d.recipient_name or "").split()[0].lower() != first.lower():
        errs.append(f"recipient_name must be {first}")
    wc = word_count(body)
    if wc > int(max_words * (1 + tolerance)):
        errs.append(f"email_body has {wc} words; limit is about {max_words}")
    all_text = " ".join([d.subject, d.opening_hook, d.pain_point_1, d.pain_point_2 or "", body, d.cta])
    if PLACEHOLDER_RE.search(all_text):
        errs.append(f"placeholder found: {PLACEHOLDER_RE.search(all_text).group(0)!r}")
    if body.count("—") + body.count(" – ") > max_em_dashes:
        errs.append("em dashes are not allowed (AI cadence)")
    bz = [b for b in BUZZWORDS if re.search(rf"\b{re.escape(b)}\b", all_text, re.I)]
    if bz:
        errs.append(f"buzzwords: {', '.join(bz)}")
    if URGENCY_RE.search(all_text):
        errs.append(f"fake urgency: {URGENCY_RE.search(all_text).group(0)!r}")
    if FAMILIARITY_RE.search(all_text):
        errs.append(f"false familiarity / overclaiming: {FAMILIARITY_RE.search(all_text).group(0)!r}")
    if STAT_RE.search(all_text):
        errs.append(f"unverifiable statistic: {STAT_RE.search(all_text).group(0)!r}")
    if SPRINTO_CLAIM_RE.search(all_text):
        errs.append(f"unsupported Sprinto claim: {SPRINTO_CLAIM_RE.search(all_text).group(0)!r}")
    if GENERIC_HOOK_RE.search(d.opening_hook + " " + head):
        errs.append("opening hook is generic; it must be a specific, dated, sourced observation")
    if not hook_url:
        errs.append("opening hook is not linked to an evidence item")
    if not d.personalisation_evidence:
        errs.append("personalisation_evidence is empty: every company claim needs a source")
    for ev in d.personalisation_evidence:
        if ev.source_url not in allowed_urls:
            errs.append(f"claim cites a source we never collected: {ev.source_url}")
    if hedging_required and not HEDGE_RE.search(d.pain_point_1 + " " + (d.pain_point_2 or "") + " " + body):
        errs.append("pain points must be hedged as hypotheses (e.g. 'often', 'sounds like', 'might')")
    ctas = CTA_RE.findall(body)
    if len(ctas) == 0:
        errs.append("needs one soft call to action (e.g. a 15-minute chat)")
    elif len(ctas) > 1:
        errs.append("more than one call to action")
    if not d.subject.strip() or len(d.subject) > 70:
        errs.append("subject must be 1-70 characters")
    if d.subject.isupper() or re.match(r"^\s*(re|fwd?):", d.subject, re.I):
        errs.append("subject must not shout or fake a reply/forward")
    return errs
