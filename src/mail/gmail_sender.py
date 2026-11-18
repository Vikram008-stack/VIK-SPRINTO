"""Step 7: Gmail send. The ONLY recipient is DEMO_RECIPIENT_EMAIL.

- build_message() is the single place a message is constructed. It calls assert_safe_recipient() and the
  real prospect's address appears only in the demo header line inside the body ("Intended recipient: ...").
- send() re-asserts the To header immediately before the API call.
- Dry run (default) writes an .eml file and records status DRY_RUN; nothing leaves the machine.
- OAuth installed-app flow, gmail.send scope only, token cached under data/secrets/.
"""
from __future__ import annotations

import base64
import json
import os
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import httpx

from src.common.logging import get_logger
from src.common.safety import UnsafeRecipientError, assert_safe_recipient, demo_recipient
from src.store.models import Contact, EmailDraft, ReasonCode, SendResult

log = get_logger(__name__)
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def intended_line(contact: Contact) -> str:
    return f"Intended recipient: {contact.name} <{contact.email or 'no email'}>, {contact.position or 'unknown title'}"


DRY_RUN_PLACEHOLDER = "demo-inbox-not-configured@example.invalid"   # reserved TLD: undeliverable by design


def build_message(draft: EmailDraft, contact: Contact, domain: str, sender_label: str = "",
                  dry_run: bool = False) -> EmailMessage:
    try:
        to = assert_safe_recipient(demo_recipient())
    except UnsafeRecipientError:
        if not dry_run:
            raise
        to = DRY_RUN_PLACEHOLDER      # dry run never sends; the .eml shows where it WOULD go
    if contact.email and to.lower() == contact.email.lower():
        raise UnsafeRecipientError("demo recipient equals the prospect's address; refusing")
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = f"[DEMO] {draft.subject}"
    if sender_label:
        msg["X-Sprinto-Demo"] = sender_label
    header = (f"{intended_line(contact)}\nCompany: {domain}\n"
              f"(Demo build: this email was sent to the demo inbox only. Human review required before any real send.)\n"
              + "-" * 60 + "\n\n")
    msg.set_content(header + draft.email_body)
    evidence = "\n".join(f"- {c.claim} :: {c.source_url}" for c in draft.personalisation_evidence)
    msg.add_alternative(
        "<div style='font-family:system-ui,Segoe UI,Arial,sans-serif;font-size:14px;line-height:1.55;color:#1f2933'>"
        f"<div style='background:#f6f3ee;border-left:3px solid #c08457;padding:10px 12px;margin-bottom:16px;"
        f"font-size:12px;color:#52606d'>{_esc(intended_line(contact))}<br>Company: {_esc(domain)}<br>"
        "Demo build: delivered to the demo inbox only.</div>"
        f"<div style='white-space:pre-wrap'>{_esc(draft.email_body)}</div>"
        f"<hr style='border:none;border-top:1px solid #e4e7eb;margin:18px 0'>"
        f"<div style='font-size:12px;color:#7b8794'><b>Evidence behind this email</b><br>"
        f"{_esc(evidence).replace(chr(10), '<br>')}</div></div>", subtype="html")
    return msg


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class GmailAuthError(RuntimeError):
    pass


