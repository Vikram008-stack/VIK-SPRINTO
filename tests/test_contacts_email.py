import json
import re
from pathlib import Path

import pytest

from src.common.config import load_settings
from src.common.safety import UnsafeRecipientError
from src.contacts.hunter import HunterClient, HunterError, parse_contacts
from src.contacts.ranker import rank_contacts
from src.mail.gmail_sender import DRY_RUN_PLACEHOLDER, GmailSender, build_message
from src.mail.guardrails import check_draft
from src.store.models import Contact, EmailDraft, EvidenceClaim

S = load_settings()
FIX = Path(__file__).parent / "fixtures" / "hunter"


def contacts(domain):
    return parse_contacts(domain, json.loads((FIX / f"{domain}.json").read_text(encoding="utf-8")))


# ------------------------------------------------------------------ ranker
def test_ranker_skips_low_confidence_security_head_and_penalises_sales():
    choice, ranked = rank_contacts("ledgerly.io", contacts("ledgerly.io"), S.roles, employee_count=None)
    assert choice.chosen.name == "Arjun Mehta"                 # CTO (tier 3)
    assert choice.runner_up.name == "Tom Reyes"                # CEO (tier 4)
    by = {r.contact.name: r for r in ranked}
    assert "confidence 61" in by["Nina Shah"].excluded          # tier 1 title but Hunter confidence too low
    assert by["Sarah Cole"].tier is None and by["Sarah Cole"].excluded   # "Head of Sales" is not a decision-maker
    assert by["Lena Brooks"].excluded                           # "Manager" alone is not enough
    assert "tier 3" in choice.why_this_person and "Runner-up" in choice.why_this_person


def test_small_company_promotes_founder():
    choice, _ = rank_contacts("acme-thin.io", contacts("acme-thin.io"), S.roles, employee_count=20)
    assert choice.chosen.name == "Priya Raman"                 # founder promoted at <= 50 employees


def test_no_suitable_person():
    people = [Contact(name="A B", position="Marketing Manager", email="a@x.io", confidence=95),
              Contact(name="C D", position="Recruiter", email="c@gmail.com", confidence=95)]
    choice, _ = rank_contacts("x.io", people, S.roles, None)
    assert choice.status == "NO_CONTACT_FOUND" and choice.chosen is None


# ------------------------------------------------------------------ hunter quota / modes
def test_hunter_mock_mode_never_calls_network(db):
    h = HunterClient(S.pipeline.hunter, db, api_key="k", live_enabled=False)
    data = h.domain_search("ledgerly.io")
    assert data["data"]["domain"] == "ledgerly.io"
    assert db.hunter_usage()["searches_used"] == 0
    with pytest.raises(HunterError) as e:
        h.domain_search("unknown-domain.io")
    assert e.value.reason.value == "hunter_live_not_enabled"


def test_hunter_live_refused_at_hard_stop(db):
    cfg = S.pipeline.hunter.model_copy(update={"mode": "live", "hard_stop": 0})

    class Boom:
        def get(self, *a, **k):
            raise AssertionError("network must not be called")
    h = HunterClient(cfg, db, api_key="k", live_enabled=True, http=Boom())
    with pytest.raises(HunterError) as e:
        h.domain_search("acme.io")
    assert e.value.reason.value == "hunter_quota_exhausted"


def test_hunter_live_caches_full_response(db):
    cfg = S.pipeline.hunter.model_copy(update={"mode": "live"})
    payload = json.loads((FIX / "ledgerly.io.json").read_text(encoding="utf-8"))
    calls = []

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return payload

    class Http:
        def get(self, url, params):
            calls.append(params)
            return Resp()
    h = HunterClient(cfg, db, api_key="k", live_enabled=True, http=Http())
    h.domain_search("ledgerly.io")
    h.domain_search("ledgerly.io")
    searches = [c for c in calls if "domain" in c]
    assert len(searches) == 1                                    # second call served from cache
    assert db.hunter_usage()["searches_used"] == 1
    assert searches[0]["type"] == "personal" and "executive" in searches[0]["seniority"]


def test_hunter_account_counter_blocks_live_call(db):
    """On serverless the local counter resets; Hunter's own account counter is the authoritative guard."""
    cfg = S.pipeline.hunter.model_copy(update={"mode": "live", "hard_stop": 20})
    calls = []

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": {"requests": {"searches": {"used": 20, "available": 50}}}}

    class Http:
        def get(self, url, params):
            calls.append(url)
            return Resp()
    h = HunterClient(cfg, db, api_key="k", live_enabled=True, http=Http())
    with pytest.raises(HunterError) as e:
        h.domain_search("acme.io")
    assert e.value.reason.value == "hunter_quota_exhausted"
    assert all(u.endswith("/account") for u in calls)            # never reached domain-search


# ------------------------------------------------------------------ guardrails
DANIEL = Contact(name="Daniel Okafor", first_name="Daniel", position="Head of Engineering", email="d@acme.io")
GOOD = EmailDraft(
    subject="Acme Thin's first SOC 2 audit", recipient_name="Daniel",
    opening_hook="Acme Thin posted a Head of Compliance role on 20 October.",
    pain_point_1="Engineering often carries evidence collection.", pain_point_2=None,
    email_body=("Hi Daniel,\n\nI noticed Acme Thin posted a Head of Compliance role on 20 October to lead your first "
                "SOC 2 audit.\n\nTeams in that spot often lose engineering time to evidence collection. That might "
                "be landing on you.\n\nSprinto automates SOC 2 and ISO 27001 readiness and evidence collection.\n\n"
                "Would a 15-minute chat next week be useful?\n\nBest,\nVikram"),
    personalisation_evidence=[EvidenceClaim(claim="Head of Compliance role", source_url="https://acme.io/careers")],
    cta="15-minute chat")
