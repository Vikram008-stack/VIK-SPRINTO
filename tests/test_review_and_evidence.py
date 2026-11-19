"""Score -> evidence -> inference, code validation of model triggers, and the human approve / reject path."""
import pytest
from fastapi.testclient import TestClient

from src.extract.extractor import trigger_supported


# ------------------------------------------------------------------ trigger validation (code checks the quote)
@pytest.mark.parametrize("ttype,quote,ok", [
    ("enterprise_expansion", "Quote multi-year, multi-product deals with native approval workflows", False),
    ("enterprise_expansion", "Enterprise plan adds SSO, audit logs and a dedicated success manager", True),
    ("explicit_audit_target", "We are preparing for our SOC 2 Type II audit in Q1 2027", True),
    ("explicit_audit_target", "We help finance teams close their books faster", False),
    ("customer_requires_cert", "Our largest customer requires ISO 27001 before renewal", True),
    ("customer_requires_cert", "Customers love our ISO 27001 badge", False),
    ("grc_leader_hire", "Senior Frontend Engineer", False),
    ("procurement_questionnaire", "We answer every security questionnaire within a day", True),
])
def test_trigger_needs_supporting_quote(ttype, quote, ok):
    assert trigger_supported(ttype, "", quote)[0] is ok


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("SPRINTO_DATA_DIR", str(tmp_path))
    import api.index as idx
    return TestClient(idx.app)


@pytest.fixture
def demo(client):
    out = client.post("/api/demo").json()["outcomes"]
    return {o["domain"] + ("#dup" if o["status"] == "DUPLICATE" else ""): o for o in out}


# ------------------------------------------------------------------ evidence chain
def test_every_component_has_inference_and_points_trace_to_evidence(demo):
    q = demo["acme-thin.io"]["qualification"]
    for c in q["breakdown"]["components"]:
        assert c["inference"], c["component"]
        counted = [e for e in c["evidence"] if e["counted"]]
        if c["points"] and c["component"] != "stage":
            assert counted and all(e["source_url"] for e in counted), c["component"]
    trig = next(c for c in q["breakdown"]["components"] if c["component"] == "compliance_trigger")
    assert sum(1 for e in trig["evidence"] if e["counted"]) == 1          # strongest only
    assert any("counted once" in e["why"] for e in trig["evidence"])        # duplicate event not double counted
    gates = q["gates"]
    assert gates and all(g["passed"] for g in gates) and q["status"] == "QUALIFIED"


def test_failed_gates_are_listed(demo):
    gates = demo["schedulr.io"]["qualification"]["gates"]
    assert any(not g["passed"] and "certified" in g["gate"] for g in gates)


def test_hard_disqualifier_gate(demo):
    gates = demo["brightpath.io"]["qualification"]["gates"]
    assert gates == [{"gate": "no hard disqualifier", "passed": False, "detail": "not software overrides the score"}]


# ------------------------------------------------------------------ human decision
def test_approve_needs_reason(client, demo):
    r = client.post("/api/approve", json={"outcome": demo["schedulr.io"], "decision": "approve", "reason": " "})
    assert r.status_code == 400 and r.json()["error"] == "reason_required"


def test_hard_disqualifier_cannot_be_approved_but_can_be_rejected(client, demo):
    r = client.post("/api/approve", json={"outcome": demo["brightpath.io"], "decision": "approve", "reason": "trust me"})
    assert r.status_code == 409 and r.json()["error"] == "hard_disqualifier"


def test_cannot_review_a_drafted_company(client, demo):
    r = client.post("/api/approve", json={"outcome": demo["ledgerly.io"], "decision": "approve", "reason": "x y z"})
    assert r.status_code == 409


def test_approve_runs_contact_and_draft(client, demo):
    r = client.post("/api/approve", json={"outcome": demo["schedulr.io"], "decision": "approve",
                                          "reason": "Renewal season and healthcare buyers"}).json()
    assert r["status"] == "DRAFTED"
    assert r["human_override"]["original_status"] == "NEEDS_RESEARCH" and r["human_override"]["original_score"] == 50
    assert r["contact"]["chosen"]["name"] == "Leo Park"                      # Head of Information Security (tier 1)
    assert r["draft_attempts"][-1]["errors"] == []
    assert r["qualification"]["status"] == "NEEDS_RESEARCH"                  # the rules' verdict stays on record
    assert r["status_reason"].startswith("Approved by a human")


def test_reject_is_logged(client, demo):
    r = client.post("/api/approve", json={"outcome": demo["blocked-demo.io"], "decision": "reject",
                                          "reason": "Not our segment"}).json()
    assert r["status"] == "REJECTED" and r["human_override"]["decision"] == "reject"


def test_followup_works_after_human_approval(client, demo):
    a = client.post("/api/approve", json={"outcome": demo["schedulr.io"], "decision": "approve",
                                          "reason": "Renewal season"}).json()
    f = client.post("/api/followup", json={"outcome": a, "scenario": "accepts", "today": "2026-11-14"}).json()
    assert f["final_state"]["meeting_status"] == "confirmed"