class GmailSender:
    """Sends via the Gmail REST API with an OAuth refresh token (gmail.send scope only).

    Credentials, first match wins: env GMAIL_CLIENT_ID + GMAIL_CLIENT_SECRET + GMAIL_REFRESH_TOKEN (Vercel),
    env GMAIL_TOKEN_JSON (authorized-user JSON), or the token file created by `python -m src.cli gmail-auth`.
    """
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

    def __init__(self, client_secret_file: Optional[str], token_file: Optional[str], dry_run: bool = True,
                 outbox_dir: Optional[Path] = None, http: Optional[httpx.Client] = None):
        self.client_secret_file = client_secret_file
        self.token_file = token_file
        self.dry_run = dry_run
        self.outbox = outbox_dir
        self.http = http or httpx.Client(timeout=30)

    def _creds(self) -> dict:
        cid, csec, rt = (os.getenv("GMAIL_CLIENT_ID"), os.getenv("GMAIL_CLIENT_SECRET"),
                         os.getenv("GMAIL_REFRESH_TOKEN"))
        if cid and csec and rt:
            return {"client_id": cid, "client_secret": csec, "refresh_token": rt}
        blob = os.getenv("GMAIL_TOKEN_JSON")
        if blob:
            return json.loads(blob)
        tok = Path(self.token_file) if self.token_file else None
        if tok and not tok.is_absolute():
            tok = Path(__file__).resolve().parents[2] / tok
        if tok and tok.exists():
            return json.loads(tok.read_text(encoding="utf-8"))
        raise GmailAuthError("Gmail sending is not connected on this server, so the draft was kept and nothing was "
                             "sent. (Setup: run `python -m src.cli gmail-auth` locally, then set GMAIL_CLIENT_ID, "
                             "GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN.)")

    def configured(self) -> bool:
        try:
            c = self._creds()
            return bool(c.get("refresh_token") and c.get("client_id"))
        except Exception:
            return False

    def _access_token(self) -> str:
        c = self._creds()
        r = self.http.post(self.TOKEN_URL, data={"client_id": c["client_id"], "client_secret": c["client_secret"],
                                                 "refresh_token": c["refresh_token"], "grant_type": "refresh_token"})
        if r.status_code != 200:
            raise GmailAuthError(f"token refresh failed (HTTP {r.status_code}): {r.text[:150]}")
        return r.json()["access_token"]

    def send(self, draft: EmailDraft, contact: Contact, domain: str) -> SendResult:
        intended = intended_line(contact)
        try:
            msg = build_message(draft, contact, domain, dry_run=self.dry_run)
            if not self.dry_run:
                assert_safe_recipient(msg["To"])    # re-assert right before any real send
        except UnsafeRecipientError as e:
            return SendResult(domain=domain, to="(blocked)", intended_recipient=intended, status="FAILED",
                              reason=ReasonCode.UNSAFE_RECIPIENT, detail=str(e))
        except Exception as e:
            return SendResult(domain=domain, to="(none)", intended_recipient=intended, status="FAILED",
                              reason=ReasonCode.GMAIL_MIME_ERROR, detail=f"{type(e).__name__}: {e}"[:300])
        to = msg["To"]
        if self.dry_run:
            if self.outbox:
                try:
                    self.outbox.mkdir(parents=True, exist_ok=True)
                    (self.outbox / f"{domain}.eml").write_bytes(bytes(msg))
                except OSError:
                    pass
            return SendResult(domain=domain, to=to, intended_recipient=intended, status="DRY_RUN")
        try:
            raw = base64.urlsafe_b64encode(bytes(msg)).decode()
            token = self._access_token()
            r = self.http.post(self.SEND_URL, json={"raw": raw}, headers={"Authorization": f"Bearer {token}"})
        except GmailAuthError as e:
            return SendResult(domain=domain, to=to, intended_recipient=intended, status="FAILED",
                              reason=ReasonCode.GMAIL_AUTH_ERROR, detail=str(e)[:300])
        except Exception as e:
            return SendResult(domain=domain, to=to, intended_recipient=intended, status="FAILED",
                              reason=ReasonCode.GMAIL_SEND_ERROR, detail=f"{type(e).__name__}: {e}"[:300])
        if r.status_code == 200:
            mid = r.json().get("id")
            log.info("gmail sent", extra={"domain": domain, "message_id": mid})
            return SendResult(domain=domain, to=to, intended_recipient=intended, status="SENT", message_id=mid)
        text = r.text[:300]
        if r.status_code in (401,) or "invalid_grant" in text:
            reason = ReasonCode.GMAIL_AUTH_ERROR
        elif r.status_code == 429 or "rateLimitExceeded" in text or "quota" in text.lower():
            reason = ReasonCode.GMAIL_QUOTA_ERROR
        else:
            reason = ReasonCode.GMAIL_SEND_ERROR
        return SendResult(domain=domain, to=to, intended_recipient=intended, status="FAILED", reason=reason,
                          detail=f"HTTP {r.status_code}: {text}")


def run_oauth_flow(client_secret_file: str, token_file: str) -> Path:
    """One-time local consent (opens a browser). Saves an authorized-user JSON with a refresh token."""
    from google_auth_oauthlib.flow import InstalledAppFlow   # dev dependency only
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    out = Path(token_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(creds.to_json(), encoding="utf-8")
    return out