ALLOWED = {"https://acme.io/careers"}


def test_good_draft_passes():
    assert check_draft(GOOD, DANIEL, ALLOWED, "https://acme.io/careers") == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d.model_copy(update={"email_body": d.email_body.replace("Hi Daniel", "Hi [Name]")}), "first name"),
    (lambda d: d.model_copy(update={"email_body": d.email_body + " We cut audit time by 80%."}), "statistic"),
    (lambda d: d.model_copy(update={"email_body": d.email_body.replace("Sprinto", "Our seamless, cutting-edge Sprinto")}),
     "buzzwords"),
    (lambda d: d.model_copy(update={"email_body": d.email_body + " Act now, this is urgent."}), "urgency"),
    (lambda d: d.model_copy(update={"email_body": d.email_body.replace("I noticed", "As we discussed, I know you're struggling;")}),
     "familiarity"),
    (lambda d: d.model_copy(update={"email_body": d.email_body.replace("audit.", "audit — big step.")}), "em dash"),
    (lambda d: d.model_copy(update={"email_body": d.email_body + " " + "word " * 95}), "words"),
    (lambda d: d.model_copy(update={"email_body": d.email_body.replace("often", "always").replace("might", "will"),
                                "pain_point_1": "Engineering carries evidence collection."}),
     "hedged"),
    (lambda d: d.model_copy(update={"email_body": d.email_body + " Or book a demo, or grab some time."}),
     "more than one call to action"),
    (lambda d: d.model_copy(update={"personalisation_evidence": [EvidenceClaim(claim="x", source_url="https://made.up/x")]}),
     "never collected"),
    (lambda d: d.model_copy(update={"email_body": d.email_body + " Trusted by 500 companies."}), "Sprinto claim"),
])
def test_guardrail_violations(mutate, expect):
    errs = check_draft(mutate(GOOD), DANIEL, ALLOWED, "https://acme.io/careers")
    assert any(expect.lower() in e.lower() for e in errs), errs


# ------------------------------------------------------------------ gmail safety
def test_message_only_ever_addressed_to_demo_recipient(monkeypatch):
    monkeypatch.setenv("DEMO_RECIPIENT_EMAIL", "me@example.com")
    msg = build_message(GOOD, DANIEL, "acme.io")
    assert msg["To"] == "me@example.com"
    body = msg.get_body(("plain",)).get_content()
    assert "Intended recipient: Daniel Okafor <d@acme.io>, Head of Engineering" in body
    assert msg["Cc"] is None and msg["Bcc"] is None


def test_send_refuses_without_demo_recipient(monkeypatch):
    monkeypatch.delenv("DEMO_RECIPIENT_EMAIL", raising=False)
    res = GmailSender(None, None, dry_run=False).send(GOOD, DANIEL, "acme.io")
    assert res.status == "FAILED" and res.reason.value == "unsafe_recipient"


def test_dry_run_without_recipient_uses_undeliverable_placeholder(monkeypatch, tmp_path):
    monkeypatch.delenv("DEMO_RECIPIENT_EMAIL", raising=False)
    res = GmailSender(None, None, dry_run=True, outbox_dir=tmp_path).send(GOOD, DANIEL, "acme.io")
    assert res.status == "DRY_RUN" and res.to == DRY_RUN_PLACEHOLDER
    assert (tmp_path / "acme.io.eml").exists()


def test_refuses_if_demo_recipient_is_the_prospect(monkeypatch):
    monkeypatch.setenv("DEMO_RECIPIENT_EMAIL", "d@acme.io")
    with pytest.raises(UnsafeRecipientError):
        build_message(GOOD, DANIEL, "acme.io")


def test_no_code_path_sets_a_different_to():
    """Static check: the only place a 'To' header is assigned is build_message(), from demo_recipient()."""
    src = Path(__file__).resolve().parents[1] / "src"
    hits = []
    for p in src.rglob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""\[\s*["'](To|Cc|Bcc)["']\s*\]\s*=""", line):
                hits.append(f"{p.name}:{i}")
    assert hits == [h for h in hits if h.startswith("gmail_sender.py")] and len(hits) == 1, hits


def test_cli_has_no_recipient_flag():
    cli = (Path(__file__).resolve().parents[1] / "src" / "cli.py").read_text(encoding="utf-8")
    assert not re.search(r'"--(to|recipient|email)"', cli)


def test_one_line_email_is_formatted():
    from src.mail.drafter import format_body
    out = format_body("Hi Ana, We saw your SOC 2 role. Teams often lose time. Open to a 15-minute chat? Best, Vikram")
    assert out.startswith("Hi Ana,\n\n") and out.endswith("Best,\nVikram")
    assert format_body("Hi Ana,\n\nKeep.\n\nBest,\nV") == "Hi Ana,\n\nKeep.\n\nBest,\nV"
