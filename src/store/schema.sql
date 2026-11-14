-- Sprinto outreach audit store. Every decision must be reconstructible from these rows.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    command         TEXT NOT NULL,                 -- run | eval
    args_json       TEXT NOT NULL DEFAULT '{}',
    dry_run         INTEGER NOT NULL DEFAULT 1,
    rubric_version  TEXT NOT NULL,
    config_hash     TEXT NOT NULL,
    summary_json    TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    domain          TEXT PRIMARY KEY,              -- normalised (lowercase, no www., no path)
    input_raw       TEXT NOT NULL,
    first_run_id    TEXT REFERENCES runs(run_id),
    last_run_id     TEXT REFERENCES runs(run_id),
    status          TEXT NOT NULL DEFAULT 'PENDING',
    status_reason   TEXT,
    company_name    TEXT,
    final_domain    TEXT,                          -- after redirects
    completed_at    TEXT,
    seconds_spent   REAL,
    cost_usd        REAL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT REFERENCES runs(run_id),
    domain          TEXT NOT NULL,
    url             TEXT NOT NULL,
    final_url       TEXT,
    page_type       TEXT,
    mode            TEXT NOT NULL,                 -- plain | js | direct | fixture
    ok              INTEGER NOT NULL,
    status_code     INTEGER,
    failure_reason  TEXT,
    failure_detail  TEXT,
    fetched_at      TEXT NOT NULL,
    from_cache      INTEGER NOT NULL DEFAULT 0,
    raw_tokens      INTEGER,
    clean_tokens    INTEGER,
    text_hash       TEXT,
    credits         INTEGER NOT NULL DEFAULT 0,
    round           INTEGER NOT NULL DEFAULT 0     -- 0 = initial fetch, 1..2 = loop rounds
);
CREATE INDEX IF NOT EXISTS ix_pages_domain ON pages(domain);

-- HTTP response cache (raw HTML on disk, metadata here). TTL enforced in code.
CREATE TABLE IF NOT EXISTS http_cache (
    cache_key       TEXT PRIMARY KEY,              -- sha256(url + mode)
    url             TEXT NOT NULL,
    mode            TEXT NOT NULL,
    status_code     INTEGER,
    final_url       TEXT,
    body_path       TEXT,
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extractions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT REFERENCES runs(run_id),
    domain          TEXT NOT NULL,
    round           INTEGER NOT NULL DEFAULT 0,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    facts_json      TEXT NOT NULL,                 -- CompanyFacts
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT REFERENCES runs(run_id),
    domain          TEXT NOT NULL,
    kind            TEXT NOT NULL,                 -- compliance | enterprise | hiring | pain
    signal          TEXT NOT NULL,
    snippet         TEXT,
    source_url      TEXT NOT NULL CHECK (source_url LIKE 'http%'),
    source_origin   TEXT NOT NULL,                 -- fetched_page | grounding | fixture
    event_date      TEXT,                          -- NULL = undated (0 recency points)
    observed_at     TEXT NOT NULL,
    quality         TEXT NOT NULL,
    trigger_type    TEXT,
    points          INTEGER NOT NULL DEFAULT 0     -- points actually awarded by the Python scorer
);
CREATE INDEX IF NOT EXISTS ix_signals_domain ON signals(domain);

CREATE TABLE IF NOT EXISTS qualification_rounds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT REFERENCES runs(run_id),
    domain          TEXT NOT NULL,
    round           INTEGER NOT NULL,
    missing_before  TEXT NOT NULL,                 -- JSON list
    fetched_urls    TEXT NOT NULL,                 -- JSON list
    missing_after   TEXT NOT NULL,                 -- JSON list
    score           INTEGER,
    tokens          INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL,
    note            TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qualifications (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT REFERENCES runs(run_id),
    domain                TEXT NOT NULL,
    status                TEXT NOT NULL,
    score                 INTEGER NOT NULL,
    breakdown_json        TEXT NOT NULL,
    model_proposed_score  INTEGER,
    score_diff            INTEGER,
    hard_disqualifier     TEXT,
    reasons_json          TEXT NOT NULL,
    unknowns_json         TEXT NOT NULL,
    confidence            TEXT NOT NULL,
    handoff_note          TEXT,
    rubric_version        TEXT NOT NULL,
    prompt_version        TEXT,
    created_at            TEXT NOT NULL
);

