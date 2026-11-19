# Sprinto Cold-Outreach Pipeline

Evidence-first outbound pipeline for Sprinto (MESA Cohort 4, Term 2, Build 2).
**Input:** a list of company domains. **Output:** for each QUALIFIED company, a personalised email to a named
decision-maker, delivered **only to `DEMO_RECIPIENT_EMAIL`**, plus an audit trail that defends every decision and a
review console for the Growth Lead.

**Live app:** https://sprinto-outreach-nu.vercel.app · **Repo:** https://github.com/Vikram008-stack/VIK-SPRINTO

> Ground rule: every answer must be defensible. Code decides sufficiency; the model judges. Missing = null.

---

## Web app (live link)

`public/` (UI) + `api/index.py` (FastAPI, Vercel serverless). Paste domains -> each company runs through the full
pipeline in its own request (max 10 per batch, 2 in parallel) -> results land in the review desk as they finish.
Buttons: **Research these companies**, **Load example**, **Upload .txt/.csv**, **Run offline demo** (zero API calls),
**Download results (JSON)**, **Download summary (CSV)**, and **Send to my demo inbox** on drafts that pass guardrails.

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | which services are configured (booleans only), models, limits |
| `POST /api/parse` `{text}` | normalise + dedupe + validate input (empty, invalid, duplicate, too many, too large) |
| `POST /api/process` `{domain}` | full pipeline for one company (dry run), returns the outcome JSON |
| `POST /api/send` `{outcome}` | re-runs guardrails, sends ONLY to `DEMO_RECIPIENT_EMAIL` (10/hour cap) |
| `POST /api/demo` | offline demo world |

