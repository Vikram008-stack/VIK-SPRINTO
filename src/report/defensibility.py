"""Per-company defensibility report (Markdown), rendered from a CompanyOutcome JSON dict."""
from __future__ import annotations

from typing import Any


def _fmt_money(v: Any) -> str:
    return "n/a" if v is None else f"${v:.4f}"


def render_company(o: dict) -> str:
    q = o.get("qualification") or {}
    lines = [f"# {o.get('company_name') or o['domain']} ({o['domain']})", "",
             f"**Status:** {o['status']}  ", f"**Why:** {o.get('status_reason') or '-'}  ",
             f"**Next action:** {o.get('next_action') or '-'}  ",
             f"**Confidence (separate from score):** {o.get('confidence') or q.get('confidence') or '-'}  ",
             f"**Run:** {o.get('run_id')} | {o.get('seconds', 0)} s | cost {_fmt_money((o.get('cost') or {}).get('cost_usd'))}"
             f" | scrape credits {(o.get('cost') or {}).get('scrape_credits', 0)}", ""]

    if q:
        lines += ["## 1. Why this company qualified (or did not)", "",
                  f"Score **{q.get('score')}/100** (rubric {q.get('rubric_version')}). "
                  + (f"Model proposed {q.get('model_proposed_score')} (diff {q.get('score_diff'):+d}); Python's score "
                     f"is final." if q.get("model_proposed_score") is not None else ""), "",
                  "| Component | Points | Bucket | Reason | Evidence |", "|---|---:|---|---|---|"]
        for c in (q.get("breakdown") or {}).get("components", []):
            ev = "<br>".join(c.get("source_urls") or []) or "-"
            lines.append(f"| {c['component']} | {c['points']}/{c['max_points']} | {c['bucket']} | {c['reason']} | {ev} |")
        lines += ["", "**Decision reasons:** " + ("; ".join(q.get("reasons") or []) or "-")]
        if q.get("hard_disqualifier"):
            lines.append(f"**Hard disqualifier:** {q['hard_disqualifier']}")
        if q.get("handoff_note"):
            lines.append(f"**Handoff:** {q['handoff_note']}")
        lines.append("")

    facts = o.get("facts") or {}
    sigs = (facts.get("compliance_signals") or []) + (facts.get("enterprise_signals") or [])
    if sigs or facts.get("hiring_signals"):
        lines += ["### Evidence collected", "", "| Signal | Quality | Event date | Seen | Source |", "|---|---|---|---|---|"]
        for s in sigs:
            lines.append(f"| {s['signal'][:120]} | {s['evidence_quality']} | {s.get('event_date') or 'undated'} | "
                         f"{(s.get('observed_at') or '')[:10]} | {s['source_url']} |")
        for h in facts.get("hiring_signals") or []:
            lines.append(f"| Job: {h['title']} | MEDIUM | {h.get('event_date') or 'undated'} | - | {h['source_url']} |")
        lines.append("")
    if facts.get("certifications_claimed"):
        lines.append("**Certifications claimed:** " + ", ".join(facts["certifications_claimed"]))
    if facts.get("conflicts"):
        lines.append("**Conflicts:** " + "; ".join(facts["conflicts"]))

    lines += ["## 2. What was missing and what the loop fetched", ""]
    for r in q.get("rounds") or []:
        lines.append(f"- Round {r['round']}: missing before {r['missing_before'] or '-'} -> fetched "
                     f"{r['fetched_urls'] or '-'} -> missing after {r['missing_after'] or 'none'} "
                     f"(score {r.get('score')})" + (f" - {r['note']}" if r.get("note") else ""))
    for n in o.get("loop_notes") or []:
        lines.append(f"- note: {n}")
    if o.get("pages"):
        lines += ["", "| Page | Mode | OK | Reason | Raw tok | Clean tok | Credits | Cache |",
                  "|---|---|---|---|---:|---:|---:|---|"]
        for p in o["pages"]:
            lines.append(f"| {p['url']} | {p['mode']} | {'yes' if p['ok'] else 'no'} | {p.get('reason') or '-'} | "
                         f"{p.get('raw_tokens') or '-'} | {p.get('clean_tokens') or '-'} | {p.get('credits', 0)} | "
                         f"{'hit' if p.get('from_cache') else '-'} |")
    if o.get("extraction_dropped"):
        lines += ["", "**Model output rejected by provenance checks:**"] + [f"- {d}" for d in o["extraction_dropped"]]
    lines.append("")

    c = o.get("contact") or {}
    if c:
        lines += ["## 3. Why this person", ""]
        ch = c.get("chosen")
        if ch:
            lines.append(f"**{ch['name']}**, {ch.get('position') or '-'} ({ch.get('source')}, confidence "
                         f"{ch.get('confidence')}, verification {ch.get('verification_status') or 'not run'})  ")
        lines.append(c.get("why_this_person") or "-")
        if c.get("runner_up"):
            ru = c["runner_up"]
            lines.append(f"\nRunner-up: {ru['name']}, {ru.get('position')}")
        if o.get("contact_candidates"):
            lines += ["", "| Candidate | Title | Tier | Excluded because |", "|---|---|---|---|"]
            for k in o["contact_candidates"]:
                lines.append(f"| {k['name']} | {k.get('position') or '-'} | {k.get('tier') or '-'} | "
                             f"{k.get('excluded') or '-'} |")
        lines.append("")

    if o.get("pain_points") or o.get("draft"):
        lines += ["## 4. Why this email", ""]
        for i, p in enumerate(o.get("pain_points") or [], 1):
            lines.append(f"- Pain {i} ({p['confidence']}): {p['hypothesis']}  \n  Signal: {p['signal'][:160]} "
                         f"({p.get('event_date') or 'undated'}) - {p['source_url']}  \n  Why it matters to this role: "
                         f"{p['why_it_matters_to_this_role']}")
        if o.get("pain_flags"):
            lines.append("- flags: " + ", ".join(o["pain_flags"]))
        d = o.get("draft")
        if d:
            lines += ["", f"**Subject:** {d['subject']}", "", "```", d["email_body"], "```", "",
                      "| Claim in email | Source |", "|---|---|"]
            for ev in d.get("personalisation_evidence") or []:
                lines.append(f"| {ev['claim'][:120]} | {ev['source_url']} |")
        for a in o.get("draft_attempts") or []:
            lines.append(f"- draft attempt {a['attempt']}: {a['words']} words, "
                         + ("passed guardrails" if not a["errors"] else "failed: " + "; ".join(a["errors"])))
        lines.append("")

    s = o.get("send")
    if s:
        lines += ["## 5. Send", "", f"Status **{s['status']}** to `{s['to']}` (demo inbox only). "
                  f"{s.get('intended_recipient') or ''}" + (f" message_id `{s['message_id']}`" if s.get("message_id")
                                                             else ""), ""]
    if o.get("failures"):
        lines += ["## Failures / flags (reason codes)", ""] + \
            [f"- `{f['reason']}` at {f['step']}: {f.get('detail') or ''}" for f in o["failures"]]
    return "\n".join(lines) + "\n"