-- Minimal business-contact data only; purge with `cli purge-contacts`.
CREATE TABLE IF NOT EXISTS contacts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT REFERENCES runs(run_id),
    domain                TEXT NOT NULL,
    role                  TEXT NOT NULL,           -- chosen | runner_up | candidate
    name                  TEXT NOT NULL,
    position              TEXT,
    seniority             TEXT,
    department            TEXT,
    email                 TEXT,
    confidence            INTEGER,
    verification_status   TEXT,
    source                TEXT NOT NULL,           -- hunter | apollo_manual | role_mailbox | fixture
    rank_score            REAL,
    why_this_person       TEXT,
    created_at            TEXT NOT NULL
);

-- Raw Hunter responses cached forever per domain (re-ranking costs zero).
CREATE TABLE IF NOT EXISTS hunter_cache (
    domain          TEXT NOT NULL,
    endpoint        TEXT NOT NULL,                 -- domain-search | email-verifier
    params_hash     TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (domain, endpoint, params_hash)
);

CREATE TABLE IF NOT EXISTS hunter_quota (
    month           TEXT PRIMARY KEY,              -- YYYY-MM
    searches_used   INTEGER NOT NULL DEFAULT 0,
    verifications_used INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_cache (
    domain          TEXT NOT NULL,
    query           TEXT NOT NULL,
    provider        TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (domain, query, provider)
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key       TEXT PRIMARY KEY,              -- sha256(model + prompt_version + input)
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pain_points (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                      TEXT REFERENCES runs(run_id),
    domain                      TEXT NOT NULL,
    position                    INTEGER NOT NULL,  -- 1 or 2
    signal                      TEXT NOT NULL,
    source_url                  TEXT NOT NULL CHECK (source_url LIKE 'http%'),
    event_date                  TEXT,
    hypothesis                  TEXT NOT NULL,
    why_it_matters_to_this_role TEXT NOT NULL,
    confidence                  TEXT NOT NULL,
    prompt_version              TEXT,
    created_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS emails (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT REFERENCES runs(run_id),
    domain              TEXT NOT NULL,
    attempt             INTEGER NOT NULL,
    draft_json          TEXT NOT NULL,             -- EmailDraft
    guardrail_ok        INTEGER NOT NULL,
    guardrail_errors    TEXT NOT NULL DEFAULT '[]',
    word_count          INTEGER,
    prompt_version      TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sends (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT REFERENCES runs(run_id),
    domain              TEXT NOT NULL,
    to_address          TEXT NOT NULL,             -- always DEMO_RECIPIENT_EMAIL
    intended_recipient  TEXT,
    status              TEXT NOT NULL,             -- DRY_RUN | SENT | FAILED
    message_id          TEXT,
    reason              TEXT,
    detail              TEXT,
    sent_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT REFERENCES runs(run_id),
    domain          TEXT,
    step            TEXT NOT NULL,
    reason          TEXT NOT NULL,
    detail          TEXT,
    at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_failures_domain ON failures(domain);

CREATE TABLE IF NOT EXISTS costs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT REFERENCES runs(run_id),
    domain          TEXT,
    step            TEXT NOT NULL,
    kind            TEXT NOT NULL,                 -- llm | scrapingbee | hunter_search | hunter_verify | search | gmail
    model           TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    credits         INTEGER NOT NULL DEFAULT 0,
    calls           INTEGER NOT NULL DEFAULT 1,
    cost_usd        REAL,                          -- NULL = price unknown, never guessed
    cached          INTEGER NOT NULL DEFAULT 0,
    at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_costs_domain ON costs(domain);
