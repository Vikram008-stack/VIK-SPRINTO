"""Follow-up agent: the six-field state object at every checkpoint (matches the workflow slide)."""
from datetime import date

import pytest

from src.followup.workflow import FIELDS, MAX_REPLY_ROUNDS, classify_reply, run_followup

TODAY = date(2026, 11, 18)   # a Wednesday
OUTCOME = {"domain": "ledgerly.io", "company_name": "Ledgerly", "status": "DRAFTED",
           "contact": {"chosen": {"name": "Arjun Mehta", "position": "CTO"}}}


def rows(r):
    return [(cp["title"], cp["state"]) for cp in r["checkpoints"]]


def test_six_fields_exactly():
    assert FIELDS == ["reply_status", "calendar_checked", "times_proposed", "reply_round", "meeting_status",
                      "crm_logged"]


def test_counter_proposal_path_matches_slide():
    r = run_followup(OUTCOME, "counter_proposes", today=TODAY)
    cps = r["checkpoints"]
    titles = [c["title"] for c in cps]
    assert titles == ["After Send Email", 'After prospect replies "Interested"', "After Check Calendar",
                      "After Send Email, proposing times", "If prospect counter-proposes", "After Book Meeting",
                      "After Update CRM"]
    s = [c["state"] for c in cps]
    # 1. after send email
    assert s[0] == {"reply_status": "no_reply", "calendar_checked": False, "times_proposed": [], "reply_round": 0,
                    "meeting_status": "unscheduled", "crm_logged": False}
    # 2. interested (branch, no tool)
    assert s[1]["reply_status"] == "interested" and cps[1]["tool"] is None and cps[1]["changed"] == ["reply_status"]
    # 3. calendar checked
    assert s[2]["calendar_checked"] is True and cps[2]["changed"] == ["calendar_checked"]
    # 4. times proposed, pending confirmation
    assert len(s[3]["times_proposed"]) == 2 and s[3]["meeting_status"] == "pending_confirmation"
    assert set(cps[3]["changed"]) == {"times_proposed", "meeting_status"}
    # 5. counter-proposal: loop back, calendar not yet re-checked, round 1
    assert s[4]["calendar_checked"] is False and s[4]["reply_round"] == 1
    assert set(cps[4]["changed"]) == {"calendar_checked", "reply_round"}
    assert s[4]["times_proposed"] == s[3]["times_proposed"]
    # 6. booked
    assert s[5]["meeting_status"] == "confirmed" and s[5]["calendar_checked"] is True
    # 7. CRM logged; final state has every field filled
    assert s[6]["crm_logged"] is True and cps[6]["changed"] == ["crm_logged"]
    assert r["final_state"] == s[6]
    assert r["crm_record"]["stage"] == "Meeting booked" and r["booked_slot"]
    assert r["calendar"]["booked"]["minutes"] == 15


def test_accepts_path_skips_the_loop():
    r = run_followup(OUTCOME, "accepts", today=TODAY)
    assert len(r["checkpoints"]) == 6
    assert r["final_state"]["reply_round"] == 0 and r["final_state"]["meeting_status"] == "confirmed"
    assert r["booked_slot"].startswith(r["final_state"]["times_proposed"][0])


def test_not_interested_stops_without_calendar_or_email():
    r = run_followup(OUTCOME, "not_interested", today=TODAY)
    fs = r["final_state"]
    assert fs["reply_status"] == "not_interested" and fs["calendar_checked"] is False
    assert fs["meeting_status"] == "declined" and fs["crm_logged"] is True
    assert [e["kind"] for e in r["emails"]] == ["cold_email"]          # nothing else was sent


def test_no_reply_logs_a_nudge_only():
    r = run_followup(OUTCOME, "no_reply", today=TODAY)
    assert r["final_state"]["reply_status"] == "no_reply" and r["final_state"]["crm_logged"] is True


def test_proposed_times_are_business_days_and_free():
    r = run_followup(OUTCOME, "accepts", today=TODAY)
    assert all(not t.startswith(("Nov 21", "Nov 22")) for t in r["final_state"]["times_proposed"])  # Sat/Sun


@pytest.mark.parametrize("text,intent", [
    ("Yes, sounds good. The first slot works.", "accept"),
    ("Not interested, please remove me.", "not_interested"),
    ("Can we do Friday 3pm instead?", "counter_propose"),
    ("hmm", "unclear"),
])
def test_reply_classifier(text, intent):
    assert classify_reply(text, TODAY)["intent"] == intent


def test_pasted_reply_drives_the_workflow():
    r = run_followup(OUTCOME, "accepts", reply_text="Thanks! Could we do Friday 3pm instead?", today=TODAY)
    assert r["classified_reply"]["intent"] == "counter_propose"
    assert r["final_state"]["reply_round"] == 1 and r["final_state"]["meeting_status"] == "confirmed"
    assert r["booked_slot"] == "Nov 20 3pm IST"


def test_unclear_reply_goes_to_a_human():
    r = run_followup(OUTCOME, "accepts", reply_text="hmm, maybe", today=TODAY)
    assert r["final_state"]["meeting_status"] == "needs_human"


def test_requires_a_contact():
    with pytest.raises(ValueError):
        run_followup({"domain": "x.io", "status": "DRAFTED"}, "accepts", today=TODAY)


def test_loop_cap_constant():
    assert MAX_REPLY_ROUNDS == 2


def test_api_followup(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SPRINTO_DATA_DIR", str(tmp_path))
    import api.index as idx
    c = TestClient(idx.app)
    assert c.post("/api/followup", json={"outcome": OUTCOME, "scenario": "bogus"}).status_code == 400
    assert c.post("/api/followup", json={"outcome": {**OUTCOME, "status": "REJECTED"}}).status_code == 409
    assert c.post("/api/followup", json={"outcome": OUTCOME, "reply_text": "  "}).status_code == 400
    r = c.post("/api/followup", json={"outcome": OUTCOME, "scenario": "counter_proposes", "today": "2026-11-18"})
    assert r.status_code == 200 and len(r.json()["checkpoints"]) == 7
