"""Pydantic models shared by every pipeline step.

Rules encoded here:
- Every fact is nullable. Missing = None, never a guess.
- A signal without a source_url cannot exist (validator), so it can never earn points.
- observed_at (when we saw it) and event_date (when it happened) are separate fields.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Optional
from urllib.parse import urlparse

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- enums
class EvidenceQuality(str, Enum):
    STRONG = "STRONG"
    MEDIUM = "MEDIUM"
    WEAK = "WEAK"
    UNACCEPTABLE = "UNACCEPTABLE"

    @property
    def rank(self) -> int:
        return {"STRONG": 3, "MEDIUM": 2, "WEAK": 1, "UNACCEPTABLE": 0}[self.value]


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class QualStatus(str, Enum):
    QUALIFIED = "QUALIFIED"
    NEEDS_RESEARCH = "NEEDS_RESEARCH"
    REJECTED = "REJECTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    DUPLICATE = "DUPLICATE"


class CompanyStatus(str, Enum):
    PENDING = "PENDING"
    QUALIFIED = "QUALIFIED"
    NEEDS_RESEARCH = "NEEDS_RESEARCH"
    REJECTED = "REJECTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    DUPLICATE = "DUPLICATE"
    NO_CONTACT_FOUND = "NO_CONTACT_FOUND"
    DRAFT_NEEDS_HUMAN = "DRAFT_NEEDS_HUMAN"
    DRAFTED = "DRAFTED"          # dry run: draft stored, nothing sent
    SENT = "SENT"
    SEND_FAILED = "SEND_FAILED"
    FAILED = "FAILED"            # unexpected error; reason code always attached


class FetchMode(str, Enum):
    PLAIN = "plain"      # ScrapingBee render_js=false
    JS = "js"            # ScrapingBee render_js=true
    DIRECT = "direct"    # direct HTTP: robots.txt, and the dev-only direct transport
    FIXTURE = "fixture"  # offline tests


class ReasonCode(str, Enum):
    # scrape (Section 4 taxonomy)
    BOT_BLOCKED = "bot_blocked"
    RATE_LIMITED = "rate_limited"
    LOGIN_WALL = "login_wall"
    CAPTCHA = "captcha"
    EMPTY_SHELL = "empty_shell"
    NON_TEXT_CONTENT = "non_text_content"
    GEO_BLOCKED = "geo_blocked"
    TIMEOUT = "timeout"
    DNS_ERROR = "dns_error"
    ROBOTS_DISALLOWED = "robots_disallowed"
    THIN_CONTENT = "thin_content"
    DOMAIN_MISMATCH = "domain_mismatch"
    HTTP_ERROR = "http_error"
    FETCH_CAP_REACHED = "fetch_cap_reached"
    # input
    INVALID_DOMAIN = "invalid_domain"
    DUPLICATE_INPUT = "duplicate_input"
    # llm
    LLM_SCHEMA_ERROR = "llm_schema_error"
    LLM_TIMEOUT = "llm_timeout"
    LLM_ERROR = "llm_error"
    PRICING_UNKNOWN = "pricing_unknown"
    SCORE_DIFF = "score_diff"
    # budget
    BUDGET_EXCEEDED = "budget_exceeded"
    # contacts
    HUNTER_QUOTA_EXHAUSTED = "hunter_quota_exhausted"
    HUNTER_NO_RESULTS = "hunter_no_results"
    HUNTER_ERROR = "hunter_error"
    HUNTER_LIVE_NOT_ENABLED = "hunter_live_not_enabled"
    NO_CONTACT_FOUND = "no_contact_found"
    EMAIL_UNVERIFIED = "email_unverified"
    # search / pain
    SEARCH_UNAVAILABLE = "search_unavailable"
    URL_PROVENANCE_REJECTED = "url_provenance_rejected"
    FEWER_THAN_TWO_PAIN_POINTS = "fewer_than_two_pain_points"
    # email
    GUARDRAIL_FAILED = "guardrail_failed"
    GMAIL_AUTH_ERROR = "gmail_auth_error"
    GMAIL_QUOTA_ERROR = "gmail_quota_error"
    GMAIL_MIME_ERROR = "gmail_mime_error"
    GMAIL_SEND_ERROR = "gmail_send_error"
    UNSAFE_RECIPIENT = "unsafe_recipient"
    # catch-all (should be rare; always carries detail)
    UNEXPECTED_ERROR = "unexpected_error"
    HOMEPAGE_UNUSABLE = "homepage_unusable"
    PARKED_DOMAIN = "parked_domain"
    SNIPPET_NOT_FOUND = "snippet_not_found"
    NO_PAIN_EVIDENCE = "no_pain_evidence"


class Step(str, Enum):
    INPUT = "input"
    SCRAPE = "scrape"
    CLEAN = "clean"
    EXTRACT = "extract"
    QUALIFY = "qualify"
    CONTACTS = "contacts"
    PAIN = "pain"
    DRAFT = "draft"
    SEND = "send"
    REPORT = "report"


# --------------------------------------------------------------------------- helpers
def _require_http_url(v: str) -> str:
    v = (v or "").strip()
    parsed = urlparse(v)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"source_url must be an absolute http(s) URL, got {v!r}")
    return v


# A string that must be an absolute http(s) URL. Used for every source_url.
SourceUrl = Annotated[str, AfterValidator(_require_http_url)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# --------------------------------------------------------------------------- step 1: scrape / clean
class PageFetch(_Model):
    domain: str
    url: str
    final_url: Optional[str] = None
    page_type: Optional[str] = None          # homepage, careers, security, ...
    mode: FetchMode
    ok: bool
    status_code: Optional[int] = None
    failure_reason: Optional[ReasonCode] = None
    failure_detail: Optional[str] = None
    fetched_at: datetime = Field(default_factory=utcnow)
    from_cache: bool = False
    raw_tokens: Optional[int] = None
    clean_tokens: Optional[int] = None
    text_hash: Optional[str] = None
    credits: int = 0


class Chunk(_Model):
    text: str
    source_url: SourceUrl
    fetched_at: datetime
    page_type: Optional[str] = None
    token_estimate: int = 0


# --------------------------------------------------------------------------- step 2: extraction
BusinessModel = Literal["cloud_subscription", "hybrid", "dev_shop", "it_services", "marketplace", "non_software"]
BuyerType = Literal["business", "consumer", "both"]
FundingStage = Literal[
    "pre_seed", "seed", "series_a", "series_b", "series_c", "series_d_plus",
    "bootstrapped", "public", "acquired",
]
TriggerType = Literal[
    "explicit_audit_target", "customer_requires_cert", "stated_deadline", "grc_leader_hire",
    "procurement_questionnaire", "enterprise_expansion", "trust_page_no_need", "generic_mention",
]


class Signal(_Model):
    """A sourced observation. Cannot be constructed without a valid source_url."""
    signal: str
    source_url: SourceUrl
    snippet: Optional[str] = None            # supporting quote, <= 25 words
    event_date: Optional[date] = None        # when it happened (from the source); None = undated
    observed_at: datetime = Field(default_factory=utcnow)  # when we saw it
    evidence_quality: EvidenceQuality = EvidenceQuality.WEAK
    trigger_type: Optional[TriggerType] = None
    source_origin: Literal["fetched_page", "grounding", "fixture"] = "fetched_page"

    @field_validator("snippet")
    @classmethod
    def _snippet_len(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v.split()) > 25:
            raise ValueError("snippet must be 25 words or fewer")
        return v


class HiringSignal(_Model):
    title: str
    source_url: SourceUrl
    event_date: Optional[date] = None


class CompanyFacts(_Model):
    domain: str
    company_name: Optional[str] = None
    product_description: Optional[str] = None
    business_model: Optional[BusinessModel] = None
    buyer_type: Optional[BuyerType] = None
    funding_stage: Optional[FundingStage] = None
    employee_count_estimate: Optional[int] = None
    open_roles_count: Optional[int] = None
    geography: Optional[str] = None
    certifications_claimed: list[str] = Field(default_factory=list)
    has_enterprise_pricing_tier: Optional[bool] = None
    operating_status: Optional[Literal["operating", "closed", "discontinued", "pre_product", "stealth"]] = None
    acquired_by: Optional[str] = None
    parent_company: Optional[str] = None
    compliance_signals: list[Signal] = Field(default_factory=list)
    enterprise_signals: list[Signal] = Field(default_factory=list)
    hiring_signals: list[HiringSignal] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    extraction_notes: str = ""
    # provenance: field name -> source_url that supplied it
    field_sources: dict[str, str] = Field(default_factory=dict)
    # scalar facts that two sources disagree on (e.g. funding stage) -> NEEDS_RESEARCH
    conflicts: list[str] = Field(default_factory=list)
    # verbatim quote that supports each scalar fact (for the evidence trail)
    field_snippets: dict[str, str] = Field(default_factory=dict)
    # model-proposed triggers that code rejected, kept for transparency (0 points)
    rejected_triggers: list[dict] = Field(default_factory=list)


# --------------------------------------------------------------------------- step 3: qualification
class ComponentScore(_Model):
    component: Literal["saas_fit", "compliance_trigger", "stage", "enterprise", "recency"]
    points: int
    max_points: int
    bucket: str
    reason: str
    source_urls: list[str] = Field(default_factory=list)
    # Score -> Evidence -> Inference: every signal considered for this component, and the rule that turned it into points
    evidence: list[dict] = Field(default_factory=list)   # {text, quote, source_url, quality, event_date, points, counted, why}
    inference: str = ""


class ScoreBreakdown(_Model):
    components: list[ComponentScore]

    @property
    def total(self) -> int:
        return sum(c.points for c in self.components)

    def get(self, name: str) -> Optional[ComponentScore]:
        return next((c for c in self.components if c.component == name), None)


class QualificationRound(_Model):
    round: int
    missing_before: list[str]
    fetched_urls: list[str]
    missing_after: list[str]
    score: Optional[int] = None
    tokens: int = 0
    cost_usd: Optional[float] = None
    note: str = ""


class QualificationResult(_Model):
    domain: str
    status: QualStatus
    score: int
    breakdown: ScoreBreakdown
    model_proposed_score: Optional[int] = None
    score_diff: Optional[int] = None
    hard_disqualifier: Optional[str] = None
    reasons: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.LOW      # kept separate from score
    handoff_note: Optional[str] = None
    rounds: list[QualificationRound] = Field(default_factory=list)
    gates: list[dict] = Field(default_factory=list)      # [{gate, passed, detail}] -> verdict
    rubric_version: str = "v1"
    prompt_version: Optional[str] = None


# --------------------------------------------------------------------------- step 4: contacts
class Contact(_Model):
    name: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    position: Optional[str] = None
    seniority: Optional[str] = None
    department: Optional[str] = None
    email: Optional[str] = None
    confidence: Optional[int] = None
    verification_status: Optional[str] = None
    source: Literal["hunter", "apollo_manual", "role_mailbox", "fixture"] = "hunter"


class ContactChoice(_Model):
    domain: str
    chosen: Optional[Contact] = None
    runner_up: Optional[Contact] = None
    why_this_person: str = ""
    ranking_factors: list[str] = Field(default_factory=list)
    status: Literal["CHOSEN", "NO_CONTACT_FOUND"] = "CHOSEN"


# --------------------------------------------------------------------------- step 5: pain points
class PainPoint(_Model):
    signal: str
    source_url: SourceUrl
    event_date: Optional[date] = None
    hypothesis: str
    why_it_matters_to_this_role: str
    confidence: Confidence


# --------------------------------------------------------------------------- steps 6-7: email
class EvidenceClaim(_Model):
    claim: str
    source_url: SourceUrl


class EmailDraft(_Model):
    subject: str
    recipient_name: str
    opening_hook: str
    pain_point_1: str
    pain_point_2: Optional[str] = None       # None when only one evidence-backed pain point exists
    email_body: str
    personalisation_evidence: list[EvidenceClaim] = Field(default_factory=list)
    cta: str


class SendResult(_Model):
    domain: str
    to: str                                   # always DEMO_RECIPIENT_EMAIL
    intended_recipient: Optional[str] = None  # "Name <email>, Title" - audit only
    status: Literal["DRY_RUN", "SENT", "FAILED"]
    message_id: Optional[str] = None
    reason: Optional[ReasonCode] = None
    detail: Optional[str] = None
    timestamp: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- audit
class Failure(_Model):
    domain: Optional[str]
    step: Step
    reason: ReasonCode
    detail: str = ""
    at: datetime = Field(default_factory=utcnow)


class CostEntry(_Model):
    domain: Optional[str]
    step: Step
    kind: Literal["llm", "scrapingbee", "hunter_search", "hunter_verify", "search", "gmail"]
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    credits: int = 0
    calls: int = 1
    cost_usd: Optional[float] = None           # None = price unknown (never guessed)
    cached: bool = False
