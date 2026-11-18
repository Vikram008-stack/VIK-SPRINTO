"""Load and validate config/*.yaml + .env into typed settings.

Secrets only ever come from the environment (.env). They are never logged or printed.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


def is_serverless() -> bool:
    """True on Vercel/Lambda (env markers) or whenever the code directory is read-only."""
    if any(os.getenv(k) for k in ("VERCEL", "VERCEL_ENV", "VERCEL_URL", "AWS_LAMBDA_FUNCTION_NAME",
                                  "LAMBDA_TASK_ROOT")):
        return True
    return not os.access(ROOT, os.W_OK)


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ pipeline.yaml
class RunCfg(_Cfg):
    concurrency: int = Field(3, ge=1, le=10)
    max_cost_usd_per_company: float = Field(0.05, gt=0)
    max_seconds_per_company: int = Field(120, gt=0)
    per_domain_delay_s: float = Field(2.0, ge=0)


class ScrapeCfg(_Cfg):
    provider: Literal["scrapingbee"] = "scrapingbee"
    cache_ttl_days: int = Field(7, ge=0)
    max_fetches_per_company: int = Field(5, ge=1, le=10)
    js_escalation_min_words: int = 200
    timeout_s: int = 30
    respect_robots: bool = True
    robots_user_agent: str = "*"
    credits: dict[str, int] = Field(default_factory=lambda: {"plain": 1, "js": 5})
    usd_per_credit: Optional[float] = None
    transport: Literal["scrapingbee", "direct"] = "scrapingbee"

    @model_validator(mode="after")
    def _robots_on(self):
        if not self.respect_robots:
            raise ValueError("respect_robots must stay true (non-negotiable)")
        return self


class CleanerCfg(_Cfg):
    max_tokens_per_group: int = 2000
    min_line_words: int = 8


class LlmCfg(_Cfg):
    provider: Literal["gemini"] = "gemini"
    models: dict[Literal["extract", "qualify", "pain", "draft", "search"], str]
    thinking_level: Optional[str] = None
    fallback_model: Optional[str] = None
    temperature: float = 0.2
    max_retries: int = Field(2, ge=0, le=2)
    timeout_s: int = 60
    pricing_usd_per_1m: dict[str, dict[Literal["input", "output"], float]] = Field(default_factory=dict)
    grounding_usd_per_request: float = 0.0
    prompt_versions: dict[str, str]

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
        """USD cost, or None if the model's price is not configured (never guessed)."""
        p = self.pricing_usd_per_1m.get(model)
        if not p:
            return None
        return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000


class SearchCfg(_Cfg):
    provider: Literal["gemini_grounded", "serper"] = "gemini_grounded"
    max_searches_per_company: int = Field(3, ge=0, le=3)
    cache_ttl_days: int = 7
    loop_may_search: bool = True


class HunterFilters(_Cfg):
    department: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    type: Optional[Literal["personal", "generic"]] = "personal"


class HunterCfg(_Cfg):
    mode: Literal["mock", "live"] = "mock"
    monthly_budget: int = 25
    hard_stop: int = 20
    verifier: bool = True
    verifier_monthly_budget: int = 50
    min_confidence: int = 70
    results_limit: int = 10
    filters: HunterFilters = HunterFilters()
    allow_role_mailbox_fallback: bool = False

    @model_validator(mode="after")
    def _stop_below_budget(self):
        if self.hard_stop > self.monthly_budget:
            raise ValueError("hunter.hard_stop must be <= hunter.monthly_budget")
        return self


class ContactsCfg(_Cfg):
    manual_contacts_file: str = "config/manual_contacts.yaml"


class QualificationCfg(_Cfg):
    already_certified_policy: Literal["research", "disqualify"] = "research"
    max_extra_rounds: int = Field(2, ge=0, le=2)


class EmailCfg(_Cfg):
    mode: Literal["dry_run", "send"] = "dry_run"
    max_words: int = 120
    word_tolerance: float = 0.10
    max_em_dashes: int = 0
    guardrail_max_attempts: int = Field(2, ge=1, le=2)
    sender_name: str = ""
    sprinto_context: str


class PathsCfg(_Cfg):
    db: str = "data/outreach.db"
    cache: str = "data/cache"
    logs: str = "data/logs"
    reports: str = "reports"

    def abs(self, name: str) -> Path:
        p = Path(getattr(self, name))
        if p.is_absolute():
            return p
        # Serverless (Vercel): the code directory is read-only; write under /tmp (or SPRINTO_DATA_DIR)
        data_dir = os.getenv("SPRINTO_DATA_DIR") or ("/tmp/sprinto" if is_serverless() else None)
        if data_dir:
            parts = p.parts[1:] if p.parts and p.parts[0] == "data" else p.parts
            return Path(data_dir).joinpath(*parts)
        return ROOT / p


