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

from src.common.config import is_serverless, load_settings  # noqa: E402
from src.common.domains import InvalidDomain, normalise_domain, parse_domains  # noqa: E402
from src.common.safety import UnsafeRecipientError, demo_recipient  # noqa: E402

MAX_DOMAINS = int(os.getenv("SPRINTO_MAX_DOMAINS", "10"))
MAX_INPUT_CHARS = 20000
PROCESS_TIMEOUT_S = int(os.getenv("SPRINTO_PROCESS_TIMEOUT_S", "240"))   # below Vercel's 300 s function limit
SEND_LIMIT = int(os.getenv("SPRINTO_SEND_LIMIT", "10"))     # per server instance, protects the demo inbox
_sends: list[float] = []
_lock = threading.Lock()

app = FastAPI(title="Sprinto Outreach", version="1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")


class _VercelPathRestore:
    """vercel.json rewrites /api/<route> to /api/index?__path=<route>; restore the original path for routing."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") in ("/api/index", "/api/index.py", "/api"):
            from urllib.parse import parse_qsl, urlencode
            qs = parse_qsl(scope.get("query_string", b"").decode(), keep_blank_values=True)
            route = next((v for k, v in qs if k == "__path"), None)
            if route is not None:
                scope = dict(scope)
                scope["path"] = "/api/" + route.lstrip("/")
                scope["raw_path"] = scope["path"].encode()
                scope["query_string"] = urlencode([(k, v) for k, v in qs if k != "__path"]).encode()
        await self.inner(scope, receive, send)


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
    # Serverless: one SQLite file PER REQUEST. Concurrent requests share a process on Vercel and a shared file in
    # /tmp locked up live; /tmp is ephemeral anyway and Hunter's quota is checked against Hunter's own counter.
    if is_serverless():
        db_path = s.pipeline.paths.abs("db").parent / "requests" / f"{run_id}.db"
    else:
        db_path = s.pipeline.paths.abs("db")
    db = Database(db_path)
    db.init()
    result: dict = {}

    def _work():
        try:
            pipe = build_live(s, db, run_id, dry_run=True, live_hunter=_hunter_live(s),
                              transport=None if s.secrets.scrapingbee_api_key else "direct", today=date.today())
            outs = pipe.run([raw], RunOptions(dry_run=True, refresh=bool(payload.get("refresh"))))
            import json as _json
            result["outcome"] = _json.loads(outs[0].to_json())
        except Exception as e:
            result["outcome"] = _fail_outcome(domain, raw, "unexpected_error",
                                              f"{type(e).__name__}: {str(e)[:200]}", step="pipeline")

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(PROCESS_TIMEOUT_S)
    if t.is_alive():
        return _fail_outcome(domain, raw, "timeout",
                             f"Research took longer than {PROCESS_TIMEOUT_S} s and was stopped so the app stays "
                             f"responsive. The site or an AI model was very slow; run this company again.",
                             step="pipeline")
    try:
        db.close()
        if is_serverless():
            os.remove(db_path)
    except OSError:
        pass
    return result.get("outcome") or _fail_outcome(domain, raw, "unexpected_error", "no result", step="pipeline")


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


@app.post("/api/followup")
def followup(payload: dict = Body(default={})):
    """Follow-up agent after the cold email: six-field state at every checkpoint (demo calendar + demo CRM)."""
    from src.followup.workflow import SCENARIOS, run_followup
    p = payload or {}
    o = p.get("outcome") or {}
    scenario = p.get("scenario") or "accepts"
    reply_text = p.get("reply_text")
    counter_text = p.get("counter_text")
    if scenario not in SCENARIOS:
        return _err(400, "invalid_scenario", f"Unknown scenario. Use one of: {', '.join(SCENARIOS)}.")
    for name, val in (("reply_text", reply_text), ("counter_text", counter_text)):
        if val is not None and (not isinstance(val, str) or len(val) > 2000):
            return _err(400, "invalid_reply", f"'{name}' must be text under 2000 characters.")
    if reply_text is not None and not reply_text.strip():
        return _err(400, "empty_reply", "Paste the prospect's reply, or pick a scenario instead.")
    if o.get("status") not in ("DRAFTED", "SENT"):
        return _err(409, "not_ready", "Follow-up runs only for companies with an approved draft (status DRAFTED or SENT).")
    today = None
    try:
        today = date.fromisoformat(str(p.get("today"))[:10]) if p.get("today") else None
    except ValueError:
        today = None
    try:
        return run_followup(o, scenario, reply_text, counter_text, today=today,
                            sender=_settings().pipeline.email.sender_name or "Vikram")
    except ValueError as e:
        return _err(422, "no_contact", str(e))


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


@app.api_route("/api/{full_path:path}", methods=["GET", "POST"])
def not_found(full_path: str):
    return _err(404, "not_found", f"No API route for /api/{full_path}")


# Local development: serve the UI from public/ (on Vercel, public/ is served statically).
if not is_serverless():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(ROOT / "public"), html=True), name="public")

app = _VercelPathRestore(app)   # noqa: F811  (ASGI wrapper; routes are registered above)
