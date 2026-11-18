# Submission: Sprinto Cold-Outreach Pipeline (Build 2)

| Item | Link / location |
|---|---|
| Live app (Vercel) | https://sprinto-outreach-nu.vercel.app |
| GitHub repo (public) | `https://github.com/<your-username>/sprinto-outreach` (fill in after pushing) |
| Demo video | (your recording link) |

The live deployment is built from the same commits as the repo (`git archive HEAD` of `main`).

## Input format

Company domains, one per line (also accepts comma-separated text, a `.txt` or `.csv` upload, full URLs, `www.`
prefixes, and a CSV header row such as `domain`). Up to 10 unique domains per batch.

```
chargebee.com
https://www.thoughtworks.com/
documenso.com
```

## What the app produces (per company)

- **Verdict**: QUALIFIED (draft ready) / NEEDS_RESEARCH / REJECTED / INSUFFICIENT_DATA / DUPLICATE / INVALID, with the
  reason and next step.
- **Score breakdown** (rubric v1, 100 points) with every point linked to its source URL; the model's suggested score is
  shown but the rule-based score is final.
- **Evidence** with quality, the date it happened vs the date we saw it, and the quoted snippet.
- **Research trail**: what was missing, which pages the agent loop fetched (max 2 rounds, 5 fetches), what got answered.
- For QUALIFIED companies: **decision-maker** (Hunter + deterministic ranker, with runner-up and everyone skipped),
  **two evidence-linked pain-point hypotheses**, and a **<120-word email** that passed the guardrails, with every
  claim mapped to a source. "Send to my demo inbox" delivers it only to the configured demo address.
- **Downloads**: full results (JSON) and a summary (CSV).

## Exceptions handled (test these)

| Input / situation | What you see |
|---|---|
| Empty input | "Paste at least one company domain…" (nothing runs) |
| `not a domain`, `localhost`, `acme` | Row marked **Invalid input** with the reason; others continue |
| `www.cal.com` + `cal.com` | Researched once; the repeat is marked **Duplicate input** |
| More than 10 domains | First 10 processed, message says how many were left out |
| Huge paste (>20k chars) / file >200 KB | Clear "too large" message |
| Domain that does not resolve | **Needs a human look**: "The domain did not resolve" (`dns_error`) |
| Site blocks bots / CAPTCHA / login wall / empty JS shell | **Needs a human look** with the exact reason; never bypassed |
| IT consultancy / non-software | **Not a fit**: hard disqualifier, stops after the homepage (no extra spend) |
| Already SOC 2 / ISO certified | **Needs a human look**: check renewal / new frameworks (policy `research`) |
| AI model overloaded / quota | Retries once, then falls back to a second model; if both fail, a clear reason code |
| Web search quota (free tier) | Continues with company pages only, flagged `search_unavailable` |
| Hunter quota near limit | Refuses live calls at 20 used (Hunter's own counter); falls back / NO_CONTACT_FOUND |
| Gmail not connected | Draft kept, "Gmail sending is not connected…", nothing sent |
| Unknown API route / bad JSON | JSON error, never a crash page |

## Demo video script (about 3 minutes)

1. **Problem (20 s)**: "Sprinto's SDRs spend 15-20 minutes per company. This desk does it in under two minutes and can
   defend every decision."
2. **Offline demo (40 s)**: click *Run offline demo*. Open **Ledgerly** (90/100): show the score bars and click a
   source link; show *How we researched*; show *Who we'd write to* (the CTO was chosen, the Head of Security was skipped
   for low Hunter confidence, sales/marketing penalised); read the email and the "passed all checks" badge.
3. **Agent loop (20 s)**: open **Acme Thin**: the homepage was thin, so the loop fetched only `/careers`, found a Head of
   Compliance role and the Series A, and the score went 34 -> 79.
4. **Live run (60 s)**: paste `thoughtworks.com`, `documenso.com`, `www.cal.com`, `cal.com`, `not a domain`.
   Show the invalid and duplicate rows appear instantly; thoughtworks is **Not a fit** (IT services) in seconds;
   documenso and cal.com go to **Needs a human look** (no verified trigger / already certified) with the open questions.
5. **Safety + defensibility (30 s)**: emails only reach the demo inbox; every claim has a source; missing facts stay
   empty and go to a human; the model never overrides the rule-based score.
6. **Close (10 s)**: repo link, tests (`pytest`: 148 offline tests), README for architecture and kill criteria.

## Local commands

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python -m uvicorn api.index:app --port 8766     # web app at http://127.0.0.1:8766
.venv\Scripts\python -m src.cli demo                          # offline demo + reports/console.html
.venv\Scripts\python -m src.cli run --domains eval\domains.txt
.venv\Scripts\python -m src.cli eval --report-only
```
