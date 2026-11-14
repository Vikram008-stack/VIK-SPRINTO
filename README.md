# Sprinto Cold-Outreach Pipeline

Evidence-first outbound pipeline for Sprinto (MESA Cohort 4, Term 2, Build 2).
Domains in → for each QUALIFIED company, a personalised email to a named decision-maker, sent **only to
`DEMO_RECIPIENT_EMAIL`**, plus an audit trail that defends every decision.

> Full README (architecture diagram, decisions, limitations, kill criteria) lands at milestone 10.

## Setup (Windows)

```
cd sprinto-outreach
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then fill in keys; never commit .env
.venv\Scripts\python -m src.cli check
.venv\Scripts\python -m src.cli init-db
.venv\Scripts\python -m pytest
```

## Commands (so far)

| Command | What it does |
|---|---|
| `python -m src.cli check` | Validates config; shows which secrets are set (never their values) |
| `python -m src.cli init-db` | Creates `data/outreach.db` with all audit tables |
| `python -m src.cli quota` | Hunter usage this month vs the hard stop (20 of 25) |
| `python -m src.cli domains FILE` | Shows normalisation + dedupe of an input list (no API calls) |
| `python -m src.cli purge-contacts` | Deletes stored contact data |

## Key config switches

- `qualification.already_certified_policy`: `research` (default, rubric v1) or `disqualify`
  (the stricter "disqualify and stop" variant from the L4 Evidence Map). One-line change in `config/pipeline.yaml`.
- `hunter.mode`: `mock` (default, replays fixtures) or `live` (also needs `--live-hunter`).
- `email.mode`: `dry_run` (default) or `send` (also needs `--send`; still only ever sends to `DEMO_RECIPIENT_EMAIL`).