Vercel env vars: `GEMINI_API_KEY`, `SCRAPINGBEE_API_KEY`, `HUNTER_API_KEY`, `DEMO_RECIPIENT_EMAIL`,
`SPRINTO_HUNTER_LIVE=1` (live Hunter, guarded by Hunter's own account counter, hard stop 20), and optionally
`GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` (from `python -m src.cli gmail-auth`).
Run locally: `.venv\Scripts\python -m uvicorn api.index:app --port 8766` and open http://127.0.0.1:8766.

## Follow-up agent: the state object at every checkpoint

After the cold email, a second agent (`src/followup/workflow.py`, `POST /api/followup`) handles the reply. The SAME
six-field state object is snapshotted at every checkpoint, and the UI shows it as a table (changed cells in green):

| # | Checkpoint | Tool | reply_status | calendar_checked | times_proposed | reply_round | meeting_status | crm_logged |
|---|---|---|---|---|---|---|---|---|
| 1 | After Send Email | Seq 1, Tool 1 `send_email` | no_reply | false | none | 0 | unscheduled | false |
| 2 | After prospect replies "Interested" | branch, no tool call | **interested** | false | none | 0 | unscheduled | false |
| 3 | After Check Calendar | Seq 2, Tool 2 `check_calendar` | interested | **true** | none | 0 | unscheduled | false |
| 4 | After Send Email, proposing times | Tool 3 `send_email` | interested | true | **[2 slots]** | 0 | **pending_confirmation** | false |
| 5 | If prospect counter-proposes | loop back to Check Calendar | interested | **false** | [2 slots] | **1** | pending_confirmation | false |
| 6 | After Book Meeting | Tool 4 `book_meeting` | interested | true | [2 slots] | 1 | **confirmed** | false |
| 7 | After Update CRM | Tool 5 `update_crm` | interested | true | [2 slots] | 1 | confirmed | **true** |

- Scenarios: interested and accepts (skips step 5), counter-proposes (loop), not interested (stop, close in CRM),
  no reply (log a nudge). Or paste a real reply: a deterministic rule-based classifier picks the branch and parses a
  proposed time ("Can we do Friday 3pm instead?").
- The counter-proposal loop is capped at 2 rounds; beyond that `meeting_status = needs_human`. Ambiguous replies go
  to a human; the agent never guesses.
- Calendar and CRM are **demo systems** (deterministic, clearly labelled): no real calendar or CRM is connected in
  this build. Proposal and confirmation emails go only to the demo inbox.

## Quick start (Windows)

```
cd sprinto-outreach
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest                     # 148 offline tests, zero API calls
.venv\Scripts\python -m src.cli demo               # FULL pipeline offline (fixtures), builds the console
start reports\console.html                          # open the review console
```

For a live run, copy `.env.example` to `.env` and fill in the keys (never commit `.env`, never paste keys in chat):

```
.venv\Scripts\python -m src.cli check                               # shows which keys are set (never values)
.venv\Scripts\python -m src.cli scrape-demo chargebee.com thoughtworks.com cal.com
.venv\Scripts\python -m src.cli run --domains eval\domains.txt      # dry run (default)
.venv\Scripts\python -m src.cli run --domains eval\domains.txt --send   # sends ONLY to DEMO_RECIPIENT_EMAIL
.venv\Scripts\python -m src.cli eval                                # expected vs actual + kill criteria
```

| Key | Needed for |
|---|---|
| `GEMINI_API_KEY` | extraction, qualification, pain points, drafting, grounded search (paid tier needed for Gemini 3 grounding) |
| `SCRAPINGBEE_API_KEY` | scraping (or `--transport direct` for a free dev run: plain HTTP, robots still enforced) |
| `HUNTER_API_KEY` | live contacts, only with `--live-hunter` (default is mock fixtures) |
| `GMAIL_CLIENT_SECRET_FILE` | `--send` (Google Cloud OAuth "Desktop app" client, `gmail.send` scope, you as test user) |
| `DEMO_RECIPIENT_EMAIL` | `--send` (the ONLY address this build can send to) |

## Commands

| Command | What it does |
|---|---|
| `run --domains F [--dry-run/--send] [--refresh] [--limit N] [--live-hunter] [--transport direct]` | Full pipeline |
| `demo` | Full pipeline offline: fictional fixture sites, recorded model output, mock Hunter, dry run |
| `eval [--report-only]` | Runs `eval/domains.txt`, compares to `eval/expected.yaml`, accuracy + kill-criterion warnings, defence sheets |
| `console [--demo]` | Rebuilds `reports/console.html` from stored outcomes |
| `scrape-demo [domains...]` | Raw vs cleaned token counts per homepage (no LLM) |
| `check`, `init-db`, `quota`, `domains FILE`, `purge-contacts` | Housekeeping |

There is deliberately **no flag to set a recipient**.

---

## Architecture

```
domains.txt -> [0] normalise + dedupe (www., case, paths)           duplicates -> DUPLICATE (duplicate_input)
  -> [1] scrape: robots.txt (RFC 9309) -> cache (7d) -> ScrapingBee plain -> JS only if <200 words / empty shell
         failure taxonomy (bot_blocked, captcha, login_wall, empty_shell, geo_blocked, timeout, dns_error,
         robots_disallowed, thin_content, parked, domain_mismatch...) - one reason per failure, never silent
  -> [1b] clean: strip nav/header/footer/cookies/scripts/hidden, dedupe, keep headings/key terms/job titles,
         harvest footer cert badges, cap ~2k tokens, every chunk keeps source_url + fetched_at
  -> [2] extract (Gemini flash-lite, JSON schema)  ->  PYTHON VERIFIES: cited URL must be a page we read,
         quoted snippet must appear on that page, enums/dates/numbers parsed; otherwise dropped + logged
  -> STAGE GATE (Python): hard disqualifier (IT services, non-software, closed, parked) -> REJECTED, stop
  -> [3] AGENT LOOP (state machine, max 2 extra rounds, max 5 fetches, max 1 search):
         sufficiency.py (pure Python) lists unmet Evidence-Map rows -> fetch ONLY the mapped page(s)
         -> re-extract affected fields -> merge with provenance (+ conflicts) -> re-score in Python
         stops early when the decision cannot change; at the cap -> designed handoff with unknowns
  -> (QUALIFIED only)
  -> [4] Hunter Domain Search (cached forever, quota guard 20/25, mock by default) -> Python ranker
         (roles.yaml tiers, penalties, confidence/verification/seniority tie-breaks) -> manual_contacts fallback
  -> [5] evidence = sourced signals + grounded search (2-call pattern: grounded prose -> structured items that may
         only reference numbered sources) -> <= 2 pain-point HYPOTHESES, confidence capped by evidence quality
  -> [6] draft (Gemini, evidence referenced by id) -> guardrails.py -> regenerate once -> DRAFT_NEEDS_HUMAN
  -> [7] Gmail (gmail.send) -> To = DEMO_RECIPIENT_EMAIL only; real contact appears only in a header line
  -> SQLite audit (18 tables) + per-company defensibility report + run summary + review console
```

| Folder | Responsibility |
|---|---|
| `config/` | `pipeline.yaml` (caps, models, prices, modes), `icp_rubric.yaml` (rubric v1 + Evidence Map), `roles.yaml`, `manual_contacts.yaml` |
| `src/scrape/` | transport, robots, cache, failure classifier, cleaner, link discovery, fetcher |
| `src/extract/` | LLM schema, extraction with provenance checks, merge |
| `src/qualify/` | scorer (rubric + decision rules), sufficiency (Evidence Map), loop (state machine) |
| `src/contacts/` | Hunter client (mock/live, cache, quota), ranker |
| `src/pain/` | search providers (Gemini grounded / Serper), research structuring, pain points |
| `src/mail/` | drafter, guardrails, Gmail sender |
| `src/report/` | defensibility markdown, run summary, eval table, defence sheets, review console |
| `src/llm/` | Gemini client (cache, retries, schema retry, cost meter), prompts (versioned) |

## The review console (`reports/console.html`)

One self-contained HTML file (no server, no external requests) built for the person who has to defend each
decision. Per company it shows, in this order: the verdict in plain words and the next step; why (score bars, each
point linked to its source); the evidence (quality, *event* date vs *seen* date, quoted snippet); how the loop
researched (what was missing, what it fetched, what got answered); who we'd write to and why (plus everyone skipped
and why); the email as a letter with every claim mapped to a source and the guardrail result; and reason codes in
plain language. Filters, search, keyboard navigation (`j`/`k`, `/`), light/dark, mobile, print.

