"""Section 6 edge-case matrix: every row encoded as a test against the Python scorer."""
from datetime import date

import pytest

from src.common.config import load_settings
from src.qualify.scorer import QualContext, decide
from src.store.models import CompanyFacts, EvidenceQuality, HiringSignal, QualStatus, Signal

TODAY = date(2026, 11, 14)
R = load_settings().rubric
U = "https://acme.io"


def facts(**kw) -> CompanyFacts:
    base = dict(domain="acme.io", company_name="Acme", business_model="cloud_subscription", buyer_type="business",
                field_sources={"business_model": U + "/", "buyer_type": U + "/", "funding_stage": U + "/about",
                               "has_enterprise_pricing_tier": U + "/pricing"})
    base.update(kw)
    return CompanyFacts(**base)


def sig(trigger, q="STRONG", days_ago=30, url=U + "/news", text=None):
    return Signal(signal=text or f"{trigger} signal", source_url=url, trigger_type=trigger,
                  evidence_quality=EvidenceQuality(q),
                  event_date=None if days_ago is None else date.fromordinal(TODAY.toordinal() - days_ago))


def ent(q="STRONG"):
    return Signal(signal="Named enterprise customer contract", source_url=U + "/customers",
                  evidence_quality=EvidenceQuality(q), trigger_type=None)


def run(f, complete=True, **ctx):
    return decide(R, f, QualContext(today=TODAY, research_complete=complete, **ctx))


def test_series_b_saas_customer_requires_soc2_qualified():
    d = run(facts(funding_stage="series_b", compliance_signals=[sig("customer_requires_cert")],
                  enterprise_signals=[ent()]))
    assert d.status == QualStatus.QUALIFIED and d.score == 25 + 40 + 15 + 10 + 10


def test_series_a_saas_no_compliance_needs_research():
    d = run(facts(funding_stage="series_a"))
    assert d.status == QualStatus.NEEDS_RESEARCH
    assert any("unverified" in r for r in d.reasons)


def test_bootstrapped_enterprise_contract_requires_iso_qualified():
    d = run(facts(funding_stage="bootstrapped",
                  compliance_signals=[sig("customer_requires_cert", text="Enterprise contract requires ISO 27001")],
                  enterprise_signals=[ent()]))
    assert d.status == QualStatus.QUALIFIED


def test_series_b_it_consultancy_rejected():
    d = run(facts(business_model="it_services", funding_stage="series_b",
                  compliance_signals=[sig("customer_requires_cert")]))
    assert d.status == QualStatus.REJECTED and d.hard_disqualifier == "not_software"


def test_series_c_already_soc2_needs_research():
    d = run(facts(funding_stage="series_c", certifications_claimed=["SOC 2 Type II"],
                  field_sources={"business_model": U, "buyer_type": U, "funding_stage": U, "cert:SOC 2 Type II": U},
                  enterprise_signals=[ent()]))
    assert d.status == QualStatus.NEEDS_RESEARCH
    assert any("already SOC 2" in r for r in d.reasons)


def test_already_certified_disqualify_policy_switch():
    d = run(facts(funding_stage="series_c", certifications_claimed=["SOC 2"]), already_certified_policy="disqualify")
    assert d.status == QualStatus.REJECTED and d.hard_disqualifier == "already_certified"


def test_certified_but_targeting_new_framework_qualifies():
    d = run(facts(funding_stage="series_b", certifications_claimed=["SOC 2 Type II"],
                  compliance_signals=[sig("explicit_audit_target", text="Starting ISO 27001 certification")],
                  enterprise_signals=[ent()]))
    assert d.status == QualStatus.QUALIFIED


def test_series_a_hiring_security_lead_needs_research():
    # undated listing, B2B segment unclear -> 25 + 25 + 15 + 4 + 0 = 69
    f = facts(funding_stage="series_a",
              hiring_signals=[HiringSignal(title="Security Lead", source_url=U + "/careers")])
    d = run(f)
    assert d.score == 69 and d.status == QualStatus.NEEDS_RESEARCH


def test_security_engineer_hire_is_not_a_leader_trigger():
    f = facts(funding_stage="series_a",
              hiring_signals=[HiringSignal(title="Senior Security Engineer", source_url=U + "/careers")])
    d = run(f)
    assert d.breakdown.get("compliance_trigger").points == 10


