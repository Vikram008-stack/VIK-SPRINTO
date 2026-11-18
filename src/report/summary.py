"""Run summary, evaluation table, and presentation defence sheets."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

import yaml

from src.report.defensibility import render_company

HUMAN_REVIEW = {"NEEDS_RESEARCH", "INSUFFICIENT_DATA", "NO_CONTACT_FOUND", "DRAFT_NEEDS_HUMAN", "SEND_FAILED",
                "FAILED"}
QUAL_OF = {"DRAFTED": "QUALIFIED", "SENT": "QUALIFIED", "NO_CONTACT_FOUND": "QUALIFIED",
           "DRAFT_NEEDS_HUMAN": "QUALIFIED", "SEND_FAILED": "QUALIFIED"}


def qual_status(o: dict) -> str:
    """The qualification verdict, independent of downstream steps (a DRAFTED company was QUALIFIED)."""
    return QUAL_OF.get(o["status"], o["status"])


def run_summary(outcomes: list[dict], hunter_usage: dict, hard_stop: int, target_s: int = 120) -> str:
    counts = Counter(o["status"] for o in outcomes)
    timed = [o["seconds"] for o in outcomes if o.get("seconds")]
    costs = [(o.get("cost") or {}).get("cost_usd") or 0 for o in outcomes]
    unknown = sum((o.get("cost") or {}).get("unknown_cost_calls", 0) for o in outcomes)
    credits = sum((o.get("cost") or {}).get("scrape_credits", 0) for o in outcomes)
    avg = sum(timed) / len(timed) if timed else 0
    lines = ["# Run summary", "", "| Status | Count |", "|---|---:|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(counts.items())]
    lines += ["", f"- Companies: {len(outcomes)}",
              f"- Average time per processed company: {avg:.1f} s (target < {target_s} s: "
              f"{'met' if avg < target_s else 'MISSED'})",
              f"- Total LLM/scrape cost: ${sum(costs):.4f}" + (f" (+{unknown} calls with unknown price)" if unknown
                                                               else ""),
              f"- ScrapingBee credits: {credits}",
              f"- Hunter searches this month: {hunter_usage.get('searches_used', 0)}/{hard_stop}", "",
              "## Human review queue", ""]
    review = [o for o in outcomes if o["status"] in HUMAN_REVIEW]
    lines += [f"- **{o['domain']}** {o['status']}: {o.get('status_reason') or ''} -> {o.get('next_action', '')}"
              for o in review] or ["- none"]
    return "\n".join(lines) + "\n"


def load_expected(path: Path) -> tuple[dict, bool]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("companies", {}), bool(data.get("frozen"))


def eval_table(outcomes: list[dict], expected: dict, frozen: bool,
               rewrites: Optional[dict] = None) -> tuple[str, float]:
    rows, hits, n = [], 0, 0
    for o in outcomes:
        exp = expected.get(o["domain"]) if o["status"] != "DUPLICATE" or o["domain"] not in expected else None
        key = o["domain"] if o["status"] != "DUPLICATE" else f"{o['domain']} (dup: {o.get('input_raw')})"
        exp_status = (exp or {}).get("status") if exp else ("DUPLICATE" if o["status"] == "DUPLICATE" else None)
        actual = qual_status(o)
        match = None
        if exp_status:
            allowed = {s.strip() for s in str(exp_status).split("|")}
            match = actual in allowed
            n += 1
            hits += int(match)
        q = o.get("qualification") or {}
        rounds = max(0, len(q.get("rounds") or []) - 1)
        fails = ", ".join(sorted({f["reason"] for f in o.get("failures") or []})) or "-"
        rows.append(f"| {key} | {exp_status or '?'} | {actual} ({o['status']}) | "
                    f"{'yes' if match else ('no' if match is False else '-')} | {q.get('score', '-')} | {rounds} | "
                    f"{len(o.get('pages') or [])} | ${((o.get('cost') or {}).get('cost_usd') or 0):.4f} | "
                    f"{o.get('seconds', 0)} | {fails} |")
    acc = hits / n if n else 0.0
    out = ["# Evaluation", "",
           ("" if frozen else "> WARNING: eval/expected.yaml is not frozen (`frozen: true`). Freeze it BEFORE the "
                              "first evaluation run or the accuracy number is not defensible.\n"),
           "| Domain | Expected | Actual | Match | Score | Loop rounds | Fetches | Cost | Seconds | Failure reasons |",
           "|---|---|---|---|---:|---:|---:|---:|---:|---|", *rows, "",
           f"**Accuracy: {hits}/{n} = {acc:.0%}**"]
    if n and acc < 0.60:
        out.append("\n**KILL CRITERION: accuracy is below 60%. Do not roll out; fix the rubric/extraction first.**")
    if rewrites:
        judged = [v for v in rewrites.values() if isinstance(v, bool)]
        if judged:
            rate = sum(judged) / len(judged)
            out.append(f"\nGrowth Lead rewrite rate (manual judgements): {sum(judged)}/{len(judged)} = {rate:.0%}"
                       f" (target < 20%, kill > 50%)")
            if rate > 0.5:
                out.append("**KILL CRITERION: more than 50% of drafts need rewriting.**")
    else:
        out.append("\nRewrite rate: not judged yet. Fill eval/rewrite_judgements.yaml (domain: true if the Growth "
                   "Lead would rewrite the draft) and re-run `eval --report-only`.")
    return "\n".join(out) + "\n", acc


def defence_sheets(outcomes: list[dict]) -> str:
    """One page each for a QUALIFIED, a REJECTED, and a NEEDS_RESEARCH/INSUFFICIENT_DATA company."""
    picks = []
    for wanted in (("QUALIFIED",), ("REJECTED",), ("NEEDS_RESEARCH", "INSUFFICIENT_DATA")):
        found = next((o for o in outcomes if qual_status(o) in wanted and o["status"] != "DUPLICATE"), None)
        picks.append((wanted, found))
    parts = ["# Defence sheets", ""]
    for wanted, o in picks:
        if o is None:
            parts += [f"## {' / '.join(wanted)}: no company in this run had this verdict", ""]
            continue
        parts += [f"---\n\n## {' / '.join(wanted)} example", "", render_company(o)]
    return "\n".join(parts)