## Rubric v1 and decisions

Score (100) = SaaS fit 25 + compliance trigger 40 (strongest verified only) + stage 15 + enterprise 10 + recency 10
(from `event_date`, never crawl date). Every point needs a `source_url`; points without one are removed by code.

- **QUALIFIED** = score >= 75 AND SaaS >= 15 AND trigger >= 20 AND trigger evidence >= MEDIUM AND no hard
  disqualifier AND no context needing human research (acquired, multiple entities, conflicts, stealth, blocked site).
- **NEEDS_RESEARCH** = 50-74, or trigger plausible but unverified, or already certified (policy `research`), or the
  forcing context above. A 90-point SaaS with no compliance evidence is never QUALIFIED.
- **REJECTED** = hard disqualifier, or score < 50 after research and not a plausible B2B SaaS fit.
- **INSUFFICIENT_DATA** = loop cap reached without knowing what the company sells, or budget exceeded. Not a rejection.

**`already_certified_policy`** (`config/pipeline.yaml`): `research` (default, rubric v1: check renewals / new
frameworks; a dated target for a framework they don't hold yet can still qualify) or `disqualify` (the stricter
"disqualify and stop" variant from the L4 Evidence Map). One-line switch.

Other decisions:
- Model tiering: `gemini-3.1-flash-lite` ($0.25/$1.50 per 1M) for extraction; `gemini-3.6-flash` ($0.75/$3.75,
  doubles on 2027-01-01) for qualification judgement, pain points, drafting and grounded search. Prices checked on
  2026-11-14; unknown prices are recorded as null, never guessed.
- The model *proposes* component scores; Python's score is final and any difference is logged (`score_diff`).
- Hiring signals are scored by code from verified job titles: a compliance/GRC/security *leader* = 25; a security
  engineer = 10 (an IC hire does not mean an audit is imminent).
- Evidence-Map "already certified?" row is settled when a cert is named, the security page was read, or the homepage
  links no security/trust page (then treated as not certified, flagged).
- Month-only dates map to the 1st of the month (never makes a signal look newer). Undated third-party claims are
  never STRONG.
- Hunter: mock by default; live needs `--live-hunter`; counter reserved atomically before each call; hard stop 20
  of 25; whole response cached so re-ranking is free; verifier only on the chosen contact.
- Dry run without `DEMO_RECIPIENT_EMAIL` addresses the `.eml` to an undeliverable `@example.invalid` placeholder.
- Re-runs skip completed companies (`--refresh` to redo); caches make an unchanged re-run cost zero API calls.

## Kill criteria and targets

| Metric | Target | Kill |
|---|---|---|
| Time per company | < 2 min | - |
| Growth Lead rewrites | < 20% of drafts | > 50% |
| Qualification accuracy (eval) | - | < 60% |

`eval` prints accuracy and warns loudly below 60%. Rewrite rate is a human judgement: fill
`eval/rewrite_judgements.yaml` (see the `.example`) and run `eval --report-only`.

## Evaluation

`eval/domains.txt` holds the Addendum D list (chargebee, thoughtworks, tcs, duolingo, getlago, cal, twenty,
documenso, `<USER_THIN_PICK>`, and `[www.cal.com]` as the dedupe test). `eval/expected.yaml` contains **draft**
predictions with `frozen: false`; review and freeze it before the first eval run.

## Known limitations

- Live run 2026-11-18 on the eval list (free-tier Gemini key): all 9 inputs got the expected verdict (draft
  expectations, not yet frozen), but no company QUALIFIED, so steps 4-7 ran live only up to the gate; the offline demo
  and 148 tests exercise them with fixtures. Overloaded models (gemini-3.6-flash 503/504) stalled one company for 3
  minutes, fixed with a 25 s timeout, one retry and a fallback model.
- The likely eval list produces few QUALIFIED companies (several are already certified -> NEEDS_RESEARCH under
  policy `research`); include at least one likely fit as the thin pick to exercise steps 4-7 live.
- Gemini 3 grounded search needs a paid-tier key; without it search degrades to company pages (`search_unavailable`).
- Token counts in the cleaner are chars/4 estimates; cost uses real usage metadata from the API.
- Single-fetch only: no crawling, logins, CAPTCHA bypass or social platforms, by design. JS-heavy careers boards
  cost 5 ScrapingBee credits per page.
- ScrapingBee $/credit in config is an estimate for the Freelance plan; set it to your plan.
- Concurrency is a 3-thread pool with SQLite WAL; fine for tens of domains, not thousands.