@pytest.mark.parametrize("enterprise_signals", [[]])
def test_b2c_saas_no_enterprise_rejected_or_needs_research(enterprise_signals):
    d = run(facts(buyer_type="consumer", funding_stage="public", enterprise_signals=enterprise_signals))
    assert d.status in (QualStatus.REJECTED, QualStatus.NEEDS_RESEARCH)
    assert d.status == QualStatus.REJECTED   # score 25+0+8+2+0 = 35 after research, not plausible B2B


def test_enterprise_clients_no_cert_requirement_needs_research():
    d = run(facts(funding_stage="series_b", enterprise_signals=[ent()]))
    assert d.status == QualStatus.NEEDS_RESEARCH


def test_series_d_upcoming_soc2_audit_qualified():
    d = run(facts(funding_stage="series_d_plus", compliance_signals=[sig("explicit_audit_target", days_ago=None)]))
    assert d.score == 25 + 40 + 8 + 4 + 0
    assert d.status == QualStatus.QUALIFIED


def test_no_funding_info_evaluated_normally():
    d = run(facts(compliance_signals=[sig("customer_requires_cert")], enterprise_signals=[ent()]))
    assert d.breakdown.get("stage").points == 5 and d.status == QualStatus.QUALIFIED


def test_gdpr_only_in_privacy_policy_low_evidence():
    d = run(facts(funding_stage="series_a",
                  compliance_signals=[sig("generic_mention", q="WEAK", days_ago=None, url=U + "/privacy")]))
    assert d.breakdown.get("compliance_trigger").points == 5
    assert d.status == QualStatus.NEEDS_RESEARCH


def test_recently_acquired_needs_research():
    d = run(facts(funding_stage="series_b", acquired_by="BigCo", compliance_signals=[sig("customer_requires_cert")],
                  enterprise_signals=[ent()]))
    assert d.status == QualStatus.NEEDS_RESEARCH and any("acquired" in r for r in d.reasons)


def test_multiple_domains_needs_research():
    d = run(facts(funding_stage="series_b", compliance_signals=[sig("customer_requires_cert")],
                  enterprise_signals=[ent()]), flags=["domain_mismatch"])
    assert d.status == QualStatus.NEEDS_RESEARCH


def test_site_blocked_needs_research():
    d = run(CompanyFacts(domain="acme.io"), flags=["homepage_unusable"])
    assert d.status == QualStatus.NEEDS_RESEARCH


def test_stealth_needs_research():
    d = run(facts(operating_status="stealth"))
    assert d.status == QualStatus.NEEDS_RESEARCH


def test_conflicting_funding_needs_research():
    d = run(facts(funding_stage="series_b", conflicts=["funding_stage: 'series_a' vs 'series_b'"],
                  compliance_signals=[sig("customer_requires_cert")], enterprise_signals=[ent()]))
    assert d.status == QualStatus.NEEDS_RESEARCH


def test_ninety_point_saas_without_compliance_never_qualified():
    d = run(facts(funding_stage="series_b", enterprise_signals=[ent()]))
    assert d.status != QualStatus.QUALIFIED


def test_weak_trigger_cannot_qualify_even_with_high_score():
    d = run(facts(funding_stage="series_b", enterprise_signals=[ent()],
                  compliance_signals=[sig("explicit_audit_target", q="WEAK")]))
    assert d.score >= 75 and d.status == QualStatus.NEEDS_RESEARCH


def test_points_require_source_url():
    f = facts(funding_stage="series_b", field_sources={})   # no provenance for business model / buyer / stage
    d = run(f)
    assert d.breakdown.get("saas_fit").points == 0
    assert d.breakdown.get("stage").points == 0


def test_closed_business_rejected():
    assert run(facts(operating_status="closed")).hard_disqualifier == "closed_or_discontinued"


def test_parked_domain_rejected():
    assert run(CompanyFacts(domain="x.io"), flags=["parked_domain"]).status == QualStatus.REJECTED


def test_unknown_company_type_after_loop_is_insufficient_data():
    from src.qualify.scorer import decide as dec
    d = dec(R, CompanyFacts(domain="x.io"), QualContext(today=TODAY, research_complete=True),
            unmet_rows=["b2b_saas", "stage", "trigger"])
    assert d.status == QualStatus.INSUFFICIENT_DATA


def test_recency_uses_event_date_not_crawl_date():
    old = facts(funding_stage="series_b", compliance_signals=[sig("customer_requires_cert", days_ago=400)],
                enterprise_signals=[ent()])
    assert run(old).breakdown.get("recency").points == 1
    undated = facts(funding_stage="series_b", compliance_signals=[sig("customer_requires_cert", days_ago=None)])
    assert run(undated).breakdown.get("recency").points == 0
