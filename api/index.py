"""Web API (Vercel serverless / local uvicorn). The UI lives in public/.

  GET  /api/health   which services are configured (booleans only), models, limits
  POST /api/parse    {text} -> normalised unique domains, duplicates, invalid lines (never throws)
  POST /api/process  {domain} -> full pipeline for ONE company (dry run) -> outcome JSON
  POST /api/send     {outcome} -> re-checks the draft, sends ONLY to DEMO_RECIPIENT_EMAIL
  POST /api/demo     offline demo world (fixtures, recorded model output): zero API calls

Every failure returns a structured outcome with a reason code; the API never crashes on bad input.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import Body, FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from src.common.config import load_settings  # noqa: E402
from src.common.domains import InvalidDomain, normalise_domain, parse_domains  # noqa: E402
from src.common.safety import UnsafeRecipientError, demo_recipient  # noqa: E402

MAX_DOMAINS = int(os.getenv("SPRINTO_MAX_DOMAINS", "10"))
MAX_INPUT_CHARS = 20000
SEND_LIMIT = int(os.getenv("SPRINTO_SEND_LIMIT", "10"))     # per server instance, protects the demo inbox
_sends: list[float] = []
_lock = threading.Lock()

app = FastAPI(title="Sprinto Outreach", version="1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")


def _err(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message, **extra})


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):   # last resort: never an HTML 500
    return _err(500, "unexpected_error", f"{type(exc).__name__}: {str(exc)[:200]}")


def _settings():
    s = load_settings()
    return s


def _services(s) -> dict:
    from src.mail.gmail_sender import GmailSender
    sec = s.secrets
    try:
        demo_recipient()
        recip = True
    except UnsafeRecipientError:
        recip = False
    gmail = GmailSender(sec.gmail_client_secret_file, sec.gmail_token_file).configured()
    return {"gemini": bool(sec.gemini_api_key), "scrapingbee": bool(sec.scrapingbee_api_key),
            "hunter": bool(sec.hunter_api_key), "hunter_live": _hunter_live(s), "gmail": gmail,
            "demo_recipient": recip}


def _hunter_live(s) -> bool:
    return bool(s.secrets.hunter_api_key) and os.getenv("SPRINTO_HUNTER_LIVE", "0") == "1"


@app.get("/api/health")
def health():
    s = _settings()
    p = s.pipeline
    return {"ok": True, "services": _services(s), "models": dict(p.llm.models),
            "fallback_model": p.llm.fallback_model, "rubric": s.rubric.rubric_version,
            "already_certified_policy": p.qualification.already_certified_policy,
            "max_domains": MAX_DOMAINS, "transport": "scrapingbee" if s.secrets.scrapingbee_api_key else "direct",
            "commit": (os.getenv("VERCEL_GIT_COMMIT_SHA") or "")[:7] or None,
            "server_time": datetime.now(timezone.utc).isoformat()}


HEADER_WORDS = {"domain", "domains", "website", "websites", "url", "urls", "company", "companies"}


def split_input(text: str) -> list[str]:
    """Accept one-per-line, comma/semicolon/tab separated, or a CSV whose first column is the domain."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^\[[^\]]+\]\([^)]*\)$", line):      # markdown link stays whole
            out.append(line)
            continue
        for part in re.split(r"[,;\t]|\s{2,}|\s(?=\S+\.\S)", line):
            part = part.strip().strip('"').strip("'")
            if part and part.lower() not in HEADER_WORDS:
                out.append(part)
    return out


@app.post("/api/parse")
def parse(payload: dict = Body(default={})):
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip():
        return _err(400, "empty_input", "Paste at least one company domain, e.g. acme.com (one per line).")
    if len(text) > MAX_INPUT_CHARS:
        return _err(413, "input_too_large", f"Input is larger than {MAX_INPUT_CHARS} characters.")
    parts = split_input(text)
    if not parts:
        return _err(400, "empty_input", "No domains found in the input.")
    parsed = parse_domains(parts)
    unique = [{"domain": d, "raw": r} for d, r in parsed.unique]
    truncated = unique[MAX_DOMAINS:]
    return {"unique": unique[:MAX_DOMAINS],
            "duplicates": [{"domain": d, "raw": r} for d, r in parsed.duplicates],
            "invalid": [{"raw": r, "error": e} for r, e in parsed.invalid],
            "truncated": [u["domain"] for u in truncated],
            "message": (f"Only the first {MAX_DOMAINS} unique domains are processed per batch; "
                        f"{len(truncated)} were left out." if truncated else None)}


def _fail_outcome(domain: str, raw: str, reason: str, message: str, step: str = "input") -> dict:
    return {"domain": domain, "input_raw": raw, "status": "FAILED", "status_reason": message,
            "next_action": "Fix the issue described and run this company again",
            "failures": [{"step": step, "reason": reason, "detail": message}], "cost": {}, "seconds": 0}


