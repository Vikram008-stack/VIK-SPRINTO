import logging

import pytest

from src.common.budget import BudgetExceeded, CompanyBudget
from src.common.domains import InvalidDomain, normalise_domain, parse_domains, same_entity
from src.common.logging import _scrub, setup_logging
from src.common.safety import UnsafeRecipientError, assert_safe_recipient, startup_safety_check


# ---------------------------------------------------------------- domains
@pytest.mark.parametrize("raw", [
    "cal.com", "CAL.com", "www.cal.com", "https://www.cal.com/", "http://cal.com/pricing?x=1",
    "[www.cal.com](https://www.cal.com)", "cal.com.",
])
def test_normalise_variants(raw):
    assert normalise_domain(raw) == "cal.com"


@pytest.mark.parametrize("raw", ["", "not a domain", "localhost", "http://", "<USER_THIN_PICK>"])
def test_invalid_domains(raw):
    with pytest.raises(InvalidDomain):
        normalise_domain(raw)


def test_eval_list_dedupes_cal():
    lines = open("eval/domains.txt", encoding="utf-8").read().splitlines()
    parsed = parse_domains(lines)
    names = [d for d, _ in parsed.unique]
    assert names.count("cal.com") == 1
    assert ("cal.com", "[www.cal.com](https://www.cal.com)") in parsed.duplicates
    assert len(names) == 8


def test_same_entity():
    assert same_entity("cal.com", "https://app.cal.com/login")
    assert same_entity("cal.com", "https://www.cal.com")
    assert not same_entity("cal.com", "https://hugedomains.com/domain_profile.cfm?d=cal.com")


# ---------------------------------------------------------------- budget
def test_budget_cost_breach():
    b = CompanyBudget(max_cost_usd=0.05, max_seconds=120)
    b.add(cost_usd=0.03)
    b.check()
    b.add(cost_usd=0.03)
    with pytest.raises(BudgetExceeded):
        b.check()


def test_budget_time_breach():
    t = [0.0]
    b = CompanyBudget(max_cost_usd=1, max_seconds=120, clock=lambda: t[0])
    t[0] = 119
    b.check()
    t[0] = 121
    with pytest.raises(BudgetExceeded, match="seconds"):
        b.check()


def test_budget_unknown_cost_counted_not_guessed():
    b = CompanyBudget(max_cost_usd=0.05, max_seconds=120)
    b.add(cost_usd=None, tokens=500)
    assert b.cost_usd == 0 and b.unknown_cost_calls == 1 and b.tokens == 500


# ---------------------------------------------------------------- safety
def test_send_only_to_demo_recipient(monkeypatch):
    monkeypatch.setenv("DEMO_RECIPIENT_EMAIL", "me@example.com")
    assert assert_safe_recipient("ME@example.com ") == "me@example.com"
    with pytest.raises(UnsafeRecipientError):
        assert_safe_recipient("ciso@prospect.com")


def test_missing_demo_recipient_blocks_send(monkeypatch):
    monkeypatch.delenv("DEMO_RECIPIENT_EMAIL", raising=False)
    startup_safety_check(sending=False)  # dry run is fine
    with pytest.raises(UnsafeRecipientError):
        startup_safety_check(sending=True)


# ---------------------------------------------------------------- logging
def test_secrets_scrubbed(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "hunterkey123456")
    assert "hunterkey123456" not in _scrub("GET https://api.hunter.io/v2/domain-search?api_key=hunterkey123456")
    assert "<redacted>" in _scrub("https://x.io/?api_key=abcdef")


def test_jsonl_log_written(tmp_path):
    path = setup_logging(tmp_path, run_id="t1")
    logging.getLogger("sprinto.test").info("fetched", extra={"domain": "a.io", "mode": "plain"})
    for h in logging.getLogger("sprinto").handlers:
        h.flush()
    line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert '"domain": "a.io"' in line and '"mode": "plain"' in line