class PipelineCfg(_Cfg):
    version: int
    run: RunCfg
    scrape: ScrapeCfg
    cleaner: CleanerCfg
    llm: LlmCfg
    search: SearchCfg
    hunter: HunterCfg
    contacts: ContactsCfg
    qualification: QualificationCfg
    email: EmailCfg
    paths: PathsCfg


# ------------------------------------------------------------------ icp_rubric.yaml
class EvidenceMapRow(_Cfg):
    id: str
    question: str
    sources: list[str]
    fetch_limit: int = Field(ge=0, le=2)
    sufficient_when: str
    if_unanswered: str
    short_circuit: str


class RubricCfg(BaseModel):
    model_config = ConfigDict(extra="allow")
    rubric_version: str
    components: dict[str, dict[str, Any]]
    decision: dict[str, Any]
    hard_disqualifiers: list[str]
    not_disqualifiers: list[str]
    evidence_quality: dict[str, list[str]]
    evidence_map: list[EvidenceMapRow]

    @model_validator(mode="after")
    def _maxes_sum_to_100(self):
        total = sum(int(c["max"]) for c in self.components.values())
        if total != 100:
            raise ValueError(f"rubric component maxima must sum to 100, got {total}")
        for name, comp in self.components.items():
            for bucket, pts in (comp.get("buckets") or {}).items():
                if int(pts) > int(comp["max"]):
                    raise ValueError(f"{name}.{bucket}={pts} exceeds component max {comp['max']}")
        return self

    def bucket_points(self, component: str, bucket: str) -> int:
        return int(self.components[component]["buckets"][bucket])


# ------------------------------------------------------------------ roles.yaml
class RolesCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    tiers: dict[int, list[str]]
    small_company: dict[str, Any]
    penalise: list[str]
    tie_break: list[str]
    min_confidence: int = 70


# ------------------------------------------------------------------ secrets (.env)
SECRET_KEYS = (
    "SCRAPINGBEE_API_KEY", "GEMINI_API_KEY", "HUNTER_API_KEY", "SEARCH_API_KEY",
    "GMAIL_CLIENT_SECRET_FILE", "GMAIL_TOKEN_FILE", "DEMO_RECIPIENT_EMAIL",
)


class Secrets(BaseModel):
    scrapingbee_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    hunter_api_key: Optional[str] = None
    search_api_key: Optional[str] = None
    gmail_client_secret_file: Optional[str] = None
    gmail_token_file: Optional[str] = None
    demo_recipient_email: Optional[str] = None

    def present(self) -> dict[str, bool]:
        """Which secrets are set - for `check` output. Never returns values."""
        return {k.upper(): bool(v) for k, v in self.model_dump().items()}

    def __repr__(self) -> str:  # never leak values into logs/tracebacks
        return f"Secrets({', '.join(k for k, v in self.present().items() if v)})"

    __str__ = __repr__


# ------------------------------------------------------------------ loaders
def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Settings(BaseModel):
    pipeline: PipelineCfg
    rubric: RubricCfg
    roles: RolesCfg
    manual_contacts: dict[str, dict[str, str]]
    secrets: Secrets

    @property
    def root(self) -> Path:
        return ROOT


def load_secrets(env_file: Optional[Path] = None) -> Secrets:
    load_dotenv(env_file or ROOT / ".env", override=False)
    return Secrets(**{k.lower(): (os.getenv(k) or None) for k in SECRET_KEYS})


def load_settings(config_dir: Optional[Path] = None, env_file: Optional[Path] = None) -> Settings:
    d = config_dir or CONFIG_DIR
    pipeline = PipelineCfg(**_load_yaml(d / "pipeline.yaml"))
    rubric = RubricCfg(**_load_yaml(d / "icp_rubric.yaml"))
    roles = RolesCfg(**_load_yaml(d / "roles.yaml"))
    manual = _load_yaml(d / "manual_contacts.yaml")
    return Settings(pipeline=pipeline, rubric=rubric, roles=roles,
                    manual_contacts=manual, secrets=load_secrets(env_file))


def config_hash(config_dir: Optional[Path] = None) -> str:
    """sha256 over the config files, stored on every run for reproducibility."""
    import hashlib
    d = config_dir or CONFIG_DIR
    h = hashlib.sha256()
    for name in ("pipeline.yaml", "icp_rubric.yaml", "roles.yaml"):
        h.update((d / name).read_bytes())
    return h.hexdigest()[:16]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
