"""Web API: valid flow, invalid/missing/edge inputs, safety. No network (demo + guarded paths only)."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("SPRINTO_DATA_DIR", str(tmp_path / "data"))
    import importlib

    import api.index as idx
    importlib.reload(idx)
    return TestClient(idx.app), idx


def test_health_reports_booleans_only(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value-xyz")
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.text
    assert "secret-value-xyz" not in body
    assert isinstance(r.json()["services"]["gemini"], bool)


@pytest.mark.parametrize("payload,code", [({}, "empty_input"), ({"text": "   "}, "empty_input"),
                                          ({"text": 5}, "empty_input"), ({"text": "#comment only"}, "empty_input")])
def test_parse_empty_inputs(client, payload, code):
    c, _ = client
    r = c.post("/api/parse", json=payload)
    assert r.status_code == 400 and r.json()["error"] == code


def test_parse_mixed_input(client):
    c, _ = client
    text = "domain\nchargebee.com, https://www.Chargebee.com/\n[www.cal.com](https://www.cal.com)\ncal.com\nnot a domain\nlocalhost"
    r = c.post("/api/parse", json={"text": text}).json()
    assert [u["domain"] for u in r["unique"]] == ["chargebee.com", "cal.com"]
    assert len(r["duplicates"]) == 2
    assert {i["raw"] for i in r["invalid"]} >= {"localhost"}


def test_parse_truncates_large_batches(client):
    c, idx = client
    text = "\n".join(f"company{i}.com" for i in range(idx.MAX_DOMAINS + 3))
    r = c.post("/api/parse", json={"text": text}).json()
    assert len(r["unique"]) == idx.MAX_DOMAINS and len(r["truncated"]) == 3 and r["message"]


def test_parse_too_large(client):
    c, _ = client
    r = c.post("/api/parse", json={"text": "a.com\n" * 5000})
    assert r.status_code == 413


def test_process_invalid_domain(client):
    c, _ = client
    r = c.post("/api/process", json={"domain": "not a domain"})
    assert r.status_code == 422 and r.json()["outcome"]["status"] == "INVALID"


def test_process_missing_domain(client):
    c, _ = client
    assert c.post("/api/process", json={}).status_code == 400
    assert c.post("/api/process", content=b"not json", headers={"Content-Type": "application/json"}).status_code in (400, 422)


def test_process_without_ai_key_is_a_clear_failure(client, monkeypatch):
    c, idx = client
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from src.common import config
    orig = config.load_secrets
    monkeypatch.setattr(idx, "load_settings", lambda: config.load_settings(env_file=None).model_copy(
        update={"secrets": orig().model_copy(update={"gemini_api_key": None})}))
    r = c.post("/api/process", json={"domain": "acme.com"})
    assert r.status_code == 200
    o = r.json()
    assert o["status"] == "FAILED" and o["failures"][0]["reason"] == "missing_config"


def test_demo_end_to_end(client):
    c, _ = client
    r = c.post("/api/demo")
    assert r.status_code == 200
    by = {o["domain"] + ("#dup" if o["status"] == "DUPLICATE" else ""): o for o in r.json()["outcomes"]}
    assert by["ledgerly.io"]["status"] == "DRAFTED"
    assert by["brightpath.io"]["status"] == "REJECTED"
    assert by["blocked-demo.io"]["status"] == "NEEDS_RESEARCH"
    assert by["acme-thin.io#dup"]["status"] == "DUPLICATE"


def test_send_rejects_bad_requests_and_bad_drafts(client):
    c, _ = client
    assert c.post("/api/send", json={}).status_code == 400
    demo = {o["domain"]: o for o in c.post("/api/demo").json()["outcomes"]}
    o = demo["ledgerly.io"]
    o["draft"]["email_body"] = o["draft"]["email_body"] + " We guarantee 90% less audit work. Act now!"
    r = c.post("/api/send", json={"outcome": o})
    assert r.status_code == 422 and r.json()["error"] == "guardrail_failed"


def test_send_without_gmail_fails_gracefully(client, monkeypatch):
    c, _ = client
    for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_TOKEN_JSON"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEMO_RECIPIENT_EMAIL", "me@example.com")
    monkeypatch.setenv("GMAIL_TOKEN_FILE", "data/secrets/does-not-exist.json")
    demo = {o["domain"]: o for o in c.post("/api/demo").json()["outcomes"]}
    r = c.post("/api/send", json={"outcome": demo["ledgerly.io"]})
    body = r.json()
    assert body["status"] == "FAILED" and body["reason"] in ("gmail_auth_error",)
    assert body["to"] == "me@example.com"


def test_static_ui_served_locally(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "Sprinto Outreach Desk" in r.text
    assert c.get("/desk.js").status_code == 200