@app.post("/api/process")
def process(payload: dict = Body(default={})):
    raw = str((payload or {}).get("domain") or "").strip()
    if not raw:
        return _err(400, "empty_input", "Missing 'domain'.")
    try:
        domain = normalise_domain(raw)
    except InvalidDomain as e:
        return _err(422, "invalid_domain", str(e), outcome={
            "domain": raw, "input_raw": raw, "status": "INVALID", "status_reason": str(e),
            "next_action": "Check the spelling; use a bare domain like acme.com",
            "failures": [{"step": "input", "reason": "invalid_domain", "detail": str(e)}]})

    s = _settings()
    if not s.secrets.gemini_api_key:
        return _fail_outcome(domain, raw, "missing_config",
                             "The server has no GEMINI_API_KEY configured, so it cannot analyse companies. "
                             "Try the offline demo, or set the key in the deployment.")
    from src.factory import build_live
    from src.pipeline import RunOptions
    from src.store.db import Database

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    if _hunter_live(s):
        s.pipeline.hunter.mode = "live"
    db = Database(s.pipeline.paths.abs("db"))
    db.init()
    try:
        pipe = build_live(s, db, run_id, dry_run=True, live_hunter=_hunter_live(s),
                          transport=None if s.secrets.scrapingbee_api_key else "direct", today=date.today())
        outs = pipe.run([raw], RunOptions(dry_run=True, refresh=bool(payload.get("refresh"))))
        o = outs[0]
        import json as _json
        return _json.loads(o.to_json())
    except Exception as e:
        return _fail_outcome(domain, raw, "unexpected_error", f"{type(e).__name__}: {str(e)[:200]}", step="pipeline")


@app.post("/api/send")
def send(payload: dict = Body(default={})):
    from src.mail.gmail_sender import GmailSender
    from src.mail.guardrails import check_draft
    from src.store.models import Contact, EmailDraft

    o = (payload or {}).get("outcome") or {}
    try:
        draft = EmailDraft(**o["draft"])
        contact = Contact(**o["contact"]["chosen"])
        domain = normalise_domain(o["domain"])
    except Exception:
        return _err(400, "invalid_request", "Send needs a processed company with a draft and a chosen contact.")
    s = _settings()
    allowed = {e.source_url for e in draft.personalisation_evidence} | {
        e.get("source_url") for e in (o.get("evidence") or []) if isinstance(e, dict)}
    hook = draft.personalisation_evidence[0].source_url if draft.personalisation_evidence else None
    errs = check_draft(draft, contact, allowed, hook, s.pipeline.email.max_words, s.pipeline.email.word_tolerance,
                       s.pipeline.email.max_em_dashes)
    if errs:
        return _err(422, "guardrail_failed", "This draft does not pass the quality rules, so it was not sent.",
                    errors=errs)
    with _lock:
        now = time.time()
        _sends[:] = [t for t in _sends if now - t < 3600]
        if len(_sends) >= SEND_LIMIT:
            return _err(429, "send_limit", f"Demo send limit reached ({SEND_LIMIT} per hour). Try later.")
        _sends.append(now)
    res = GmailSender(s.secrets.gmail_client_secret_file, s.secrets.gmail_token_file, dry_run=False).send(
        draft, contact, domain)
    return res.model_dump(mode="json")


@app.post("/api/demo")
def demo():
    from src import demo_offline
    from src.factory import build_offline_demo
    from src.pipeline import RunOptions
    from src.store.db import Database
    import json as _json
    import tempfile

    s = _settings()
    tmp = Path(tempfile.mkdtemp(prefix="sprinto-demo-"))
    db = Database(tmp / "demo.db")
    db.init()
    pipe = build_offline_demo(s, db, "demo-" + uuid.uuid4().hex[:6], reports_dir=tmp / "reports",
                              outcomes_dir=tmp / "outcomes")
    outs = pipe.run(demo_offline.DOMAINS, RunOptions(dry_run=True, command="demo"))
    return {"today": pipe.today.isoformat(), "outcomes": [_json.loads(o.to_json()) for o in outs]}


if os.getenv("VERCEL"):
    @app.api_route("/{full_path:path}", methods=["GET", "POST"])
    def not_found(full_path: str, request: Request):
        return _err(404, "not_found", f"No API route for /{full_path}",
                    seen_path=request.url.path, headers={k: v for k, v in request.headers.items()
                                                         if k.startswith("x-") and "auth" not in k
                                                         and "cookie" not in k})

# Local development: serve the UI from public/ (on Vercel, public/ is served statically).
if not os.getenv("VERCEL"):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(ROOT / "public"), html=True), name="public")
