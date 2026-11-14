from datetime import date

import pytest
from pydantic import ValidationError

from src.store.models import (
    CompanyFacts, ComponentScore, EvidenceQuality, PainPoint, ScoreBreakdown, Signal,
)


def test_signal_requires_source_url():
    with pytest.raises(ValidationError):
        Signal(signal="Preparing for SOC 2", source_url="")
    with pytest.raises(ValidationError):
        Signal(signal="Preparing for SOC 2", source_url="model said so")


def test_signal_valid_and_dates_separate():
    s = Signal(signal="Hiring Head of Compliance", source_url="https://acme.io/careers",
               event_date=date(2026, 10, 1), evidence_quality=EvidenceQuality.MEDIUM)
    assert s.event_date == date(2026, 10, 1)
    # observed_at is stamped independently (now), never copied from event_date
    assert s.observed_at.date() > s.event_date


def test_signal_undated_is_none_not_crawl_date():
    s = Signal(signal="SOC 2 mention", source_url="https://acme.io/security")
    assert s.event_date is None


def test_snippet_max_25_words():
    with pytest.raises(ValidationError, match="25 words"):
        Signal(signal="x", source_url="https://a.io", snippet=" ".join(["w"] * 26))


def test_company_facts_all_nullable():
    f = CompanyFacts(domain="acme.io")
    assert f.funding_stage is None and f.business_model is None
    assert f.compliance_signals == []


def test_company_facts_rejects_unknown_enum():
    with pytest.raises(ValidationError):
        CompanyFacts(domain="acme.io", funding_stage="probably_series_b")


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        CompanyFacts(domain="acme.io", made_up_field=1)


def test_pain_point_needs_url():
    with pytest.raises(ValidationError):
        PainPoint(signal="s", source_url="", hypothesis="h", why_it_matters_to_this_role="w", confidence="LOW")


def test_breakdown_total():
    b = ScoreBreakdown(components=[
        ComponentScore(component="saas_fit", points=25, max_points=25, bucket="cloud_subscription", reason=""),
        ComponentScore(component="compliance_trigger", points=40, max_points=40, bucket="explicit_audit_target",
                       reason="", source_urls=["https://a.io/news"]),
    ])
    assert b.total == 65
    assert b.get("saas_fit").points == 25
