"""LLM access with caching, retries, schema validation, and cost metering.

- generate_json(): structured output validated by pydantic. On schema failure: retry ONCE with the error
  message, then raise LlmError(llm_schema_error). Transient errors: backoff, max 2 retries.
- grounded(): Google Search grounding. Returns prose + citations taken from grounding METADATA only
  (never from URLs the model writes in prose). Search tools and response schemas are never combined.
- Every call: cache lookup by sha256(model, prompt_version, schema, input) -> zero-cost re-runs;
  budget check before paid calls; cost row written per call (cost None when price unknown).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from src.common.budget import CompanyBudget
from src.common.config import LlmCfg
from src.common.logging import get_logger
from src.store.db import Database
from src.store.models import CostEntry, ReasonCode, Step

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)


class LlmError(RuntimeError):
    def __init__(self, reason: ReasonCode, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason.value}: {detail}")


@dataclass
class CallCtx:
    domain: Optional[str]
    step: Step
    budget: Optional[CompanyBudget] = None
    run_id: Optional[str] = None


@dataclass
class LlmResult:
    parsed: Any
    raw_text: str
    model: str
    prompt_version: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Optional[float] = None
    cached: bool = False


@dataclass
class Citation:
    index: int
    uri: str
    title: Optional[str] = None
    resolved_uri: Optional[str] = None


@dataclass
class GroundedResult:
    text: str
    citations: list[Citation] = field(default_factory=list)
    supports: list[dict] = field(default_factory=list)     # {"text": segment, "citations": [idx,...]}
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Optional[float] = None
    cached: bool = False


@dataclass
class RawCall:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class TransientLlmError(RuntimeError):
    pass


class BaseLlm:
    """Shared caching/retry/validation/costing. Subclasses implement _call_json and _call_grounded."""

    def __init__(self, cfg: LlmCfg, db: Database, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.db = db
        self.sleep = sleep

    # ------------------------------------------------------------------ overridables
    def _call_json(self, model: str, system: str, user: str, schema: Type[BaseModel]) -> RawCall:
        raise NotImplementedError

    def _call_grounded(self, model: str, prompt: str) -> tuple[RawCall, list[Citation], list[dict]]:
        raise NotImplementedError

    # ------------------------------------------------------------------ helpers
    def _key(self, *parts: str) -> str:
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    def _record(self, ctx: CallCtx, model: str, in_t: int, out_t: int, cost: Optional[float], cached: bool,
                kind: str = "llm") -> None:
        self.db.add_cost(CostEntry(domain=ctx.domain, step=ctx.step, kind=kind, model=model, input_tokens=in_t,
                                   output_tokens=out_t, cost_usd=0.0 if cached else cost, cached=cached),
                         run_id=ctx.run_id)
        if ctx.budget and not cached:
            ctx.budget.add(cost_usd=cost, tokens=in_t + out_t)

    def _with_retries(self, fn: Callable[[], Any]) -> Any:
        last: Optional[Exception] = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                return fn()
            except TransientLlmError as e:
                last = e
                if attempt < self.cfg.max_retries:
                    self.sleep(2 ** attempt)
        reason = ReasonCode.LLM_TIMEOUT if last and "timeout" in str(last).lower() else ReasonCode.LLM_ERROR
        raise LlmError(reason, str(last))

    # ------------------------------------------------------------------ public
    def generate_json(self, ctx: CallCtx, task: str, system: str, user: str, schema: Type[T]) -> LlmResult:
        model = self.cfg.models[task]
        pv = self.cfg.prompt_versions.get(task, "v1")
        key = self._key(model, pv, schema.__name__, system, user)
        row = self.db.one("SELECT * FROM llm_cache WHERE cache_key=?", (key,))
        if row:
            parsed = schema.model_validate_json(row["response_json"])
            self._record(ctx, model, row["input_tokens"] or 0, row["output_tokens"] or 0, 0.0, cached=True)
            return LlmResult(parsed=parsed, raw_text=row["response_json"], model=model, prompt_version=pv,
                             input_tokens=row["input_tokens"] or 0, output_tokens=row["output_tokens"] or 0,
                             cost_usd=0.0, cached=True)
        if ctx.budget:
            ctx.budget.check()

        in_total = out_total = 0
        prompt = user
        parsed = None
        raw: Optional[RawCall] = None
        for schema_attempt in range(2):   # original + ONE corrective retry
            raw = self._with_retries(lambda: self._call_json(model, system, prompt, schema))
            in_total += raw.input_tokens
            out_total += raw.output_tokens
            try:
                parsed = schema.model_validate_json(_strip_fences(raw.text))
                break
            except ValidationError as e:
                log.warning("llm schema error", extra={"domain": ctx.domain, "task": task,
                                                       "attempt": schema_attempt + 1})
                prompt = (user + "\n\nYOUR PREVIOUS OUTPUT FAILED VALIDATION:\n" + str(e)[:1500]
                          + "\nReturn ONLY corrected JSON matching the schema.")
        cost = self.cfg.cost(model, in_total, out_total)
        self._record(ctx, model, in_total, out_total, cost, cached=False)
        if parsed is None:
            raise LlmError(ReasonCode.LLM_SCHEMA_ERROR, f"{task}: output failed schema validation twice")
        text = parsed.model_dump_json()
        self.db.execute(
            "INSERT OR REPLACE INTO llm_cache (cache_key, model, prompt_version, response_json, input_tokens,"
            " output_tokens, created_at) VALUES (?,?,?,?,?,?,?)",
            (key, model, pv, text, in_total, out_total, datetime.now(timezone.utc).isoformat()))
        return LlmResult(parsed=parsed, raw_text=text, model=model, prompt_version=pv, input_tokens=in_total,
                         output_tokens=out_total, cost_usd=cost, cached=False)

    def grounded(self, ctx: CallCtx, prompt: str) -> GroundedResult:
        """Uncached here: SearchProvider caches by (domain, query)."""
        model = self.cfg.models["search"]
        if ctx.budget:
            ctx.budget.check()
        raw, cites, supports = self._with_retries(lambda: self._call_grounded(model, prompt))
        cost = self.cfg.cost(model, raw.input_tokens, raw.output_tokens)
        if cost is not None:
            cost += self.cfg.grounding_usd_per_request
        self._record(ctx, model, raw.input_tokens, raw.output_tokens, cost, cached=False, kind="search")
        return GroundedResult(text=raw.text, citations=cites, supports=supports, model=model,
                              input_tokens=raw.input_tokens, output_tokens=raw.output_tokens, cost_usd=cost)


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


class GeminiLlm(BaseLlm):
    """google-genai SDK implementation."""

    def __init__(self, cfg: LlmCfg, db: Database, api_key: str, **kw):
        super().__init__(cfg, db, **kw)
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set")
        from google import genai
        from google.genai import types
        self._types = types
        self.client = genai.Client(api_key=api_key,
                                   http_options=types.HttpOptions(timeout=cfg.timeout_s * 1000))

    def _usage(self, resp) -> tuple[int, int]:
        u = getattr(resp, "usage_metadata", None)
        if not u:
            return 0, 0
        out = (getattr(u, "candidates_token_count", 0) or 0) + (getattr(u, "thoughts_token_count", 0) or 0)
        return getattr(u, "prompt_token_count", 0) or 0, out

    def _config(self, **kw):
        t = self._types
        extra = {}
        if self.cfg.thinking_level:
            extra["thinking_config"] = t.ThinkingConfig(thinking_level=self.cfg.thinking_level)
        return t.GenerateContentConfig(temperature=self.cfg.temperature, **extra, **kw)

    def _call_json(self, model: str, system: str, user: str, schema: Type[BaseModel]) -> RawCall:
        from google.genai import errors
        try:
            try:
                resp = self.client.models.generate_content(
                    model=model, contents=user,
                    config=self._config(system_instruction=system, response_mime_type="application/json",
                                        response_schema=schema))
            except errors.ClientError as e:
                if "schema" not in str(e).lower():
                    raise
                # Fallback: JSON mode with the schema in the prompt (some schema features are unsupported)
                resp = self.client.models.generate_content(
                    model=model,
                    contents=user + "\n\nJSON SCHEMA:\n" + json.dumps(schema.model_json_schema()),
                    config=self._config(system_instruction=system, response_mime_type="application/json"))
        except errors.ServerError as e:
            raise TransientLlmError(f"server error: {e}") from e
        except errors.ClientError as e:
            if getattr(e, "code", None) == 429:
                raise TransientLlmError(f"rate limited: {e}") from e
            raise LlmError(ReasonCode.LLM_ERROR, str(e)[:300]) from e
        except Exception as e:  # httpx timeouts etc.
            if "timeout" in str(e).lower() or "timed out" in str(e).lower():
                raise TransientLlmError(f"timeout: {e}") from e
            raise LlmError(ReasonCode.LLM_ERROR, f"{type(e).__name__}: {str(e)[:300]}") from e
        in_t, out_t = self._usage(resp)
        return RawCall(text=resp.text or "", input_tokens=in_t, output_tokens=out_t)

    def _call_grounded(self, model: str, prompt: str):
        from google.genai import errors
        t = self._types
        try:
            resp = self.client.models.generate_content(
                model=model, contents=prompt,
                config=self._config(tools=[t.Tool(google_search=t.GoogleSearch())]))
        except errors.ServerError as e:
            raise TransientLlmError(str(e)) from e
        except errors.ClientError as e:
            if getattr(e, "code", None) == 429:
                raise TransientLlmError(str(e)) from e
            raise LlmError(ReasonCode.SEARCH_UNAVAILABLE, str(e)[:300]) from e
        except Exception as e:
            if "timeout" in str(e).lower():
                raise TransientLlmError(str(e)) from e
            raise LlmError(ReasonCode.SEARCH_UNAVAILABLE, f"{type(e).__name__}: {str(e)[:300]}") from e
        cites: list[Citation] = []
        supports: list[dict] = []
        cand = (resp.candidates or [None])[0]
        gm = getattr(cand, "grounding_metadata", None) if cand else None
        if gm:
            for i, ch in enumerate(getattr(gm, "grounding_chunks", None) or []):
                web = getattr(ch, "web", None)
                if web and getattr(web, "uri", None):
                    cites.append(Citation(index=i, uri=web.uri, title=getattr(web, "title", None)))
            for sp in getattr(gm, "grounding_supports", None) or []:
                seg = getattr(sp, "segment", None)
                supports.append({"text": getattr(seg, "text", "") if seg else "",
                                 "citations": list(getattr(sp, "grounding_chunk_indices", None) or [])})
        in_t, out_t = self._usage(resp)
        return RawCall(text=resp.text or "", input_tokens=in_t, output_tokens=out_t), cites, supports


class FakeLlm(BaseLlm):
    """Offline/test LLM. handlers: task -> fn(system, user) -> dict | str (JSON).
    grounded_handler: fn(prompt) -> (text, [Citation], supports)."""

    def __init__(self, cfg: LlmCfg, db: Database, handlers: Optional[dict] = None,
                 grounded_handler: Optional[Callable] = None):
        super().__init__(cfg, db, sleep=lambda _: None)
        self.handlers = handlers or {}
        self.grounded_handler = grounded_handler
        self.calls: list[tuple[str, str]] = []
        self._task_for_model: dict[str, str] = {}

    def generate_json(self, ctx, task, system, user, schema):
        self._current_task = task
        return super().generate_json(ctx, task, system, user, schema)

    def _call_json(self, model, system, user, schema):
        task = self._current_task
        self.calls.append((task, user))
        if task not in self.handlers:
            raise LlmError(ReasonCode.LLM_ERROR, f"FakeLlm has no handler for task {task!r}")
        out = self.handlers[task](system, user)
        text = out if isinstance(out, str) else json.dumps(out)
        return RawCall(text=text, input_tokens=len(system + user) // 4, output_tokens=len(text) // 4)

    def _call_grounded(self, model, prompt):
        self.calls.append(("search", prompt))
        if not self.grounded_handler:
            raise LlmError(ReasonCode.SEARCH_UNAVAILABLE, "no grounded handler")
        text, cites, supports = self.grounded_handler(prompt)
        return RawCall(text=text, input_tokens=len(prompt) // 4, output_tokens=len(text) // 4), cites, supports
